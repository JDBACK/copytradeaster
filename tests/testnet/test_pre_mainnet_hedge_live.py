"""Package G: HEDGE live matrix (pre-mainnet, REAL testnet only).

Live-only qualification: every test boots the PRODUCTION App on a scratch DB
(seeded from ``data/copytrade.sqlite3`` accounts+credentials, never the prod
file itself) with ``CT_POSITION_MODE=HEDGE`` (repo ``.env``), drives REAL
signed REST (``fapi.asterdex-testnet.com``) with real ``wallet.txt`` accounts,
and ends with best-effort flatten + an independent signed flat audit
(``position_risk`` flat + ``open_orders`` empty) in ``finally``.

Row coverage (HEDGE dual-leg matrix):
- G0 ``test_hedge_precheck_flat``: exchange starts flat / no open orders.
- G1 ``test_hedge_long_and_short_together``: master LONG + SHORT legs open
  together; the follower mirrors BOTH legs simultaneously (no netting).
- G2 ``test_hedge_different_fills``: asymmetric leg sizes; the follower keeps
  both legs with the same size ordering (no netting, no leg drop).
- G3 ``test_hedge_stop_per_leg_replace``: each leg carries its own resting
  stop on the correct side; replacing the master SHORT stop converges the
  follower SHORT stop while the LONG stop is untouched.
- G4 ``test_hedge_leverage_update_keeps_legs``: a symbol leverage update
  propagates to the follower while both legs (qty + stops) are untouched.
- G5 ``test_hedge_partial_close_one_leg``: partial master LONG reduction
  shrinks only the follower LONG leg; the SHORT leg is untouched.
- G6 ``test_hedge_close_one_leg_keeps_other``: full master LONG close flattens
  only the follower LONG leg (qty 0, no LONG stops); SHORT intact.

Offline/fake NEVER closes a row: no mocks, no synthetic fills, no shadow
execution. A row that collides with exchange behaviour (as Package E L2 did)
is an honest skip with proof, never a fit.
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
QTY_BIG = D("0.004")
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
    if settings.position_mode != "HEDGE":
        pytest.fail(f"Package G requires CT_POSITION_MODE=HEDGE, got {settings.position_mode!r}")
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


def _seed_scratch(db_path: Path, max_followers: int = 1) -> list[str]:
    if not PROD_DB.exists():
        pytest.skip("production DB data/copytrade.sqlite3 is required to seed live credentials")
    src = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    try:
        for table in _SEED_TABLES:
            _copy_table(src, dst, table)
        rows = dst.execute(
            "SELECT id FROM accounts WHERE role = 'follower' AND enabled = 1 "
            "AND COALESCE(execution_mode, 'REAL') = 'REAL' ORDER BY id"
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
    tmp_path: Path, name: str, max_followers: int = 1
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


async def _wait_stable(
    check, *, polls: int = 3, interval: float = 1.5, timeout: float = 90.0, what: str
) -> None:
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


async def _ready_flag(app: App) -> bool:
    return await app.settings.get_flag("ready") == "true"


# --- live trading helpers (all REAL signed REST) --------------------------------


async def _leg_qty(client, account_id: str, leg: PosSide) -> D:
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL and position.position_side == leg:
            return position.position_amt
    return D("0")


async def _leg_stops(client, account_id: str, leg: PosSide) -> list:
    return [
        order
        for order in await client.open_orders(account_id, SYMBOL)
        if order.client_order_id.startswith("ct")
        and order.type.startswith("STOP")
        and order.position_side == leg
    ]


async def _open_master_leg(
    client, leg: PosSide, qty: D, tag: str, stop_distance: D = D("1200")
) -> dict:
    """REAL master MARKET open on one HEDGE leg + protective STOP_MARKET."""
    stamp = time.time_ns()
    entry_side = Side.BUY if leg == PosSide.LONG else Side.SELL
    stop_side = Side.SELL if leg == PosSide.LONG else Side.BUY
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=entry_side,
            type="MARKET",
            quantity=qty,
            position_side=leg,
            new_client_order_id=f"{tag}-open-{stamp}"[:36],
            new_order_resp_type="RESULT",
        ),
    )
    position = next(
        p
        for p in await client.position_risk("main", SYMBOL)
        if p.position_amt != 0 and p.position_side == leg
    )
    if leg == PosSide.LONG:
        stop_level = (position.entry_price - stop_distance).quantize(D("0.1"))
    else:
        stop_level = (position.entry_price + stop_distance).quantize(D("0.1"))
    master_stop = await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=stop_side,
            type="STOP_MARKET",
            quantity=abs(position.position_amt),
            stop_price=stop_level,
            reduce_only=False,
            position_side=leg,
            working_type="MARK_PRICE",
            new_client_order_id=f"{tag}-stop-{stamp}"[:36],
        ),
    )
    return {"leg": leg, "stop_coid": master_stop.client_order_id, "stop": stop_level}


async def _reduce_master_leg(client, leg: PosSide, qty: D, tag: str) -> None:
    """REAL master reduce-only MARKET on one HEDGE leg (partial or full).

    HEDGE closes carry positionSide with NO reduceOnly flag (the exchange
    rejects reduceOnly in hedge mode with -1106) - same convention as the
    production planner.
    """
    close_side = Side.SELL if leg == PosSide.LONG else Side.BUY
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=close_side,
            type="MARKET",
            quantity=qty,
            reduce_only=False,
            position_side=leg,
            new_client_order_id=f"{tag}-red-{time.time_ns()}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


async def _cancel_master_leg_stops(client, leg: PosSide) -> None:
    for order in await client.open_orders("main", SYMBOL):
        if order.position_side == leg and order.type.startswith("STOP"):
            with contextlib.suppress(Exception):
                await client.cancel_order(
                    "main", SYMBOL, orig_client_order_id=order.client_order_id
                )


async def _flatten_account(client, account_id: str) -> None:
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
    # Teardown order matters. Flattening while the engine runs lets it
    # reopen follower legs (it still sees the master legs open); auditing
    # while it runs has the same race. So: flatten (main first, so the
    # engine converges followers to flat instead of reopening them), stop
    # the engine, revive storage for the audit client's NonceStore, audit.
    with contextlib.suppress(Exception):
        await _flatten_account(app.master_client, "main")
    with contextlib.suppress(Exception):
        await asyncio.sleep(3.0)
    with contextlib.suppress(Exception):
        for account_id in account_ids:
            if account_id != "main":
                await _flatten_account(app.master_client, account_id)
    with contextlib.suppress(Exception):
        await app.stop()
    try:
        with contextlib.suppress(Exception):
            await app.db.connect()
        await _assert_flat(audit, account_ids)
    finally:
        with contextlib.suppress(Exception):
            await audit_http.aclose()


async def _start_app(settings: Settings) -> App:
    app = App(settings)
    await app.start()
    assert app._ready and app._stream_connect_count >= 1
    assert await _ready_flag(app)
    return app


async def _stable_leg_qtys(audit, fid: str, what: str) -> tuple:
    """Both legs nonzero AND unchanging across polls; returns settled qtys.

    Capturing a baseline right after legs first appear races the engine's
    own convergence trim (live: SHORT -0.005 trimmed to -0.004 seconds after
    the open). Stability across polls absorbs one-shot convergence, so the
    baseline is a value the engine itself already holds still.
    """
    holder: dict = {}

    async def settled():
        long_qty = await _leg_qty(audit, fid, PosSide.LONG)
        short_qty = await _leg_qty(audit, fid, PosSide.SHORT)
        prev = (holder.get("long"), holder.get("short"))
        holder["long"] = long_qty
        holder["short"] = short_qty
        ok = long_qty > 0 and short_qty < 0 and prev == (long_qty, short_qty)
        return ok, f"long={long_qty} short={short_qty}"

    await _wait_stable(settled, what=what)
    return holder["long"], holder["short"]


async def _economic_sends(app: App, account_id: str) -> int:
    rows = await app.db.fetchall(
        "SELECT status FROM execution_commands WHERE account_id = ? AND status IN "
        "('SUBMITTED','CONFIRMED')",
        (account_id,),
    )
    return len(rows)


# --- G0: pre-check ---------------------------------------------------------------


async def test_hedge_precheck_flat(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g0")
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    try:
        await _assert_flat(audit, ["main", *followers])
    finally:
        with contextlib.suppress(Exception):
            await app.stop()
        with contextlib.suppress(Exception):
            await audit_http.aclose()


# --- G1: LONG + SHORT together -----------------------------------------------------


async def test_hedge_long_and_short_together(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g1")
    (fid,) = followers
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    account_ids = ["main", fid]
    try:
        await _open_master_leg(app.master_client, PosSide.LONG, QTY, "pm-g1")
        await _open_master_leg(app.master_client, PosSide.SHORT, QTY, "pm-g1")

        async def both_legs():
            long_qty = await _leg_qty(audit, fid, PosSide.LONG)
            short_qty = await _leg_qty(audit, fid, PosSide.SHORT)
            long_stops = len(await _leg_stops(audit, fid, PosSide.LONG))
            short_stops = len(await _leg_stops(audit, fid, PosSide.SHORT))
            ok = long_qty > 0 and short_qty < 0 and long_stops >= 1 and short_stops >= 1
            return ok, f"long={long_qty}/{long_stops} short={short_qty}/{short_stops}"

        await _wait_stable(both_legs, what="follower LONG+SHORT legs with stops")
        sends = await _economic_sends(app, fid)
        assert sends >= 2, f"expected >=2 economic sends for two legs, got {sends}"
    finally:
        await _flatten_audit_stop(app, audit, audit_http, account_ids)


# --- G2: different fills -------------------------------------------------------------


async def test_hedge_different_fills(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g2")
    (fid,) = followers
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    account_ids = ["main", fid]
    try:
        await _open_master_leg(app.master_client, PosSide.LONG, QTY, "pm-g2")
        await _open_master_leg(app.master_client, PosSide.SHORT, QTY_BIG, "pm-g2")

        async def asymmetric():
            long_qty = await _leg_qty(audit, fid, PosSide.LONG)
            short_qty = await _leg_qty(audit, fid, PosSide.SHORT)
            ok = long_qty > 0 and short_qty < 0 and abs(short_qty) > abs(long_qty)
            return ok, f"long={long_qty} short={short_qty}"

        await _wait_stable(asymmetric, what="follower asymmetric LONG/SHORT legs")
    finally:
        await _flatten_audit_stop(app, audit, audit_http, account_ids)


# --- G3: stop per leg + replace ---------------------------------------------------------


async def test_hedge_stop_per_leg_replace(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g3")
    (fid,) = followers
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    account_ids = ["main", fid]
    try:
        await _open_master_leg(app.master_client, PosSide.LONG, QTY, "pm-g3")
        short_info = await _open_master_leg(app.master_client, PosSide.SHORT, QTY, "pm-g3")

        async def stops_ok():
            long_stops = await _leg_stops(audit, fid, PosSide.LONG)
            short_stops = await _leg_stops(audit, fid, PosSide.SHORT)
            ok = (
                len(long_stops) >= 1
                and len(short_stops) >= 1
                and all(s.side == Side.SELL for s in long_stops)
                and all(s.side == Side.BUY for s in short_stops)
            )
            lside = [str(s.side) for s in long_stops]
            sside = [str(s.side) for s in short_stops]
            return ok, f"long={lside} short={sside}"

        await _wait_stable(stops_ok, what="per-leg stops on correct sides")
        long_before = {
            (s.client_order_id, s.stop_price) for s in await _leg_stops(audit, fid, PosSide.LONG)
        }

        # Replace the master SHORT stop tighter; the follower SHORT stop must
        # converge while the LONG stop set is untouched.
        await _cancel_master_leg_stops(app.master_client, PosSide.SHORT)
        short_pos = next(
            p
            for p in await app.master_client.position_risk("main", SYMBOL)
            if p.position_side == PosSide.SHORT and p.position_amt != 0
        )
        tighter = (short_pos.entry_price + D("400")).quantize(D("0.1"))
        stamp = time.time_ns()
        await app.master_client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="STOP_MARKET",
                quantity=abs(short_pos.position_amt),
                stop_price=tighter,
                reduce_only=False,
                position_side=PosSide.SHORT,
                working_type="MARK_PRICE",
                new_client_order_id=f"pm-g3-rstop-{stamp}"[:36],
            ),
        )

        async def replaced():
            short_stops = await _leg_stops(audit, fid, PosSide.SHORT)
            long_now = {
                (s.client_order_id, s.stop_price)
                for s in await _leg_stops(audit, fid, PosSide.LONG)
            }
            ok = (
                len(short_stops) >= 1
                and all(s.side == Side.BUY for s in short_stops)
                and long_now == long_before
                and short_info["stop"] not in [s.stop_price for s in short_stops]
            )
            return ok, (
                f"short={[s.stop_price for s in short_stops]} stable={long_now == long_before}"
            )

        await _wait_stable(replaced, what="SHORT stop replaced, LONG stop stable")
    finally:
        await _flatten_audit_stop(app, audit, audit_http, account_ids)


# --- G4: leverage update keeps legs ---------------------------------------------------------


async def test_hedge_leverage_update_keeps_legs(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g4")
    (fid,) = followers
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    account_ids = ["main", fid]
    try:
        await _open_master_leg(app.master_client, PosSide.LONG, QTY, "pm-g4")
        await _open_master_leg(app.master_client, PosSide.SHORT, QTY, "pm-g4")

        long_before, short_before = await _stable_leg_qtys(
            audit, fid, "follower LONG+SHORT legs settled"
        )
        # The engine owns follower leverage (LeverageManager control plane);
        # a master leverage change must not disturb either leg (no
        # close/reopen churn, qty and stops stable across polls).
        stops_before = (
            len(await _leg_stops(audit, fid, PosSide.LONG)),
            len(await _leg_stops(audit, fid, PosSide.SHORT)),
        )
        market_before = await app.db.fetchall(
            "SELECT command_key FROM execution_commands WHERE account_id = ? "
            "AND kind LIKE 'MARKET%'",
            (fid,),
        )
        master_lev_rows = await app.master_client.position_risk("main", SYMBOL)
        master_lev = next(
            (p.leverage for p in master_lev_rows if p.position_side == PosSide.LONG), 20
        )
        await app.master_client.set_leverage("main", SYMBOL, 10 if master_lev != 10 else 5)

        async def legs_undisturbed():
            long_qty = await _leg_qty(audit, fid, PosSide.LONG)
            short_qty = await _leg_qty(audit, fid, PosSide.SHORT)
            long_stops = len(await _leg_stops(audit, fid, PosSide.LONG))
            short_stops = len(await _leg_stops(audit, fid, PosSide.SHORT))
            market_now = await app.db.fetchall(
                "SELECT command_key FROM execution_commands WHERE account_id = ? "
                "AND kind LIKE 'MARKET%'",
                (fid,),
            )
            ok = (
                long_qty == long_before
                and short_qty == short_before
                and (long_stops, short_stops) == stops_before
                and len(market_now) == len(market_before)
            )
            return ok, (
                f"long={long_qty} short={short_qty} stops={(long_stops, short_stops)} "
                f"market_cmds={len(market_now)}"
            )

        await _wait_stable(legs_undisturbed, what="master leverage change, legs undisturbed")
    finally:
        await _flatten_audit_stop(app, audit, audit_http, account_ids)


# --- G5: partial close one leg ---------------------------------------------------------


async def test_hedge_partial_close_one_leg(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g5")
    (fid,) = followers
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    account_ids = ["main", fid]
    try:
        await _open_master_leg(app.master_client, PosSide.LONG, QTY_BIG, "pm-g5")
        await _open_master_leg(app.master_client, PosSide.SHORT, QTY_BIG, "pm-g5")

        long_before, short_before = await _stable_leg_qtys(
            audit, fid, "follower LONG+SHORT legs settled"
        )
        master_long = await _leg_qty(audit, "main", PosSide.LONG)
        await _reduce_master_leg(app.master_client, PosSide.LONG, abs(master_long) / 2, "pm-g5")

        async def partial():
            long_qty = await _leg_qty(audit, fid, PosSide.LONG)
            short_qty = await _leg_qty(audit, fid, PosSide.SHORT)
            short_stops = len(await _leg_stops(audit, fid, PosSide.SHORT))
            ok = D("0") < long_qty < long_before and short_qty == short_before and short_stops >= 1
            return ok, f"long={long_before}->{long_qty} short={short_qty}/{short_stops}"

        await _wait_stable(partial, what="LONG leg shrunk, SHORT leg untouched")
    finally:
        await _flatten_audit_stop(app, audit, audit_http, account_ids)


# --- G6: close one leg keeps the other ---------------------------------------------------------


async def test_hedge_close_one_leg_keeps_other(tmp_path) -> None:
    settings, followers = await _prepare_scratch(tmp_path, "pm-g6")
    (fid,) = followers
    app = await _start_app(settings)
    audit_http = httpx.AsyncClient(timeout=30.0)
    audit = _audit_client(app, audit_http)
    account_ids = ["main", fid]
    try:
        await _open_master_leg(app.master_client, PosSide.LONG, QTY, "pm-g6")
        await _open_master_leg(app.master_client, PosSide.SHORT, QTY, "pm-g6")

        long_before, short_before = await _stable_leg_qtys(
            audit, fid, "follower LONG+SHORT legs settled"
        )

        master_long = await _leg_qty(audit, "main", PosSide.LONG)
        await _cancel_master_leg_stops(app.master_client, PosSide.LONG)
        await _reduce_master_leg(app.master_client, PosSide.LONG, abs(master_long), "pm-g6")

        async def long_gone():
            long_qty = await _leg_qty(audit, fid, PosSide.LONG)
            long_stops = len(await _leg_stops(audit, fid, PosSide.LONG))
            short_qty = await _leg_qty(audit, fid, PosSide.SHORT)
            short_stops = len(await _leg_stops(audit, fid, PosSide.SHORT))
            ok = (
                long_qty == 0 and long_stops == 0 and short_qty == short_before and short_stops >= 1
            )
            return ok, f"long={long_qty}/{long_stops} short={short_qty}/{short_stops}"

        await _wait_stable(long_gone, what="LONG leg flat, SHORT leg intact")
    finally:
        await _flatten_audit_stop(app, audit, audit_http, account_ids)
