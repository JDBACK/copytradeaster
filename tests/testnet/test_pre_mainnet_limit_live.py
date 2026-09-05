"""Package C: LIMIT live matrix (pre-mainnet, REAL testnet only).

Live-only qualification that the engine mirrors FILLED magnitude, never pending
LIMIT intent, over the REAL Aster Testnet (``fapi.asterdex-testnet.com``) with
real ``wallet.txt`` accounts. Every test boots the PRODUCTION App on a scratch
DB (seeded from ``data/copytrade.sqlite3`` accounts+credentials, never the prod
file itself), drives the REAL master user stream
(``fstream.asterdex-testnet.com``) and REAL signed REST, and ends with
best-effort flatten + an independent signed flat audit (``position_risk`` flat
+ ``open_orders`` empty) in ``finally``.

Row coverage (LIMIT qualification rows) -> test node:
- L1 ``test_limit_entry_partials_track_filled_only``: master real LIMIT BUY
  away from mark rests unfilled -> followers stay flat, no episode (pending
  intent is never mirrored); the price is then walked toward the market until
  partial fills land (target: 2 partial observations) and the remainder is
  cancelled -> followers track the filled quantity only, single OPEN episode.
- L2 ``test_limit_reduction_partials_converge_to_residual``: open master
  position, then a real LIMIT SELL reduction away from mark -> followers hold
  entry size (no front-running of the pending reduction); walked down to a
  partial fill and remainder cancelled -> followers converge down to the
  residual, projector equals REST.
- L3 ``test_limit_flat_single_episode_no_duplicates``: MARKET open+close ->
  final flat on every account plus journal proof of exactly one episode for the
  symbol (no duplicate episode, unique follower order ids).

HEDGE-aware: main is HEDGE, so LONG/SHORT legs exist alongside zero legs;
quantity reads sum legs and only nonzero legs count toward convergence.
Settle/stability gates (``_wait_stable``) prove quiescence across consecutive
polls; no single-shot asserts against the LIVE App. No reconnect injection is
used here (no W-row), so no forced ``close_listen_key`` appears. No direct
executor submits occur anywhere in this file: every ``ExecutionAction`` flows
from the production Coordinator with its per-leg ``sync_basis`` revision, so
the deterministic-COID lesson holds by construction. Fault injection: none
(master orders are all real).

Offline/fake NEVER closes a row: no mocks, no synthetic fills, no shadow
execution. NOBODY runs live pytest from shared-state checkouts: these tests
are implementation-only until the pre-mainnet run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import time
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.app import App
from copytradeaster.aster.trading_api import PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.storage.database import Database

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
QTY = D("0.002")
QTY_TOTAL = D("0.006")
PROD_DB = Path("data/copytrade.sqlite3")
_SEED_TABLES = ("accounts", "credentials", "copy_settings", "agent_validations")


# --- scratch scaffolding -------------------------------------------------------


def _settings_for(tmp_path: Path, name: str) -> Settings:
    settings = Settings(
        db_path=tmp_path / f"{name}.sqlite3",
        log_path=tmp_path / f"{name}.log",
    )
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    if settings.dry_run:
        pytest.fail("live matrix refuses to run in dry_run: orders would be fake")
    return settings


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


def _seed_scratch(db_path: Path, max_followers: int = 2) -> list[str]:
    """Copy accounts+credentials into a fresh scratch DB; keep main + K followers.

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


async def _prepare_scratch(
    tmp_path: Path, name: str, max_followers: int = 2
) -> tuple[Settings, list[str]]:
    settings = _settings_for(tmp_path, name)
    seed_db = Database(settings.db_path)
    await seed_db.connect()
    try:
        await seed_db.migrate()
    finally:
        await seed_db.close()
    followers = _seed_scratch(Path(str(settings.db_path)), max_followers)
    return settings, followers


