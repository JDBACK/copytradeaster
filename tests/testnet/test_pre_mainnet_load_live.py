"""Package F: hybrid REAL+SHADOW load gate (pre-mainnet, REAL testnet only).

3-5 REAL followers (main + scratch-seeded copies) plus 10 SHADOW slots on
:class:`ShadowExchange` run through the shared coordinator / planner /
executor / journal with the production snapshot profile (the UNMODIFIED
configured default scope — the scoped whitelist+held+episodes profile per
the E1 design, NOT the old hybrid script's single-symbol narrowing).
The campaign drives >=100 master
open/reduce/grow/close cycles with real master orders + follower protection,
with mid-campaign fault injection (graceful restart, SIGTERM of the App
process, SIGKILL of a worker mid-cycle) and a final flat/order-free signed
audit on every REAL account.
Mechanics (cycle shape, unknown-outcome master recovery, cleanup/audit) reuse
``scripts/hybrid_load_test.py``; the SIGKILL worker reuses the A5 pattern
from ``tests/testnet/test_pre_mainnet_unknown_live.py`` adapted to this load
run. This file owns NO production code and never touches the prod DB: every
run seeds a unique ``tmp_path`` database from a prod copy.

Coverage map (phase -> load-gate requirement row):
- P0 setup/scratch/shadow seed + preflight ......... R1 (REAL testnet only,
  unique run DB, flat/order-free gate, balance gate with measured numbers),
  R2 (3-5 REAL + 10 SHADOW topology, production snapshot profile).
- P1/P3/P5/P7 master open/reduce/grow/close cycles . R3 (>=100 real-master
  cycles, every leg protected, command_key uniqueness / no duplicate
  economic mutations).
- P2 graceful App restart .......................... R4 (stop->start->resume
  converges with no duplicate fills).
- P4 SIGTERM of the App process .................... R5 (process death with
  boot recovery, same command_keys, single economic fill each).
- P6 SIGKILL of a worker mid-cycle ................. R5 (A5 pattern: UNKNOWN
  window death, boot resolver replays the same command_key, one fill).
- P8 journal/metrics/signed audit .................. R6 (zero
  SENDING/UNKNOWN/FAILED-terminal rows), R7 (metrics_snapshot percentiles +
  weight/min reported), R8 (final flat/order-free signed audit on ALL real
  accounts).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sqlite3
import sys
import time
from dataclasses import replace
from decimal import ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.app import App
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import (
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PosSide,
    RiskPolicy,
    Side,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
)


def _settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        db_path=tmp_path / "hybrid-load.sqlite3",
        log_path=tmp_path / "hybrid-load.log",
        shadow_accounts_enabled=True,
        shadow_auto_trigger_conditionals=False,
    )
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    if settings.dry_run:
        pytest.fail("load gate refuses to run in dry_run: orders would be fake")
    # Production-equivalent snapshot profile: the UNMODIFIED configured
    # default scope (scoped whitelist+held+episodes per the E1 design).
    # Constructing a reference Settings() reads the same environment, so any
    # divergence here means the caller narrowed the scope (e.g. the old
    # hybrid script forced a single-symbol snapshot) — refuse that.
    reference = Settings()
    assert settings.follower_snapshot_symbols == reference.follower_snapshot_symbols, (
        "load gate requires the unmodified production snapshot scope, "
        f"got {settings.follower_snapshot_symbols!r} vs configured default "
        f"{reference.follower_snapshot_symbols!r}"
    )
    assert settings.follower_snapshot_symbols != [SYMBOL], (
        "load gate refuses single-symbol snapshot narrowing: production-equivalent scope required"
    )
    return settings


SYMBOL = "BTCUSDT"
# Master leg sized so the resize half snaps clean: 0.004 / 2 = 0.002 stays a
# whole step under the conservative _snap_down floor (0.002 halved to 0.001
# floored to zero and killed cycle 1). ~$320 notional is fine on testnet;
# follower sizing is risk-based off the master leg, unaffected.
QTY = D("0.004")
# Stop sits 1500 below entry: 500 was hunted within a minute on current
# testnet volatility (79k BTC whipping ±500 in seconds -> driver/engine
# open->close->open churn), while the B/D matrices proved 1200-1800 stable
# on this market. If churn persists at 1500 it exonerates volatility and
# points at the engine — the rerun discriminates. Nothing else changes.
STOP_DISTANCE = D("1500")
N_REAL_FOLLOWERS = 3
# Shadow fleet capped by the MEASURED testnet weight envelope (load12
# forensics): ~103 followers x ~7 weight each ~= 721+ per sweep against a
# 40/s testnet budget -> sweeps took 17-37s (measured) vs the 5s
# master-freshness window -> perpetual stale -> drift->dirty->boundary
# (3 heavy passes) -> slower -> more stale: a congestion-collapse loop.
# Ceiling ~= budget x window / cost ~= 40x5/7 ~= 28 absolute, minus orders
# overhead and boundary storms -> v1 cap 10 holds with margin. Mainnet
# budgets are higher so the cap can grow with a measured mainnet envelope;
# no code change needed for that, only this number.
N_SHADOW = 10
STEP_TIMEOUT = 90.0
RESTART_AT = 30
SIGTERM_AT = 60
SIGKILL_AT = 80
CYCLES = 100
BALANCE_FLOOR_USDT = D("10")
PROD_DB = Path("data/copytrade.sqlite3")
_SEED_TABLES = ("accounts", "credentials", "copy_settings", "agent_validations")


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    src_tables = {
        row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    dst_tables = {
        row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table not in src_tables or table not in dst_tables:
        return 0
    src_cols = [row[1] for row in src.execute(f"PRAGMA table_info({table})")]
    dst_cols = [row[1] for row in dst.execute(f"PRAGMA table_info({table})")]
    cols = [col for col in src_cols if col in dst_cols]
    if not cols:
        return 0
    names = ", ".join(f'"{col}"' for col in cols)
    placeholders = ", ".join("?" for _ in cols)
    rows = src.execute(f"SELECT {names} FROM {table}").fetchall()
    if rows:
        dst.executemany(f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({placeholders})', rows)
        dst.commit()
    return len(rows)


def _seed_scratch(db_path: Path, max_followers: int = N_REAL_FOLLOWERS) -> list[str]:
    """Copy REAL credentials into a fresh scratch DB; keep main + K followers.

    The ciphertext blobs decrypt under the same CT_ENCRYPTION_KEY, so the
    production App boots against REAL keys without ever opening the prod file.
    """
    if not PROD_DB.exists():
        pytest.skip("production DB data/copytrade.sqlite3 is required to seed live credentials")
    src = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    try:
        for table in _SEED_TABLES:
            _copy_table(src, dst, table)
        acct_cols = [row[1] for row in dst.execute("PRAGMA table_info(accounts)")]
        if "execution_mode" in acct_cols:
            mode_filter = "AND COALESCE(execution_mode, 'REAL') = 'REAL'"
        else:  # pragma: no cover - legacy schema without execution_mode
            mode_filter = ""
        rows = dst.execute(
            f"SELECT id FROM accounts WHERE role = 'follower' AND enabled = 1 {mode_filter} "
            "ORDER BY id"
        ).fetchall()
        keep = [str(row[0]) for row in rows[:max_followers]]
        if not keep:
            pytest.skip("at least one imported REAL follower is required")
        wanted = ",".join("?" for _ in ("main", *keep))
        dst.execute(f"UPDATE accounts SET enabled = 0 WHERE id NOT IN ({wanted})", ("main", *keep))
        dst.commit()
        return keep
    finally:
        dst.close()
        src.close()


async def _seed_shadow_followers(db_path: Path, count: int = N_SHADOW) -> list[str]:
    """Add SHADOW follower slots (no credentials, no HTTP) to the scratch DB."""
    db = Database(db_path)
    await db.connect()
    try:
        await db.migrate()
        accounts = AccountRepo(db)
        policies = SettingsRepo(db)
        ids: list[str] = []
        for index in range(1, count + 1):
            account_id = f"shadow-{index:03d}"
            await accounts.upsert(
                account_id,
                "follower",
                account_id,
                f"shadow://{account_id}",
                execution_mode="SHADOW",
            )
            await policies.upsert_policy(
                RiskPolicy(
                    risk_percent=D("0.01"),
                    max_leverage=20,
                    max_entry_deviation_pct=D("0.01"),
                ),
                account_id,
            )
            ids.append(account_id)
        return ids
    finally:
        await db.close()


async def _prepare_scratch(tmp_path: Path) -> tuple[Settings, list[str], list[str]]:
    settings = _settings_for(tmp_path)
    seed_db = Database(settings.db_path)
    await seed_db.connect()
    try:
        await seed_db.migrate()
    finally:
        await seed_db.close()
    followers = _seed_scratch(Path(str(settings.db_path)))
    shadows = await _seed_shadow_followers(Path(str(settings.db_path)))
    return settings, followers, shadows


# --- live trading helpers (all REAL signed REST) --------------------------------


async def _wait_until(predicate, timeout: float, what: str, interval: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


async def _ready_flag(app: App) -> bool:
    return await app.settings.get_flag("ready") == "true"


async def _boot(settings: Settings) -> App:
    app = App(settings)
    await app.start()
    assert app._ready and app._stream_connect_count >= 1
    assert await _ready_flag(app)
    await _seed_shadow_leverage(app)
    return app


async def _seed_shadow_leverage(app: App) -> None:
    """Replicate the flat-leg leverage report a faithful double would emit.

    ShadowExchange.position_risk omits flat legs while real Aster returns
    them WITH leverage, so Coordinator._snapshot never observe()s shadow
    leverage and the control plane would gate shadow entries
    LEVERAGE_NOT_READY forever. The double is shared and pinned by offline
    asserts, so the load gate seeds the control plane exactly as _snapshot
    would have (coordinator.py observe call): set the exchange-side leverage
    to the catalog candidate, then fold the same authoritative value into
    the confirmed map. Called after every (re)boot because LeverageManager
    state is RAM-only. The candidate is read from the manager's catalog
    (never hardcoded): observe() only stamps the current generation when
    the value agrees, so a wrong value would stay unready.
    """
    await app.leverage.refresh_catalog()
    entry = app.leverage._candidate.get(SYMBOL)
    assert entry is not None, (
        f"leverage catalog has no candidate for {SYMBOL} after refresh; "
        "refusing to guess shadow leverage"
    )
    candidate = int(entry.leverage)
    assert candidate > 0, f"leverage catalog candidate for {SYMBOL} is not positive: {candidate!r}"
    shadow_slots = [fr.account_id for fr in app.followers if fr.execution_mode == "SHADOW"]
    assert shadow_slots, "no SHADOW followers to seed leverage for"
    for slot in shadow_slots:
        await app.shadow_exchange.set_leverage(slot, SYMBOL, candidate)
    for slot in shadow_slots:
        app.leverage.observe(slot, {SYMBOL: candidate})
    unready = [slot for slot in shadow_slots if not app.leverage.is_ready(slot, SYMBOL)]
    assert not unready, (
        f"shadow leverage still not ready after seeding candidate {candidate} "
        f"for {SYMBOL}: {unready[:5]} (of {len(unready)})"
    )


async def _position_side(client: AsterClient) -> PosSide:
    hedge = await client.get_position_mode("main")
    return PosSide.LONG if hedge else PosSide.BOTH


async def _symbol_qty(client: AsterClient, account_id: str) -> D:
    total = D("0")
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL:
            total += abs(position.position_amt)
    return total


async def _has_position(client: AsterClient, account_id: str) -> bool:
    return await _symbol_qty(client, account_id) != 0


def _snap_down(value: D, tick: D) -> D:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _unknown_order_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, AsterError):
        return False
    if exc.code in {-1000, -1006, -1007} or exc.http_status in {503, 504}:
        return True
    message = exc.msg.lower()
    return exc.code == 0 and ("timeout" in message or "timed out" in message)


async def _master_position_abs(client: AsterClient, position_side: PosSide) -> D:
    positions = await client.position_risk("main", SYMBOL)
    return sum(
        (abs(p.position_amt) for p in positions if p.position_side == position_side),
        D(0),
    )


async def _submit_master_order(
    client: AsterClient,
    request: PlaceOrderRequest,
    retry_stats: dict[str, int],
) -> str:
    """Master submit with outcome-unknown recovery (no duplicate economics)."""
    current = request
    before_abs = await _master_position_abs(client, request.position_side)
    requested_abs = abs(request.quantity or D(0))
    target_abs = (
        before_abs + requested_abs
        if request.side == Side.BUY
        else max(before_abs - requested_abs, D(0))
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
                    return found.client_order_id
                if current.type != "MARKET":
                    continue
                actual_abs = await _master_position_abs(client, current.position_side)
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
    client: AsterClient, client_order_id: str, retry_stats: dict[str, int]
) -> None:
    for attempt in range(3):
        try:
            await client.cancel_order("main", SYMBOL, orig_client_order_id=client_order_id)
            return
        except AsterError as exc:
            if exc.code == -2011:
                return
            if not _unknown_order_error(exc):
                raise
            retry_stats["unknown_outcomes"] += 1
            await asyncio.sleep(0.5 * (attempt + 1))
            found = await client.query_order("main", SYMBOL, orig_client_order_id=client_order_id)
            if found is None or found.status in {"CANCELED", "EXPIRED", "FILLED"}:
                retry_stats["resolved_by_query"] += 1
                return
            retry_stats["retries"] += 1
    raise RuntimeError(f"could not cancel master order {client_order_id}")


async def _follower_state(app: App, want_open: bool, want_protection: bool | None = None) -> bool:
    async def wait_for_generation(fr) -> None:
        await app.executor.wait_until_idle(fr.account_id)

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


async def _protection_complete(app: App) -> bool:
    """Every live leg carries one full bot protection under the contract."""
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
    app: App,
    *,
    want_open: bool,
    want_protection: bool | None,
    timeout: float,
    description: str,
    retry_stats: dict[str, int],
) -> None:
    """Wait for fan-out; fall back to an authoritative master resnapshot."""
    started = time.monotonic()
    recovered = False
    while time.monotonic() - started < timeout:
        if await _follower_state(app, want_open, want_protection):
            return
        if not recovered and time.monotonic() - started >= 8.0:
            master_positions = [
                p
                for p in await app.master_client.position_risk("main", SYMBOL)
                if p.position_amt != 0
            ]
            if (want_open and master_positions) or (not want_open and not master_positions):
                await app.projector.rebuild_full(app.master_client, "main")
                await app.reconciler.reconcile(
                    app.projector.state, reason=f"hybrid-load-authoritative:{description}"
                )
                retry_stats["authoritative_recoveries"] += 1
                recovered = True
        await asyncio.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {description}")


async def _run_cycle(
    app: App,
    cycle: int,
    tag: str,
    quantity: D,
    tick_size: D,
    step_size: D,
    min_qty: D,
    position_side: PosSide,
    hedge: bool,
    retry_stats: dict[str, int],
) -> None:
    """One master open/reduce/grow/close cycle; followers stay protected."""
    main = app.master_client
    open_coid = f"hl-{tag}-o{cycle}-{time.time_ns() % 10**9:09d}"[:36]
    stop_coid = f"hl-{tag}-s{cycle}-{time.time_ns() % 10**9:09d}"[:36]
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
        retry_stats,
    )
    await _wait_until(lambda: _has_position(main, "main"), STEP_TIMEOUT, f"main open cycle {cycle}")
    (position,) = [p for p in await main.position_risk("main", SYMBOL) if p.position_amt != 0]
    stop_price = _snap_down(position.entry_price - STOP_DISTANCE, tick_size)
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
        retry_stats,
    )
    await _wait_follower_state_with_recovery(
        app,
        want_open=True,
        want_protection=True,
        timeout=STEP_TIMEOUT,
        description=f"follower open/protection cycle {cycle}",
        retry_stats=retry_stats,
    )
    assert await _protection_complete(app), (
        f"incomplete follower protection after open, cycle {cycle}"
    )

    # QUANTITY snap: must use step_size (qty increment). tick_size is the
    # PRICE increment and flooring a 0.002 half by 0.1 floors to zero.
    resize_qty = _snap_down(abs(position.position_amt) / 2, step_size)
    assert resize_qty >= min_qty, (
        f"resize half {resize_qty} (from {position.position_amt}) floored below min_qty "
        f"{min_qty}: cycle {cycle} would submit dust"
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
            new_client_order_id=f"hl-{tag}-r{cycle}-{time.time_ns() % 10**9:09d}"[:36],
        ),
        retry_stats,
    )
    await _wait_follower_state_with_recovery(
        app,
        want_open=True,
        want_protection=True,
        timeout=STEP_TIMEOUT,
        description=f"follower reduce/protection cycle {cycle}",
        retry_stats=retry_stats,
    )
    assert await _protection_complete(app), (
        f"incomplete follower protection after reduce, cycle {cycle}"
    )

    await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=resize_qty,
            position_side=position_side,
            new_order_resp_type="RESULT",
            new_client_order_id=f"hl-{tag}-g{cycle}-{time.time_ns() % 10**9:09d}"[:36],
        ),
        retry_stats,
    )
    await _wait_follower_state_with_recovery(
        app,
        want_open=True,
        want_protection=True,
        timeout=STEP_TIMEOUT,
        description=f"follower grow/protection cycle {cycle}",
        retry_stats=retry_stats,
    )
    assert await _protection_complete(app), (
        f"incomplete follower protection after grow, cycle {cycle}"
    )

    (position,) = [p for p in await main.position_risk("main", SYMBOL) if p.position_amt != 0]
    stop_orders = await main.open_orders("main", SYMBOL)
    live_stop = next((o for o in stop_orders if o.client_order_id == live_stop_coid), None)
    assert live_stop is not None, f"master stop disappeared before cancel, cycle {cycle}"
    await _cancel_master_order(main, live_stop.client_order_id, retry_stats)
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
            new_client_order_id=f"hl-{tag}-c{cycle}-{time.time_ns() % 10**9:09d}"[:36],
        ),
        retry_stats,
    )
    await _wait_follower_state_with_recovery(
        app,
        want_open=False,
        want_protection=False,
        timeout=STEP_TIMEOUT,
        description=f"follower flat cycle {cycle}",
        retry_stats=retry_stats,
    )
    assert not app._ingest_dropped, f"master ingest dropped {app._ingest_dropped} events"


# --- fault-injection workers (P4/P6, A5 pattern adapted to load) ------------------


async def _run_app_process_worker(db_path_str: str) -> None:
    """Boot the load App from the shared run DB, then idle until signalled."""
    settings = Settings(
        db_path=Path(db_path_str),
        log_path=Path(db_path_str).with_suffix(".worker.log"),
        shadow_accounts_enabled=True,
        shadow_auto_trigger_conditionals=False,
    )
    app = App(settings)
    await app.start()
    await _seed_shadow_leverage(app)
    print("LOAD_APP_READY", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        with contextlib.suppress(Exception):
            await app.stop()


async def _run_kill_worker(db_path_str: str, coid: str) -> None:
    """Place one real MARKET via the run journal, then hang inside UNKNOWN."""
    from copytradeaster.credentials.service import SqliteCredentials
    from copytradeaster.trading.executor import Executor

    worker_settings = Settings()
    if "testnet" not in worker_settings.rest_base_url.lower():
        raise RuntimeError("load SIGKILL worker is restricted to testnet")
    source_db = Database(worker_settings.db_path)
    await source_db.connect()
    try:
        worker_creds = SqliteCredentials(source_db, worker_settings.encryption_key_bytes())
        await worker_creds.load()
        if "main" not in worker_creds.account_ids():
            raise RuntimeError("main credentials are required")
        http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        from copytradeaster.aster.rate_limits import RateLimits

        limiter = RateLimits(
            worker_settings.weight_limit_per_min,
            worker_settings.orders_limit_per_min,
            worker_settings.orders_limit_per_10s,
        )
        real = AsterClient(
            http,
            worker_settings.rest_base_url,
            limiter,
            worker_creds,
            worker_creds,
            NonceStore(source_db),
        )
        journal = Database(Path(db_path_str))
        await journal.connect()
        await journal.migrate()
        hedge = await real.get_position_mode("main")

        class HangAfterAcceptedClient:
            async def position_risk(self, account_id, symbol=None):
                return await real.position_risk(account_id, symbol)

            async def place_order(self, account_id, req):
                await real.place_order(account_id, req)
                await asyncio.sleep(3600)
                raise AssertionError("kill worker survived the UNKNOWN window")

        executor = Executor(
            lambda _account_id: HangAfterAcceptedClient(),
            CommandRepo(journal),
            OrderLinkRepo(journal),
            {},
            settings=SettingsRepo(journal),
        )
        payload = {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": format(QTY, "f"),
            "newOrderRespType": "RESULT",
        }
        if hedge:
            payload["positionSide"] = PosSide.LONG.value
        action = ExecutionAction(
            kind=ActionKind.MARKET_OPEN,
            symbol=SYMBOL,
            payload=payload,
            client_order_id=coid,
            replaces_client_order_id=None,
            priority=1,
            revision=5,
        )
        await executor.submit(ExecutionPlan(account_id="main", trade_id=None, actions=[action]))
    finally:
        await source_db.close()
    raise AssertionError("kill worker survived accepted MARKET")


# --- flatten / signed audit (P8) ---------------------------------------------------


async def _flatten_account(client: AsterClient, account_id: str, tag: str) -> None:
    for order in await client.open_orders(account_id, SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order(
                account_id, SYMBOL, orig_client_order_id=order.client_order_id
            )
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL and position.position_amt != 0:
            with contextlib.suppress(Exception):
                await client.place_order(
                    account_id,
                    PlaceOrderRequest(
                        symbol=SYMBOL,
                        side=Side.SELL if position.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(position.position_amt),
                        reduce_only=position.position_side.value == "BOTH",
                        position_side=position.position_side,
                        new_client_order_id=f"{tag}-{account_id}-{time.time_ns() % 10**9:09d}"[:36],
                        new_order_resp_type="RESULT",
                    ),
                )


async def _assert_flat_signed(client: AsterClient, account_ids: list[str]) -> None:
    """Independent signed audit: every REAL account flat, zero open orders."""
    for account_id in account_ids:
        positions = await client.position_risk(account_id, SYMBOL)
        nonzero = [p for p in positions if p.symbol == SYMBOL and p.position_amt != 0]
        assert not nonzero, (
            f"{account_id} not flat: {[(str(p.position_side), p.position_amt) for p in nonzero]}"
        )
        orders = await client.open_orders(account_id, SYMBOL)
        assert not orders, (
            f"{account_id} has leftover orders: {[(o.client_order_id, o.type) for o in orders]}"
        )


def _fmt_metrics(snapshot: dict) -> str:
    def pct(series: dict) -> str:
        return (
            f"count={series.get('count')} p50={series.get('p50')} "
            f"p95={series.get('p95')} p99={series.get('p99')} max={series.get('max')}"
        )

    return (
        f"reconcile_duration_s[{pct(snapshot['reconcile_duration_s'])}] "
        f"follower_duration_s[{pct(snapshot['follower_duration_s'])}] "
        f"request_weight_per_min={snapshot['request_weight_per_min']}"
    )


# --- the gate ---------------------------------------------------------------------


async def test_hybrid_load_real_plus_shadow(tmp_path):
    """F: 100 master cycles across 3-5 REAL + 10 SHADOW with fault injection."""
    settings, followers, shadows = await _prepare_scratch(tmp_path)
    assert 3 <= len(followers) <= 5, (
        f"expected 3-5 REAL followers, got {len(followers)}: {followers}"
    )
    assert len(shadows) == N_SHADOW, f"expected {N_SHADOW} SHADOW slots, got {len(shadows)}"
    retry_stats: dict[str, int] = {
        "unknown_outcomes": 0,
        "resolved_by_query": 0,
        "resolved_by_position": 0,
        "partial_residuals": 0,
        "retries": 0,
        "authoritative_recoveries": 0,
    }
    phase_metrics: list[str] = []
    # Resumable gate: CT_LOAD_PHASES subset of
    # a,restart,b,sigterm,c,sigkill,d,audit (default all); CT_LOAD_SCALE
    # multiplies cycle boundaries (default 1.0 = 30/60/80/100). Each phase
    # block is self-contained given the booted App; restart phases compare
    # journal/fills within their own window, the audit flattens first.
    _phases = {p.strip() for p in os.getenv("CT_LOAD_PHASES", "all").split(",")}

    def _want(*names: str) -> bool:
        return "all" in _phases or any(n in _phases for n in names)

    _scale = float(os.getenv("CT_LOAD_SCALE", "1.0"))
    _restart_at = max(2, round(RESTART_AT * _scale))
    _sigterm_at = max(_restart_at + 1, round(SIGTERM_AT * _scale))
    _sigkill_at = max(_sigterm_at + 1, round(SIGKILL_AT * _scale))
    _cycles = max(_sigkill_at + 1, round(CYCLES * _scale))
    db_path_str = str(settings.db_path)  # shared by restart/kill workers in any phase subset

    app = await _boot(settings)
    real_ids = [fr.account_id for fr in app.followers if fr.execution_mode == "REAL"]
    shadow_ids = [fr.account_id for fr in app.followers if fr.execution_mode == "SHADOW"]
    assert len(app.followers) == len(real_ids) + len(shadow_ids) == len(followers) + N_SHADOW, (
        f"follower topology mismatch: {len(app.followers)} loaded, "
        f"{len(real_ids)} REAL, {len(shadow_ids)} SHADOW"
    )
    audit_http: httpx.AsyncClient | None = None
    try:
        # P0: preflight — every REAL account flat/order-free; measured balance gate.
        for account_id in ["main", *real_ids]:
            positions = await app.master_client.position_risk(account_id, SYMBOL)
            orders = await app.master_client.open_orders(account_id, SYMBOL)
            assert not [p for p in positions if p.position_amt != 0] and not orders, (
                f"preflight refused: {account_id} is not flat/order-free "
                f"(positions={[str(p.position_amt) for p in positions]}, orders={len(orders)}); "
                "flatten testnet accounts before the load gate"
            )
        balances = await app.master_client.balance("main")
        usdt = next((b.balance for b in balances if b.asset == "USDT"), D("0"))
        if usdt < BALANCE_FLOOR_USDT:
            pytest.skip(
                f"insufficient testnet balance for the load gate: measured {usdt} USDT "
                f"< {BALANCE_FLOOR_USDT} USDT floor on main"
            )
        info = await app.master_client.exchange_info(force=True)
        filters = info.tradable_filters()[SYMBOL]
        quantity = _snap_down(QTY, filters.step_size)
        assert quantity >= filters.min_qty, (
            f"quantity {quantity} below min_qty {filters.min_qty} (step {filters.step_size})"
        )
        position_side = await _position_side(app.master_client)
        hedge = position_side == PosSide.LONG

        async def run_range(start: int, stop: int, tag: str) -> None:
            for cycle in range(start, stop + 1):
                await _run_cycle(
                    app,
                    cycle,
                    tag,
                    quantity,
                    filters.tick_size,
                    filters.step_size,
                    filters.min_qty,
                    position_side,
                    hedge,
                    retry_stats,
                )

        # P1: first block of cycles.
        if _want("a"):
            await run_range(1, _restart_at, "a")

        # P2: graceful restart — stop -> start -> resume converges, no dup.
        if _want("restart"):
            keys_before = {
                str(row["command_key"])
                for row in await app.db.fetchall("SELECT command_key FROM execution_commands")
            }
            fills_before = len(await app.master_client.all_orders("main", SYMBOL, limit=1000))
            await app.stop()
            app = await _boot(settings)
            await _wait_follower_state_with_recovery(
                app,
                want_open=False,
                want_protection=False,
                timeout=STEP_TIMEOUT,
                description="resume converges flat after graceful restart",
                retry_stats=retry_stats,
            )
            in_flight = await app.db.fetchall(
                "SELECT command_key FROM execution_commands "
                "WHERE status IN ('UNKNOWN','SENDING','SUBMITTED')"
            )
            assert not in_flight, (
                f"in-flight rows survived graceful restart: {[dict(r) for r in in_flight]}"
            )
            keys_after = {
                str(row["command_key"])
                for row in await app.db.fetchall("SELECT command_key FROM execution_commands")
            }
            assert keys_after == keys_before, (
                "graceful restart must replay no command (no duplicate mutations)"
            )
            fills_after = len(await app.master_client.all_orders("main", SYMBOL, limit=1000))
            assert fills_after == fills_before, (
                f"graceful restart issued exchange orders: {fills_before} -> {fills_after}"
            )
            phase_metrics.append(f"post-restart: {_fmt_metrics(app.metrics_snapshot())}")

        # P3: second block.
        if _want("b"):
            await run_range(_restart_at + 1, _sigterm_at, "b")

        # P4: SIGTERM of the App process mid-campaign; reboot converges, no dup.
        if _want("sigterm"):
            await _wait_follower_state_with_recovery(
                app,
                want_open=False,
                want_protection=False,
                timeout=STEP_TIMEOUT,
                description="flat before SIGTERM window",
                retry_stats=retry_stats,
            )
            keys_before = {
                str(row["command_key"])
                for row in await app.db.fetchall("SELECT command_key FROM execution_commands")
            }
            fills_before = len(await app.master_client.all_orders("main", SYMBOL, limit=1000))
            await app.stop()
            db_path_str = str(settings.db_path)
            worker = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve()),
                "--load-app-process",
                db_path_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + STEP_TIMEOUT
                while True:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, (
                        "App worker failed to boot: LOAD_APP_READY not seen in time"
                    )
                    line = await asyncio.wait_for(worker.stdout.readline(), timeout=remaining)
                    if not line:
                        pytest.fail("App worker stdout closed before LOAD_APP_READY")
                    if line.decode(errors="replace").strip() == "LOAD_APP_READY":
                        break
                    # else: engine startup log line on stdout — skip until the marker.
                worker.terminate()
                try:
                    await asyncio.wait_for(worker.wait(), timeout=30)
                except TimeoutError:
                    worker.kill()
                    await worker.wait()
                    pytest.fail("SIGTERM App worker did not exit after SIGTERM")
                assert worker.returncode == -signal.SIGTERM, (
                    f"SIGTERM worker exit {worker.returncode} is not SIGTERM"
                )
            finally:
                if worker.returncode is None:
                    worker.kill()
                    await worker.wait()
            app = await _boot(settings)
            await _wait_follower_state_with_recovery(
                app,
                want_open=False,
                want_protection=False,
                timeout=STEP_TIMEOUT,
                description="resume converges flat after SIGTERM",
                retry_stats=retry_stats,
            )
            keys_after = {
                str(row["command_key"])
                for row in await app.db.fetchall("SELECT command_key FROM execution_commands")
            }
            assert keys_after == keys_before, (
                "SIGTERM recovery replayed commands (duplicate mutations)"
            )
            fills_after = len(await app.master_client.all_orders("main", SYMBOL, limit=1000))
            assert fills_after == fills_before, (
                f"SIGTERM window issued exchange orders: {fills_before} -> {fills_after} "
                "(single economic fill each: zero here)"
            )
            phase_metrics.append(f"post-sigterm: {_fmt_metrics(app.metrics_snapshot())}")

        # P5: third block.
        if _want("c"):
            await run_range(_sigterm_at + 1, _sigkill_at, "c")

        # P6: SIGKILL of a worker mid-cycle (A5 pattern); boot recovery replays
        # the same command_key with a single economic fill.
        if _want("sigkill"):
            await _wait_follower_state_with_recovery(
                app,
                want_open=False,
                want_protection=False,
                timeout=STEP_TIMEOUT,
                description="flat before SIGKILL window",
                retry_stats=retry_stats,
            )
            kill_coid = f"hl-kill-{time.time_ns() % 10**12:012d}"[:36]
            # Scope single-fill proof to THIS run: command_key is deterministic
            # (sha256 of account|trade|kind|payload), so reruns on fresh DBs
            # reuse coids and allOrders still shows older runs' fills.
            kill_t0_ms = time.time_ns() // 1_000_000 - 60_000
            await app.stop()
            killer = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve()),
                "--load-kill-worker",
                db_path_str,
                kill_coid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:

                async def _sending_visible() -> bool:
                    # The killer holds the run DB open; a transient lock error just
                    # means the worker is mid-write, so retry until the timeout.
                    with contextlib.suppress(Exception):
                        db = Database(Path(db_path_str))
                        await db.connect()
                        try:
                            # The App is stopped: the killer worker is the only
                            # writer, so any SENDING row is its UNKNOWN window
                            # (the executor derives ct<command_key> coids; the
                            # kill_coid tags only the worker invocation).
                            rows = await db.fetchall(
                                "SELECT command_key FROM execution_commands "
                                "WHERE status IN ('SENDING','UNKNOWN','SUBMITTED')"
                            )
                            return bool(rows)
                        finally:
                            await db.close()
                    return False

                deadline = time.monotonic() + STEP_TIMEOUT
                while time.monotonic() < deadline:
                    if await _sending_visible():
                        break
                    await asyncio.sleep(0.5)
                else:
                    pytest.fail("SIGKILL worker never reached the UNKNOWN window")
                killer.kill()
                await killer.wait()
                assert killer.returncode == -signal.SIGKILL, (
                    f"SIGKILL worker exit {killer.returncode} is not SIGKILL"
                )

                from copytradeaster.trading.executor import resolve_boot_unknowns

                journal = Database(Path(db_path_str))
                await journal.connect()
                try:
                    commands = CommandRepo(journal)
                    in_flight_rows = await commands.in_flight()
                    assert len(in_flight_rows) == 1, (
                        f"expected exactly one in-flight row after SIGKILL, "
                        f"got {len(in_flight_rows)}"
                    )
                    row = in_flight_rows[0]
                    assert row["status"] == "SENDING", (
                        f"unexpected in-flight status {row['status']}"
                    )
                    # The executor derives the exchange coid (ct<command_key>);
                    # kill_coid only tags the worker invocation, never the order.
                    kill_row_coid = str(row["client_order_id"])
                    # The App is stopped (its transport is closed), so boot
                    # recovery runs over a dedicated signed client sharing the
                    # stopped App's loaded credentials and nonce sequencing.
                    resolve_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
                    try:
                        from copytradeaster.aster.rate_limits import RateLimits

                        resolve_client = AsterClient(
                            resolve_http,
                            app.cfg.rest_base_url,
                            RateLimits(
                                app.cfg.weight_limit_per_min,
                                app.cfg.orders_limit_per_min,
                                app.cfg.orders_limit_per_10s,
                            ),
                            app.credentials,
                            app.credentials,
                            NonceStore(journal),
                            dry_run=False,
                        )
                        report = await resolve_boot_unknowns(
                            lambda _account_id: resolve_client,
                            commands,
                            OrderLinkRepo(journal),
                        )
                        if report.unresolved:
                            # Saturated-main allOrders is eventually consistent;
                            # one bounded re-probe before failing fail-closed.
                            await asyncio.sleep(15)
                            report = await resolve_boot_unknowns(
                                lambda _account_id: resolve_client,
                                commands,
                                OrderLinkRepo(journal),
                            )
                    finally:
                        await resolve_http.aclose()
                    assert report.confirmed == 1 and report.unresolved == 0, (
                        f"boot recovery must replay the same command_key once: {report!r}"
                    )
                    recovered = await commands.get(str(row["command_key"]))
                    assert recovered is not None and recovered["status"] == "CONFIRMED"
                finally:
                    await journal.close()
            finally:
                if killer.returncode is None:
                    killer.kill()
                    await killer.wait()
            app = await _boot(settings)
            matches = [
                o
                for o in await app.master_client.all_orders("main", SYMBOL, limit=1000)
                if o.client_order_id == kill_row_coid and o.update_time >= kill_t0_ms
            ]
            assert len(matches) == 1, (
                f"SIGKILL recovery must leave a single economic fill for {kill_row_coid}, "
                f"got {len(matches)}"
            )
            with contextlib.suppress(Exception):
                await _flatten_account(app.master_client, "main", "hl-killrec")
            await _wait_follower_state_with_recovery(
                app,
                want_open=False,
                want_protection=False,
                timeout=STEP_TIMEOUT,
                description="resume converges flat after SIGKILL",
                retry_stats=retry_stats,
            )
            phase_metrics.append(f"post-sigkill: {_fmt_metrics(app.metrics_snapshot())}")

        # P7: final block to >=100 cycles.
        if _want("d"):
            await run_range(_sigkill_at + 1, _cycles, "d")

        # P7b: resolve any tail SENDING/UNKNOWN via the PROD boot-recovery path
        # before the zero-row audit (a response-loss on the last driver send
        # must confirm through recovery, not fail the gate outright; genuinely
        # unresolvable rows still fail P8a fail-closed).
        if _want("d"):
            from copytradeaster.trading.executor import resolve_boot_unknowns as _resolve_tail

            tail_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
            try:
                tail_client = AsterClient(
                    tail_http,
                    app.cfg.rest_base_url,
                    app.limiter,
                    app.credentials,
                    app.credentials,
                    app.nonces,
                    dry_run=False,
                )
                tail_report = await _resolve_tail(
                    lambda _account_id: tail_client,
                    CommandRepo(app.db),
                    OrderLinkRepo(app.db),
                )
                phase_metrics.append(f"tail-resolve: {tail_report!r}")
            finally:
                await tail_http.aclose()

        # P8a: journal — zero SENDING/UNKNOWN/FAILED-terminal rows, unique keys.
        if _want("audit"):
            status_rows = await app.db.fetchall(
                "SELECT status, COUNT(*) AS n FROM execution_commands GROUP BY status"
            )
            counts = {str(row["status"]): int(row["n"]) for row in status_rows}
            bad = {status: n for status, n in counts.items() if status != "CONFIRMED"}
            assert not bad, f"non-confirmed journal rows remain: {bad} (full counts: {counts})"
            for terminal in ("SENDING", "UNKNOWN", "FAILED"):
                assert counts.get(terminal, 0) == 0, (
                    f"journal has {counts.get(terminal, 0)} {terminal} rows (full counts: {counts})"
                )
            key_rows = await app.db.fetchall("SELECT command_key FROM execution_commands")
            keys = [str(row["command_key"]) for row in key_rows]
            assert len(keys) == len(set(keys)), "duplicate command_key rows in the journal"
            assert not app._ingest_dropped, f"master ingest dropped {app._ingest_dropped} events"

        # P8b: metrics percentiles present; report p50/p95/p99 + weight/min.
        if _want("audit"):
            snapshot = app.metrics_snapshot()
            for series_key in ("reconcile_duration_s", "follower_duration_s"):
                series = snapshot.get(series_key)
                assert isinstance(series, dict), (
                    f"{series_key} missing from metrics snapshot: {snapshot!r}"
                )
                for quantile in ("p50", "p95", "p99"):
                    assert series.get(quantile) is not None, (
                        f"{series_key}.{quantile} is None (no load samples recorded); "
                        f"snapshot={_fmt_metrics(snapshot)}; phases={phase_metrics}"
                    )
                assert series.get("count", 0) > 0, (
                    f"{series_key} recorded zero samples; snapshot={_fmt_metrics(snapshot)}; "
                    f"phases={phase_metrics}"
                )
            assert isinstance(snapshot.get("request_weight_per_min"), int), (
                f"request_weight_per_min missing; snapshot={snapshot!r}"
            )
            phase_metrics.append(f"final: {_fmt_metrics(snapshot)}")

        # P8c: final flat/order-free signed audit on ALL real accounts, then stop.
        if _want("audit"):
            audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))

            audit = AsterClient(
                audit_http,
                app.cfg.rest_base_url,
                app.limiter,
                app.credentials,
                app.credentials,
                app.nonces,
                dry_run=False,
            )
            with contextlib.suppress(Exception):
                for account_id in ["main", *real_ids]:
                    await _flatten_account(app.master_client, account_id, "hl-clean")
                for account_id in shadow_ids:
                    fr = next(fr for fr in app.followers if fr.account_id == account_id)
                    with contextlib.suppress(Exception):
                        await _flatten_account(fr.client, account_id, "hs-clean")
            await _assert_flat_signed(audit, ["main", *real_ids])
    finally:
        with contextlib.suppress(Exception):
            await app.stop()
        if audit_http is not None:
            with contextlib.suppress(Exception):
                await audit_http.aclose()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--load-kill-worker":
        asyncio.run(_run_kill_worker(sys.argv[2], sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--load-app-process":
        asyncio.run(_run_app_process_worker(sys.argv[2]))
    else:
        raise SystemExit(
            "usage: test_pre_mainnet_load_live.py --load-app-process <db_path> | "
            "--load-kill-worker <db_path> <coid>"
        )
