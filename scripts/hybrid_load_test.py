"""Hybrid copy-trade load campaign.

The master and every REAL follower use signed Aster testnet calls. Additional
followers are seeded as SHADOW rows in a fresh SQLite database and execute
against the in-process ShadowExchange. The production coordinator, planner,
executor and journal are shared by both paths.

Safety properties:
- refuses a non-testnet REST URL;
- clones only encrypted REAL credentials into a run database;
- never creates credentials for SHADOW rows;
- refuses to start if real accounts are not flat and order-free;
- always attempts cleanup of real and shadow accounts in ``finally``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import httpx

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CredentialRepo,
    NonceStore,
    SettingsRepo,
)

SYMBOL = "BTCUSDT"
DEFAULT_QTY = Decimal("0.002")
DEFAULT_STOP_DISTANCE = Decimal("500")
DEFAULT_CYCLES = 100
DEFAULT_FOLLOWERS = 30
DEFAULT_TIMEOUT_S = 30.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="signed testnet + shadow copy load campaign")
    parser.add_argument("--source-db", type=Path, default=None)
    parser.add_argument("--run-db", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--followers", type=int, default=DEFAULT_FOLLOWERS)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--quantity", type=Decimal, default=DEFAULT_QTY)
    parser.add_argument("--stop-distance", type=Decimal, default=DEFAULT_STOP_DISTANCE)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    return parser


async def _clone_real_accounts(cfg: Settings, source_path: Path, run_path: Path) -> None:
    if run_path.exists():
        raise RuntimeError(f"refusing to overwrite existing run database: {run_path}")
    source = Database(source_path)
    target = Database(run_path)
    await source.connect()
    await target.connect()
    try:
        await source.migrate()
        await target.migrate()
        account_repo = AccountRepo(target)
        source_credentials = CredentialRepo(source)
        target_credentials = CredentialRepo(target)
        source_settings = SettingsRepo(source)
        target_settings = SettingsRepo(target)
        rows = await source.fetchall(
            "SELECT id, role, label, user_address, enabled FROM accounts "
            "WHERE COALESCE(execution_mode, 'REAL')='REAL' ORDER BY id"
        )
        if not any(str(row["id"]) == "main" for row in rows):
            raise RuntimeError("source DB has no main account")
        for row in rows:
            account_id = str(row["id"])
            await account_repo.upsert(
                account_id,
                str(row["role"]),
                str(row["label"]),
                str(row["user_address"]),
                execution_mode="REAL",
            )
            await target.execute_write(
                "UPDATE accounts SET enabled=? WHERE id=?",
                (int(row["enabled"]), account_id),
            )
            blob = await source_credentials.get(account_id)
            await target_credentials.put_encrypted(account_id, blob)
            policy = await source_settings.get_policy(account_id)
            if policy is not None:
                await target_settings.upsert_policy(policy, account_id)
    finally:
        await source.close()
        await target.close()


async def _seed_shadow_followers(
    db_path: Path, total_followers: int, shadow_balance: Decimal = Decimal("1000")
) -> list[str]:
    db = Database(db_path)
    await db.connect()
    try:
        await db.migrate()
        rows = await db.fetchall(
            "SELECT id FROM accounts WHERE role='follower' AND "
            "COALESCE(execution_mode, 'REAL')='REAL' ORDER BY id"
        )
        real_count = len(rows)
        if total_followers < real_count:
            raise RuntimeError(
                f"requested {total_followers} followers but source has {real_count} REAL followers"
            )
        shadow_count = total_followers - real_count
        account_repo = AccountRepo(db)
        settings = SettingsRepo(db)
        from copytradeaster.domain.models import RiskPolicy

        for index in range(1, shadow_count + 1):
            account_id = f"shadow-{index:03d}"
            await account_repo.upsert(
                account_id,
                "follower",
                account_id,
                f"shadow://{account_id}",
                execution_mode="SHADOW",
            )
            await settings.upsert_policy(
                RiskPolicy(
                    risk_percent=Decimal("0.01"),
                    max_leverage=20,
                    max_entry_deviation_pct=Decimal("0.01"),
                ),
                account_id,
            )
        return [str(row["id"]) for row in rows] + [
            f"shadow-{index:03d}" for index in range(1, shadow_count + 1)
        ]
    finally:
        await db.close()


async def _real_account_ids(db_path: Path) -> list[str]:
    db = Database(db_path)
    await db.connect()
    try:
        rows = await db.fetchall(
            "SELECT id FROM accounts WHERE role='follower' AND "
            "COALESCE(execution_mode, 'REAL')='REAL' ORDER BY id"
        )
        return [str(row["id"]) for row in rows]
    finally:
        await db.close()


async def _preflight(cfg: Settings, db_path: Path) -> dict[str, bool]:
    """Read-only real-account gate before App.start can reconcile anything."""
    db = Database(db_path)
    await db.connect()
    await db.migrate()
    creds = SqliteCredentials(db, cfg.encryption_key_bytes())
    await creds.load()
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        cfg.weight_limit_per_min,
        cfg.orders_limit_per_min,
        cfg.orders_limit_per_10s,
    )
    client = AsterClient(
        http,
        cfg.rest_base_url,
        limiter,
        creds,
        creds,
        NonceStore(db),
    )
    try:
        rows = await db.fetchall(
            "SELECT id FROM accounts WHERE COALESCE(execution_mode, 'REAL')='REAL' "
            "AND (role='master' OR role='follower') ORDER BY id"
        )
        modes: dict[str, bool] = {}
        for row in rows:
            account_id = str(row["id"])
            positions = await client.position_risk(account_id)
            orders = await client.open_orders(account_id)
            if any(p.position_amt != 0 for p in positions) or orders:
                raise RuntimeError(
                    f"preflight refused: {account_id} is not flat/order-free "
                    f"(positions={len(positions)}, orders={len(orders)})"
                )
            modes[account_id] = await client.get_position_mode(account_id)
        return modes
    finally:
        await http.aclose()
        await db.close()


def _snap_down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


async def _position(client, account_id: str) -> list:
    return [p for p in await client.position_risk(account_id, SYMBOL) if p.position_amt != 0]


def _unknown_order_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, AsterError):
        return False
    if exc.code in {-1000, -1006, -1007} or exc.http_status in {503, 504}:
        return True
    message = exc.msg.lower()
    return exc.code == 0 and ("timeout" in message or "timed out" in message)


async def _master_position_abs(client, account_id: str, position_side: PosSide) -> Decimal:
    positions = await client.position_risk(account_id, SYMBOL)
    return sum(
        (abs(p.position_amt) for p in positions if p.position_side == position_side),
        Decimal(0),
    )


async def _submit_master_order(
    client,
    request: PlaceOrderRequest,
    retry_stats: dict[str, int],
) -> str:
    """Submit a master order with outcome-unknown recovery.

    A timed-out HTTP response is not treated as a safe rejection. The order is
    queried first; market requests additionally reconcile the actual position
    and retry only the residual quantity, preventing an accidental duplicate
    close/open.
    """
    current = request
    before_abs = await _master_position_abs(client, "main", request.position_side)
    requested_abs = abs(request.quantity or Decimal(0))
    target_abs = (
        before_abs + requested_abs
        if request.side == Side.BUY
        else max(before_abs - requested_abs, Decimal(0))
    )
    for attempt in range(3):
        try:
            order = await client.place_order("main", current)
            return order.client_order_id or current.new_client_order_id
        except Exception as exc:  # noqa: BLE001 - classify only unknown outcomes below
            if not _unknown_order_error(exc):
                raise
            retry_stats["unknown_outcomes"] += 1
            for probe in range(3):
                await asyncio.sleep(0.4 * (probe + 1))
                found = await client.query_order(
                    "main", current.symbol, orig_client_order_id=current.new_client_order_id
                )
                if found is not None:
                    retry_stats["resolved_by_query"] += 1
                    return found.client_order_id
                if current.type != "MARKET":
                    continue
                actual_abs = await _master_position_abs(client, "main", current.position_side)
                if current.side == Side.BUY and actual_abs >= target_abs:
                    retry_stats["resolved_by_position"] += 1
                    return current.new_client_order_id
                if current.side == Side.SELL and actual_abs <= target_abs:
                    retry_stats["resolved_by_position"] += 1
                    return current.new_client_order_id
                if current.side == Side.BUY and before_abs < actual_abs < target_abs:
                    current = replace(current, quantity=target_abs - actual_abs)
                    retry_stats["partial_residuals"] += 1
                    break
                if current.side == Side.SELL and target_abs < actual_abs < before_abs:
                    current = replace(current, quantity=actual_abs - target_abs)
                    retry_stats["partial_residuals"] += 1
                    break
            if attempt == 2:
                raise
            retry_stats["retries"] += 1
            current = replace(
                current,
                new_client_order_id=f"{request.new_client_order_id}-r{attempt + 2}"[:36],
            )
    raise RuntimeError("master order retry loop exhausted")


async def _cancel_master_order(
    client, symbol: str, client_order_id: str, retry_stats: dict[str, int]
) -> None:
    for attempt in range(3):
        try:
            await client.cancel_order("main", symbol, orig_client_order_id=client_order_id)
            return
        except AsterError as exc:
            if exc.code == -2011:
                return
            if not _unknown_order_error(exc):
                raise
            retry_stats["unknown_outcomes"] += 1
            await asyncio.sleep(0.5 * (attempt + 1))
            found = await client.query_order("main", symbol, orig_client_order_id=client_order_id)
            if found is None or found.status in {"CANCELED", "EXPIRED", "FILLED"}:
                retry_stats["resolved_by_query"] += 1
                return
            retry_stats["retries"] += 1
    raise RuntimeError(f"could not cancel master order {client_order_id}")


async def _wait_until(predicate, timeout: float, description: str, interval: float = 0.15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}")


async def _follower_state(app, want_open: bool, want_protection: bool | None = None) -> bool:
    async def wait_for_generation(fr) -> None:
        await app.executor.wait_until_idle(fr.account_id)
        async with fr.lock:
            return

    await asyncio.gather(*(wait_for_generation(fr) for fr in app.followers))
    if not app._ingest_queue.empty():
        return False
    checks = []
    for fr in app.followers:
        positions, orders = await asyncio.gather(
            fr.client.position_risk(fr.account_id, SYMBOL),
            fr.client.open_orders(fr.account_id, SYMBOL),
        )
        held = any(p.position_amt != 0 for p in positions)
        protections = [
            o
            for o in orders
            if o.type in {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}
        ]
        checks.append(held == want_open)
        if want_protection is not None:
            checks.append(bool(protections) == want_protection)
    return all(checks)


async def _follower_protection_complete(app) -> bool:
    """Every live leg has one full bot protection under the exchange contract."""
    for fr in app.followers:
        positions, orders = await asyncio.gather(
            fr.client.position_risk(fr.account_id, SYMBOL),
            fr.client.open_orders(fr.account_id, SYMBOL),
        )
        for position in (p for p in positions if p.position_amt != 0):
            matching = [
                order
                for order in orders
                if order.type in {"STOP_MARKET", "STOP"}
                and order.client_order_id.startswith("ct")
                and order.position_side in {position.position_side, PosSide.BOTH}
            ]
            if len(matching) != 1:
                return False
            protection = matching[0]
            if protection.type == "STOP_MARKET":
                if not protection.close_position:
                    return False
            elif protection.orig_qty != abs(position.position_amt):
                return False
    return True


async def _wait_follower_state_with_recovery(
    app,
    main,
    *,
    want_open: bool,
    want_protection: bool | None,
    timeout: float,
    description: str,
    retry_stats: dict[str, int],
) -> None:
    """Wait for fan-out and use the real master snapshot if WS delivery lags."""
    started = time.monotonic()
    recovered = False
    while time.monotonic() - started < timeout:
        if await _follower_state(app, want_open, want_protection):
            return
        if not recovered and time.monotonic() - started >= 8.0:
            master_positions = await _position(main, "main")
            if (want_open and master_positions) or (not want_open and not master_positions):
                await app.projector.rebuild_full(main, "main")
                await app.reconciler.reconcile(
                    app.projector.state, reason=f"hybrid-load-authoritative:{description}"
                )
                retry_stats["authoritative_recoveries"] += 1
                recovered = True
        await asyncio.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {description}")


async def _cancel_all(client, account_id: str) -> None:
    for order in await client.open_orders(account_id):
        with contextlib.suppress(Exception):
            await client.cancel_order(
                account_id, order.symbol, orig_client_order_id=order.client_order_id
            )


async def _flatten_account(client, account_id: str, coid_prefix: str) -> None:
    await _cancel_all(client, account_id)
    for index, position in enumerate(await client.position_risk(account_id)):
        if position.position_amt == 0:
            continue
        side = Side.SELL if position.position_amt > 0 else Side.BUY
        reduce_only = position.position_side == PosSide.BOTH
        await client.place_order(
            account_id,
            PlaceOrderRequest(
                symbol=position.symbol,
                side=side,
                type="MARKET",
                quantity=abs(position.position_amt),
                reduce_only=reduce_only,
                position_side=position.position_side,
                new_order_resp_type="RESULT",
                new_client_order_id=f"{coid_prefix}-{index}-{int(time.time() * 1000)}"[:36],
            ),
        )
    await _cancel_all(client, account_id)


async def _cleanup(app, original_modes: dict[str, bool]) -> dict[str, object]:
    real = [fr for fr in app.followers if fr.execution_mode == "REAL"]
    shadow = [fr for fr in app.followers if fr.execution_mode == "SHADOW"]
    errors: list[str] = []
    for account_id in ["main"] + [fr.account_id for fr in real]:
        try:
            await _flatten_account(
                app.master_client
                if account_id == "main"
                else next(fr.client for fr in real if fr.account_id == account_id),
                account_id,
                "hl-clean",
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must continue all accounts
            errors.append(f"{account_id}: {exc}")
    for fr in shadow:
        try:
            await _flatten_account(fr.client, fr.account_id, "hs-clean")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fr.account_id}: {exc}")
    for fr in real:
        wanted = original_modes.get(fr.account_id)
        if wanted is None:
            continue
        try:
            current = await fr.client.get_position_mode(fr.account_id)
            if current != wanted:
                await fr.client.set_position_mode(fr.account_id, wanted)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fr.account_id}: restore mode: {exc}")
    if "main" in original_modes:
        try:
            current = await app.master_client.get_position_mode("main")
            if current != original_modes["main"]:
                await app.master_client.set_position_mode("main", original_modes["main"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"main: restore mode: {exc}")
    return {"errors": errors}


async def _audit(app) -> dict[str, object]:
    rows: dict[str, object] = {}
    for account_id, client in [("main", app.master_client)] + [
        (fr.account_id, fr.client) for fr in app.followers
    ]:
        positions = await client.position_risk(account_id)
        orders = await client.open_orders(account_id)
        rows[account_id] = {
            "mode": "SHADOW"
            if any(
                fr.account_id == account_id and fr.execution_mode == "SHADOW"
                for fr in app.followers
            )
            else "REAL",
            "positions": [
                {
                    "symbol": p.symbol,
                    "position_side": p.position_side.value,
                    "qty": str(p.position_amt),
                }
                for p in positions
                if p.position_amt != 0
            ],
            "open_orders": [o.client_order_id for o in orders],
        }
    return rows


async def run(args: argparse.Namespace) -> dict[str, object]:
    cfg = Settings()
    if "testnet" not in cfg.rest_base_url.lower():
        raise RuntimeError(f"load run is testnet-only, got {cfg.rest_base_url!r}")
    if args.followers <= 0 or args.cycles <= 0 or args.quantity <= 0:
        raise ValueError("followers, cycles and quantity must be positive")
    source_db = args.source_db or cfg.db_path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_db = args.run_db or Path("data") / f"hybrid-load-{stamp}.sqlite3"
    report_path = args.report or run_db.with_suffix(".json")
    await _clone_real_accounts(cfg, source_db, run_db)
    real_ids = await _seed_shadow_followers(run_db, args.followers)
    original_modes = await _preflight(cfg, run_db)
    del real_ids

    run_cfg = cfg.model_copy(
        update={
            "db_path": run_db,
            "shadow_accounts_enabled": True,
            "shadow_auto_trigger_conditionals": False,
            "log_level": "WARNING",
            "follower_snapshot_symbols": [SYMBOL],
        }
    )
    from copytradeaster.app import App

    app = App(run_cfg)
    # Per-command console notifications make 30-way runs unreadable; the JSON
    # progress and durable journal remain the authoritative load evidence.
    app.coordinator._on_notify = None
    app.executor._on_confirmed = None
    started = True
    cycles: list[dict[str, object]] = []
    cleanup: dict[str, object] = {"errors": ["not-run"]}
    result: dict[str, object] = {
        "run_db": str(run_db),
        "cycles_requested": args.cycles,
        "cycles_completed": 0,
    }
    try:
        await app.start()
        real_follower_ids = [fr.account_id for fr in app.followers if fr.execution_mode == "REAL"]
        shadow_follower_ids = [
            fr.account_id for fr in app.followers if fr.execution_mode == "SHADOW"
        ]
        if len(app.followers) != args.followers:
            raise RuntimeError(
                f"follower count mismatch: expected {args.followers}, got {len(app.followers)}"
            )
        info = await app.master_client.exchange_info(force=True)
        filters = info.tradable_filters()[SYMBOL]
        quantity = _snap_down(args.quantity, filters.step_size)
        if quantity < filters.min_qty or quantity * Decimal("65000") < filters.min_notional:
            raise RuntimeError(f"quantity {quantity} does not pass {SYMBOL} filters")
        hedge = await app.master_client.get_position_mode("main")
        position_side = PosSide.LONG if hedge else PosSide.BOTH
        main = app.master_client
        master_retry_stats = {
            "unknown_outcomes": 0,
            "resolved_by_query": 0,
            "resolved_by_position": 0,
            "partial_residuals": 0,
            "retries": 0,
            "authoritative_recoveries": 0,
        }

        for cycle in range(1, args.cycles + 1):
            started_at = time.monotonic()
            open_coid = f"hl-open-{cycle}-{int(time.time() * 1000)}"[:36]
            stop_coid = f"hl-stop-{cycle}-{int(time.time() * 1000)}"[:36]
            close_coid = f"hl-close-{cycle}-{int(time.time() * 1000)}"[:36]
            await _submit_master_order(
                main,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.BUY,
                    type="MARKET",
                    quantity=quantity,
                    position_side=position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=open_coid,
                ),
                master_retry_stats,
            )
            await _wait_until(
                lambda: _position(main, "main"),
                args.timeout,
                f"main open cycle {cycle}",
            )
            position = (await _position(main, "main"))[0]
            stop_price = _snap_down(position.entry_price - args.stop_distance, filters.tick_size)
            live_stop_coid = await _submit_master_order(
                main,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL,
                    type="STOP_MARKET",
                    quantity=abs(position.position_amt),
                    stop_price=stop_price,
                    reduce_only=not hedge,
                    position_side=position_side,
                    working_type="MARK_PRICE",
                    new_client_order_id=stop_coid,
                ),
                master_retry_stats,
            )
            await _wait_follower_state_with_recovery(
                app,
                main,
                want_open=True,
                want_protection=True,
                timeout=args.timeout,
                description=f"follower open/protection cycle {cycle}",
                retry_stats=master_retry_stats,
            )
            if not await _follower_protection_complete(app):
                raise RuntimeError(f"incomplete follower protection after open, cycle {cycle}")

            # Corrective profile: real partial reduce and grow while the master
            # trigger remains unchanged. Followers must stay protected after
            # both transitions (closePosition or exact fixed quantity).
            resize_qty = _snap_down(abs(position.position_amt) / 2, filters.step_size)
            if resize_qty < filters.min_qty:
                raise RuntimeError(
                    f"quantity {position.position_amt} is too small for resize phase"
                )
            await _submit_master_order(
                main,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL,
                    type="MARKET",
                    quantity=resize_qty,
                    reduce_only=not hedge,
                    position_side=position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"hl-reduce-{cycle}-{time.time_ns()}"[:36],
                ),
                master_retry_stats,
            )
            await _wait_follower_state_with_recovery(
                app,
                main,
                want_open=True,
                want_protection=True,
                timeout=args.timeout,
                description=f"follower reduce/protection cycle {cycle}",
                retry_stats=master_retry_stats,
            )
            if not await _follower_protection_complete(app):
                raise RuntimeError(f"incomplete follower protection after reduce, cycle {cycle}")

            await _submit_master_order(
                main,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.BUY,
                    type="MARKET",
                    quantity=resize_qty,
                    position_side=position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"hl-grow-{cycle}-{time.time_ns()}"[:36],
                ),
                master_retry_stats,
            )
            await _wait_follower_state_with_recovery(
                app,
                main,
                want_open=True,
                want_protection=True,
                timeout=args.timeout,
                description=f"follower grow/protection cycle {cycle}",
                retry_stats=master_retry_stats,
            )
            if not await _follower_protection_complete(app):
                raise RuntimeError(f"incomplete follower protection after grow, cycle {cycle}")

            position = (await _position(main, "main"))[0]
            stop_orders = await main.open_orders("main", SYMBOL)
            live_stop = next((o for o in stop_orders if o.client_order_id == live_stop_coid), None)
            if live_stop is None:
                raise RuntimeError(f"master stop disappeared before cancel, cycle {cycle}")
            await _cancel_master_order(main, SYMBOL, live_stop.client_order_id, master_retry_stats)
            # Existing follower protection is intentionally retained when the
            # master removes its own stop (safety-first policy). The next real
            # master close is the authoritative transition to flat; waiting
            # for every background post-fill task here can otherwise delay the
            # master close itself.
            await asyncio.sleep(0.5)
            await _submit_master_order(
                main,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL,
                    type="MARKET",
                    quantity=abs(position.position_amt),
                    reduce_only=not hedge,
                    position_side=position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=close_coid,
                ),
                master_retry_stats,
            )
            await _wait_follower_state_with_recovery(
                app,
                main,
                want_open=False,
                want_protection=False,
                timeout=args.timeout,
                description=f"follower flat cycle {cycle}",
                retry_stats=master_retry_stats,
            )
            if app._ingest_dropped:
                raise RuntimeError(f"master ingest dropped {app._ingest_dropped} events")
            elapsed = time.monotonic() - started_at
            cycles.append({"cycle": cycle, "elapsed_s": round(elapsed, 3)})
            if cycle % 10 == 0 or cycle == 1:
                print(
                    json.dumps(
                        {
                            "progress": f"{cycle}/{args.cycles}",
                            "elapsed_s": round(elapsed, 3),
                            "real_followers": len(real_follower_ids),
                            "shadow_followers": len(shadow_follower_ids),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

        command_rows = await app.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM execution_commands GROUP BY status"
        )
        command_counts = {str(row["status"]): int(row["n"]) for row in command_rows}
        final_audit = await _audit(app)
        non_confirmed = {
            status: count for status, count in command_counts.items() if status != "CONFIRMED"
        }
        if non_confirmed:
            raise RuntimeError(f"non-confirmed execution journal rows: {non_confirmed}")
        dirty_accounts = {
            account_id: state
            for account_id, state in final_audit.items()
            if state["positions"] or state["open_orders"]  # type: ignore[index]
        }
        if dirty_accounts:
            raise RuntimeError(f"pre-cleanup audit is not flat/order-free: {dirty_accounts}")
        shadow_metrics = dict(app.shadow_exchange.metrics)
        elapsed_values = sorted(float(cycle["elapsed_s"]) for cycle in cycles)

        def percentile(fraction: float) -> float:
            index = min(int((len(elapsed_values) - 1) * fraction), len(elapsed_values) - 1)
            return elapsed_values[index]

        result = {
            "started_at": datetime.now(UTC).isoformat(),
            "rest_base_url": run_cfg.rest_base_url,
            "run_db": str(run_db),
            "followers_requested": args.followers,
            "followers_loaded": len(app.followers),
            "real_followers": real_follower_ids,
            "shadow_followers": shadow_follower_ids,
            "cycles_requested": args.cycles,
            "cycles_completed": len(cycles),
            "cycles": cycles,
            "cycle_latency_s": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "max": max(elapsed_values),
            },
            "command_counts_before_cleanup": command_counts,
            "master_retry_stats": master_retry_stats,
            "shadow_signed_requests": app.shadow_exchange.signed_requests,
            "shadow_metrics": shadow_metrics,
            "ingest_dropped": app._ingest_dropped,
            "audit_before_cleanup": final_audit,
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["cycles_completed"] = len(cycles)
        result["cycles"] = cycles
        result["master_retry_stats"] = locals().get("master_retry_stats", {})
        raise
    finally:
        if started:
            cleanup = await _cleanup(app, original_modes)
            try:
                result["cleanup"] = cleanup  # type: ignore[name-defined]
                result["audit_after_cleanup"] = await _audit(app)  # type: ignore[name-defined]
            except Exception as exc:  # noqa: BLE001
                cleanup.setdefault("errors", []).append(f"final audit: {exc}")
            await app.stop()
        else:
            result = {
                "run_db": str(run_db),
                "cycles_requested": args.cycles,
                "cycles_completed": 0,
                "cleanup": cleanup,
            }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


async def _main() -> int:
    args = _parser().parse_args()
    result = await run(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not result.get("cleanup", {}).get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