async def _wait_for(predicate, timeout: float, what: str, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


async def _ready_flag(app: App) -> bool:
    return await app.settings.get_flag("ready") == "true"


# --- live trading helpers (all REAL signed REST) --------------------------------


async def _position_side(client, account_id: str = "main") -> PosSide:
    hedge = await client.get_position_mode(account_id)
    return PosSide.LONG if hedge else PosSide.BOTH


async def _symbol_qty(client, account_id: str) -> D:
    """HEDGE-aware total: sums LONG/SHORT legs, ignores zero legs."""
    total = D("0")
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL:
            total += position.position_amt
    return total


async def _follower_stops(client, account_id: str) -> list:
    return [
        order
        for order in await client.open_orders(account_id, SYMBOL)
        if order.client_order_id.startswith("ct") and order.type.startswith("STOP")
    ]


async def _mark_price(client) -> D:
    rows = await client.position_risk("main", SYMBOL)
    for row in rows:
        if row.mark_price > 0:
            return row.mark_price
    pytest.skip("no live mark price on testnet positionRisk")


async def _tick_step(client) -> tuple[D, D]:
    info = await client.exchange_info(force=True)
    filt = info.tradable_filters()[SYMBOL]
    return filt.tick_size, filt.step_size


def _snap_down(px: D, tick: D) -> D:
    return (px // tick) * tick


def _snap_up(px: D, tick: D) -> D:
    stepped = (px // tick) * tick
    return stepped if stepped == px else stepped + tick


async def _place_limit_buy(client, qty: D, price: D, position_side: PosSide, tag: str) -> str:
    coid = f"{tag}-{time.time_ns()}"[:36]
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="LIMIT",
            quantity=qty,
            price=price,
            time_in_force="GTC",
            position_side=position_side,
            new_client_order_id=coid,
            new_order_resp_type="RESULT",
        ),
    )
    return coid


async def _place_limit_sell(client, qty: D, price: D, position_side: PosSide, tag: str) -> str:
    reduce_only = position_side == PosSide.BOTH
    coid = f"{tag}-{time.time_ns()}"[:36]
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="LIMIT",
            quantity=qty,
            price=price,
            time_in_force="GTC",
            reduce_only=reduce_only,
            position_side=position_side,
            new_client_order_id=coid,
            new_order_resp_type="RESULT",
        ),
    )
    return coid


async def _rest_master_stop(client, position_side: PosSide, qty: D, ref_price: D, tag: str) -> str:
    """Real protective STOP_MARKET on main, sized to the entry qty.

    Mirror of the master-matrix ``_open_master_long`` stop: SELL STOP_MARKET
    below the reference price (limit price for LIMIT entries, entry price for
    MARKET opens), ``reduce_only`` only in one-way mode, MARK_PRICE working
    type, unique COID. The engine may only copy a PROTECTED master position,
    so every master entry in this file rests this stop alongside it. The
    offset floors at 2% of mark so the stop can never rest at/above a high
    mark and trigger on placement.
    """
    rows = await client.position_risk("main", SYMBOL)
    mark = next((row.mark_price for row in rows if row.mark_price > 0), ref_price)
    offset = max(D("1200"), (mark * D("0.02")).quantize(D("0.1")))
    stop_level = (ref_price - offset).quantize(D("0.1"))
    hedge = position_side != PosSide.BOTH
    coid = f"{tag}-mstop-{time.time_ns()}"[:36]
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=qty,
            stop_price=stop_level,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=coid,
        ),
    )
    return coid


async def _ensure_master_stop(
    client, position_side: PosSide, qty: D, ref_price: D, tag: str
) -> str:
    """Rest a master STOP_MARKET alongside an entry; reuse the open one."""
    for order in await client.open_orders("main", SYMBOL):
        if order.type == "STOP_MARKET":
            return order.client_order_id
    return await _rest_master_stop(client, position_side, qty, ref_price, tag)


async def _cancel_master_stops(client) -> None:
    """Cancel every open master STOP_MARKET (mirrors the in-body stop cancel)."""
    for order in await client.open_orders("main", SYMBOL):
        if order.type == "STOP_MARKET":
            await _cancel_coid(client, "main", order.client_order_id)


async def _cancel_coid(client, account_id: str, coid: str) -> None:
    with contextlib.suppress(Exception):
        await client.cancel_order(account_id, SYMBOL, orig_client_order_id=coid)


async def _master_limit_pending(client) -> list:
    """Open master LIMIT orders on SYMBOL (the pending, unfilled intent)."""
    return [order for order in await client.open_orders("main", SYMBOL) if order.type == "LIMIT"]


async def _close_master_qty(client, position_side: PosSide, qty: D, tag: str) -> None:
    if qty == 0:
        return
    hedge = position_side != PosSide.BOTH
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=qty,
            reduce_only=not hedge,
            position_side=position_side,
            new_client_order_id=f"{tag}-close-{time.time_ns()}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


async def _open_master_market(client, position_side: PosSide, qty: D, tag: str) -> None:
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=qty,
            position_side=position_side,
            new_client_order_id=f"{tag}-open-{time.time_ns()}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


async def _flatten_account(client, account_id: str) -> None:
    """Best-effort cancel + close for one account."""
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
                        new_client_order_id=f"pm-flat-{account_id}-{time.time_ns()}"[:36],
                        new_order_resp_type="RESULT",
                    ),
                )


async def _assert_flat(client, account_ids: list[str]) -> None:
    """Independent signed audit: every account flat, no open orders on SYMBOL."""
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


def _audit_client(app: App, http: httpx.AsyncClient):
    """Separate transport, shared credential/nonce sequencing (no nonce reuse)."""
    from copytradeaster.aster.trading_api import AsterClient

    return AsterClient(
        http,
        app.cfg.rest_base_url,
        app.limiter,
        app.credentials,
        app.credentials,
        app.nonces,
        dry_run=False,
    )


async def _flatten_audit_stop(
    app: App, audit, audit_http: httpx.AsyncClient, account_ids: list[str]
) -> None:
    """Finally block: best-effort flatten, HARD signed flat audit, then teardown."""
    with contextlib.suppress(Exception):
        for account_id in account_ids:
            await _flatten_account(app.master_client, account_id)
    try:
        await _assert_flat(audit, account_ids)
    finally:
        with contextlib.suppress(Exception):
            await app.stop()
        with contextlib.suppress(Exception):
            await audit_http.aclose()


async def _wait_stable(
    check, *, polls: int = 3, interval: float = 1.5, timeout: float = 60.0, what: str
) -> None:
    """Poll an async ``check`` until it holds across N consecutive polls.

    ``check`` returns ``(ok, detail)``; on timeout the error names the last
    failing sub-condition(s) with values. Single-shot asserts against a LIVE
    App (background sweep/reconcile loops) are inherently racy; stability
    across consecutive polls is the only meaningful quiescence proof.
    """
    deadline = time.monotonic() + timeout
    stable = 0
    last_detail = ""
    while time.monotonic() < deadline:
        ok, detail = await check()
        if ok:
            stable += 1
            if stable >= polls:
                return
        else:
            stable = 0
            last_detail = detail
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for stable {what}; last failing: {last_detail}")


def _projector_symbol_qty(app: App) -> D:
    return sum(
        (
            position.qty
            for key, position in app.projector.state.positions.items()
            if key.symbol == SYMBOL
        ),
        D("0"),
    )


async def _copy_trade_count(app: App, status: str | None = None) -> int:
    if status is None:
        rows = await app.db.fetchall("SELECT trade_id FROM copy_trades WHERE symbol = ?", (SYMBOL,))
    else:
        rows = await app.db.fetchall(
            "SELECT trade_id FROM copy_trades WHERE symbol = ? AND status = ?", (SYMBOL, status)
        )
    return len(rows)


async def _wait_followers_settled(
    app: App, followers: list[str], polls: int = 3, interval: float = 2.0, timeout: float = 60.0
) -> None:
    """Require follower qty unchanged AND exactly one stop across N polls."""
    deadline = time.monotonic() + timeout
    last: tuple | None = None
    stable = 0
    while time.monotonic() < deadline:
        current: list[tuple[D, int]] = []
        for account_id in followers:
            qty = await _symbol_qty(app.master_client, account_id)
            stops = len(await _follower_stops(app.master_client, account_id))
            current.append((qty, stops))
        settled = (
            all(qty != 0 and stops == 1 for qty, stops in current)
            and last is not None
            and list(current) == list(last)
        )
        if settled:
            stable += 1
            if stable >= polls - 1:
                return
        else:
            stable = 0
        last = list(current)
        await asyncio.sleep(interval)
    raise TimeoutError("timed out waiting for followers settled (stable qty + one stop)")


# --- L1: LIMIT entry, partial fills track filled magnitude only ----------------------


async def test_limit_entry_partials_track_filled_only(tmp_path):
    """L1: resting LIMIT is not mirrored; walked-to partial fills are, singly.

    Phase A places a real LIMIT BUY away from mark (pending intent): followers
    must stay flat with no episode, proven stable. Phase B walks the limit
    price toward the market until partial fills land (target: 2 partial
    observations; a single-partial or instant-full branch is recorded from the
    measured fill trajectory) and cancels the remainder: followers track the
    filled quantity only, in a single OPEN episode, and never grow toward the
    cancelled pending remainder.
    """
    settings, followers = await _prepare_scratch(tmp_path, "limit_c1", max_followers=2)
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", *followers]:
            await _flatten_account(client, account_id)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        mark = await _mark_price(client)
        tick, _step = await _tick_step(client)

        # Phase A: real resting LIMIT away from mark (3% below, GTC) with a
        # real protective STOP_MARKET alongside it: the engine only copies a
        # protected master position, so the stop must rest from entry on.
        away = _snap_down(mark * D("0.97"), tick)
        away_coid = await _place_limit_buy(client, QTY_TOTAL, away, position_side, "pm-c1-away")
        await _ensure_master_stop(client, position_side, QTY_TOTAL, away, "pm-c1a")

        async def _resting_not_mirrored() -> tuple[bool, str]:
            failures = []
            if await _symbol_qty(client, "main") != 0:
                failures.append("master resting LIMIT filled immediately")
            pending = await _master_limit_pending(client)
            if not any(o.client_order_id == away_coid for o in pending):
                failures.append("resting LIMIT missing from REST (pending intent not real)")
            if not any(o.type == "STOP_MARKET" for o in await client.open_orders("main", SYMBOL)):
                failures.append("no protective master STOP rests alongside the LIMIT entry")
            for account_id in followers:
                if await _symbol_qty(client, account_id) != 0:
                    failures.append(f"{account_id} mirrors pending intent")
            if await _copy_trade_count(app) != 0:
                failures.append("episode opened for unfilled LIMIT")
            if _projector_symbol_qty(app) != 0:
                failures.append(
                    f"projector nonzero while master flat: {_projector_symbol_qty(app)}"
                )
            return (not failures, "; ".join(failures))

        await _wait_stable(_resting_not_mirrored, what="L1 resting LIMIT not mirrored")
        await _cancel_coid(client, "main", away_coid)

        # Phase B: walk the limit price toward the market; record fill steps.
        trajectory: list[D] = []
        partial_observations = 0
        filled = await _symbol_qty(client, "main")
        assert filled == 0, f"away LIMIT filled before the walk: {filled}"
        mults = [D("0.999"), D("1.001"), D("1.003"), D("1.006"), D("1.010")]
        mi = 0
        while mi < len(mults):
            filled = await _symbol_qty(client, "main")
            remaining = QTY_TOTAL - filled
            if remaining <= 0 or remaining < D("0.001"):
                break
            px = _snap_up(mark * mults[mi], tick)
            coid = await _place_limit_buy(client, remaining, px, position_side, f"pm-c1-w{mi}")
            # The entry stop from Phase A stays resting through the walk; re-use
            # it (place only if missing) so every walked LIMIT stays protected.
            await _ensure_master_stop(client, position_side, QTY_TOTAL, px, f"pm-c1-w{mi}")
            try:
                prev = filled
                step_deadline = time.monotonic() + 25
                while time.monotonic() < step_deadline:
                    await asyncio.sleep(1.0)
                    cur = await _symbol_qty(client, "main")
                    if cur != prev:
                        trajectory.append(cur)
                        if D("0") < cur < QTY_TOTAL:
                            partial_observations += 1
                        prev = cur
                        if cur >= QTY_TOTAL:
                            break
            finally:
                await _cancel_coid(client, "main", coid)
            filled = await _symbol_qty(client, "main")
            if filled >= QTY_TOTAL:
                break
            if filled > 0 and partial_observations >= 2:
                break
            mi += 1

        for coid in [away_coid]:
            await _cancel_coid(client, "main", coid)
        for order in await _master_limit_pending(client):
            await _cancel_coid(client, "main", order.client_order_id)
        filled = await _symbol_qty(client, "main")
        if filled == 0:
            pytest.skip(
                f"walk produced no fills (trajectory={trajectory}); "
                "market ran away from the limit steps"
            )
        branch = "full-fill" if filled >= QTY_TOTAL else f"{partial_observations}-partial"

        # Followers converge to the filled magnitude only, then hold it after
        # the pending remainder is gone.
        await _wait_followers_settled(app, followers)
        settled_followers = {
            account_id: await _symbol_qty(client, account_id) for account_id in followers
        }
        assert await _copy_trade_count(app, "OPEN") == 1, (
            f"L1 ({branch}): expected a single OPEN episode, trajectory={trajectory}"
        )

        async def _holds_filled_only() -> tuple[bool, str]:
            failures = []
            if await _symbol_qty(client, "main") != filled:
                failures.append(f"master {await _symbol_qty(client, 'main')} != filled {filled}")
            if await _master_limit_pending(client):
                failures.append("pending LIMIT remainder still open")
            if _projector_symbol_qty(app) != filled:
                failures.append(f"projector {_projector_symbol_qty(app)} != filled {filled}")
            for account_id in followers:
                cur = await _symbol_qty(client, account_id)
                if cur != settled_followers[account_id]:
                    failures.append(
                        f"{account_id} drifted {settled_followers[account_id]} -> {cur} "
                        f"after pending cancelled (branch={branch})"
                    )
                if cur == 0:
                    failures.append(f"{account_id} flat despite filled master {filled}")
            if await _copy_trade_count(app, "OPEN") != 1:
                failures.append("episode count != 1 after remainder cancelled")
            return (not failures, "; ".join(failures))

        await _wait_stable(_holds_filled_only, what="L1 followers hold filled qty only")
        await _cancel_master_stops(client)
        await _close_master_qty(client, position_side, filled, "pm-c1")
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", *followers])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()
        with contextlib.suppress(Exception):
            await app.http.aclose()
        with contextlib.suppress(Exception):
            await app.db.close()


# --- L2: LIMIT reduction converges to the residual -----------------------------------


async def test_limit_reduction_partials_converge_to_residual(tmp_path):
    """L2: pending LIMIT reduction is not front-run; partial fill converges.

    Master opens a real MARKET position (followers copy entry size); a real
    LIMIT SELL reduction away from mark must leave followers at entry size,
    proven stable. Walked down to a partial fill with the remainder cancelled,
    followers converge down to the residual and the projector equals REST.
    """
    settings, followers = await _prepare_scratch(tmp_path, "limit_c2", max_followers=2)
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", *followers]:
            await _flatten_account(client, account_id)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        mark = await _mark_price(client)
        tick, _step = await _tick_step(client)
        await _open_master_market(client, position_side, QTY, "pm-c2")
        # Real protective STOP_MARKET on the entry (master-matrix mirror): the
        # engine only copies a protected master position.
        position = next(
            p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0
        )
        await _rest_master_stop(client, position_side, QTY, position.entry_price, "pm-c2-entry")
        await _wait_followers_settled(app, followers)
        entry_followers = {
            account_id: await _symbol_qty(client, account_id) for account_id in followers
        }
        assert await _copy_trade_count(app, "OPEN") == 1

        # Pending reduction away from mark (3% above): no front-running.
        away = _snap_up(mark * D("1.03"), tick)
        reduce_coid = await _place_limit_sell(client, QTY, away, position_side, "pm-c2-red")

        async def _reduction_not_frontrun() -> tuple[bool, str]:
            failures = []
            if await _symbol_qty(client, "main") != QTY:
                failures.append("master moved before the reduction filled")
            pending = await _master_limit_pending(client)
            if not any(o.client_order_id == reduce_coid for o in pending):
                failures.append("pending reduction missing from REST")
            for account_id in followers:
                cur = await _symbol_qty(client, account_id)
                if cur != entry_followers[account_id]:
                    failures.append(f"{account_id} front-ran pending reduction to {cur}")
            return (not failures, "; ".join(failures))

        await _wait_stable(_reduction_not_frontrun, what="L2 pending reduction not front-run")
        await _cancel_coid(client, "main", reduce_coid)

        # Walk the reduction down until a partial fill lands, cancel the rest.
        residual = await _symbol_qty(client, "main")
        assert residual == QTY
        for mi, mult in enumerate([D("1.001"), D("0.999"), D("0.997"), D("0.995")]):
            residual = await _symbol_qty(client, "main")
            if residual < QTY:
                break
            px = _snap_down(mark * mult, tick)
            coid = await _place_limit_sell(client, residual, px, position_side, f"pm-c2-w{mi}")
            try:
                step_deadline = time.monotonic() + 25
                while time.monotonic() < step_deadline:
                    await asyncio.sleep(1.0)
                    if await _symbol_qty(client, "main") < residual:
                        break
            finally:
                await _cancel_coid(client, "main", coid)
        for order in await _master_limit_pending(client):
            await _cancel_coid(client, "main", order.client_order_id)
        residual = await _symbol_qty(client, "main")
        if residual == QTY:
            pytest.skip("reduction walk produced no fills; market ran away from the limit steps")
        if residual == 0:
            pytest.skip(
                f"reduction filled in full ({QTY}); residual-convergence row needs a partial"
            )

        async def _converged_to_residual() -> tuple[bool, str]:
            failures = []
            if await _symbol_qty(client, "main") != residual:
                failures.append("master moved after remainder cancelled")
            if _projector_symbol_qty(app) != residual:
                failures.append(
                    f"projector {_projector_symbol_qty(app)} != REST residual {residual}"
                )
            for account_id in followers:
                cur = await _symbol_qty(client, account_id)
                if cur == 0:
                    failures.append(f"{account_id} flat despite residual {residual}")
                elif cur >= entry_followers[account_id]:
                    failures.append(
                        f"{account_id} did not reduce: {entry_followers[account_id]} -> {cur}"
                    )
            return (not failures, "; ".join(failures))

        await _wait_stable(_converged_to_residual, what="L2 followers converged to residual")

        await _cancel_master_stops(client)
        await _close_master_qty(client, position_side, residual, "pm-c2")
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", *followers])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()
        with contextlib.suppress(Exception):
            await app.http.aclose()
        with contextlib.suppress(Exception):
            await app.db.close()


# --- L3: final flat, single episode, no duplicates -------------------------------------


async def test_limit_flat_single_episode_no_duplicates(tmp_path):
    """L3: open+close leaves every account flat with exactly one journal episode.

    The copy journal must hold a single episode for the symbol (no duplicate
    episode from LIMIT/MARKET boundary replays) and follower order links must
    carry unique order ids.
    """
    settings, followers = await _prepare_scratch(tmp_path, "limit_c3", max_followers=2)
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", *followers]:
            await _flatten_account(client, account_id)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        # Real protective STOP_MARKET on the entry (master-matrix mirror): the
        # engine only copies a protected master position.
        await _open_master_market(client, position_side, QTY, "pm-c3")
        position = next(
            p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0
        )
        await _rest_master_stop(client, position_side, QTY, position.entry_price, "pm-c3-entry")
        await _wait_followers_settled(app, followers)
        assert await _copy_trade_count(app, "OPEN") == 1

        await _cancel_master_stops(client)
        await _close_master_qty(client, position_side, QTY, "pm-c3")

        async def _all_flat() -> tuple[bool, str]:
            failures = []
            for account_id in ["main", *followers]:
                if await _symbol_qty(client, account_id) != 0:
                    failures.append(f"{account_id} not flat")
            return (not failures, "; ".join(failures))

        await _wait_stable(_all_flat, what="L3 all accounts flat")
        await _assert_flat(audit, ["main", *followers])

        total = await _copy_trade_count(app)
        open_count = await _copy_trade_count(app, "OPEN")
        assert total == 1, f"expected exactly one episode, found {total}"
        assert open_count == 0, f"expected no OPEN episode after close, found {open_count}"
        rows = await app.db.fetchall("SELECT status FROM copy_trades WHERE symbol = ?", (SYMBOL,))
        assert len(rows) == 1 and rows[0]["status"] != "OPEN"
        link_rows = await app.db.fetchall("SELECT follower_client_order_id FROM order_links", ())
        coids = [str(row["follower_client_order_id"]) for row in link_rows]
        assert len(coids) == len(set(coids)), f"duplicate follower order links: {coids}"
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", *followers])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()
        with contextlib.suppress(Exception):
            await app.http.aclose()
        with contextlib.suppress(Exception):
            await app.db.close()
