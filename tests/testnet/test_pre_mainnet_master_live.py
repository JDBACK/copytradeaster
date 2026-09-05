"""Package B: Snapshot / WS / Startup live matrix (pre-mainnet, REAL testnet only).

Live-only qualification: every test boots the PRODUCTION App on a scratch DB
(seeded from ``data/copytrade.sqlite3`` accounts+credentials, never the prod
file itself), drives the REAL master user stream (``fstream.asterdex-testnet.com``)
and REAL signed REST (``fapi.asterdex-testnet.com``) with real ``wallet.txt``
accounts, and ends with best-effort flatten + an independent signed flat audit
(``position_risk`` flat + ``open_orders`` empty) in ``finally``.

Row coverage (Snapshot / WS / Startup qualification rows):
- S1 ``test_rebuild_holds_queued_trade_no_double_apply``: a live TRADE queued
  while a real REST rebuild is in flight keeps its pre-boundary generation and
  cannot double-apply; the snapshot covers the fill exactly once.
- S2 ``test_delayed_stale_trade_post_snapshot_no_mutation``: a real WS TRADE
  frame, capture-delayed past a newer authoritative snapshot (local receipt
  erased to simulate transport loss + late redelivery; replayed bytes are the
  unmodified real frame), hits the ``stale-event-after-snapshot`` cutoff:
  no mutation, no duplicate episode, recovery converges to REST.
- W1 ``test_reconnect_gap_fill_converges_no_duplicates``: forced
  ``close_listen_key`` transport drop, a real MARKET+STOP fill committed in the
  gap, resnapshot converges with no duplicate follower qty / Stop.
- W2 ``test_failed_resnapshot_closes_admission_then_recovers_to_rest``: a
  one-shot failure injected around the real ``AsterClient`` (``balance``) fails
  the boundary -> entry admission closed (``ready=false``); after recovery the
  final state equals real REST.
- P1 ``test_startup_transient_master_change_no_stale_follower_entry``: slow
  startup reconcile with a real master open+close between the initial snapshot
  and WS READY -> no stale entry on REAL followers.

Offline/fake NEVER closes a row: no mocks, no synthetic fills, no shadow
execution. Faults are injected only around the real ``AsterClient`` / real WS
delivery (``close_listen_key``, one-shot REST failure, delayed redelivery of a
real frame). NOBODY runs live pytest from shared-state checkouts: these tests
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


async def _open_master_long(client, position_side: PosSide, tag: str) -> dict:
    """REAL master MARKET open + protective STOP_MARKET. Returns ids for cleanup."""
    stamp = time.time_ns()
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=QTY,
            position_side=position_side,
            new_client_order_id=f"{tag}-open-{stamp}"[:36],
            new_order_resp_type="RESULT",
        ),
    )
    position = next(p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0)
    stop_level = (position.entry_price - D("1200")).quantize(D("0.1"))
    hedge = position_side != PosSide.BOTH
    master_stop = await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=QTY,
            stop_price=stop_level,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"{tag}-stop-{stamp}"[:36],
        ),
    )
    return {"position_side": position_side, "stop_coid": master_stop.client_order_id}


async def _close_master_long(client, position_side: PosSide, tag: str) -> None:
    qty = abs(await _symbol_qty(client, "main"))
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


async def _flatten_account(client, account_id: str) -> None:
    """Best-effort cancel + close for one account (mirrors _flatten_main, generalized)."""
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


async def _start_app(settings: Settings) -> App:
    app = App(settings)
    await app.start()
    assert app._ready and app._stream_connect_count >= 1
    assert await _ready_flag(app)
    return app


async def _wait_followers_protected(app: App, followers: list[str], timeout: float = 120.0) -> None:
    async def protected() -> bool:
        for account_id in followers:
            if await _symbol_qty(app.master_client, account_id) == 0:
                return False
            if len(await _follower_stops(app.master_client, account_id)) != 1:
                return False
        return True

    await _wait_for(protected, timeout, "followers protected from real WS event")


async def _wait_followers_settled(
    app: App, followers: list[str], polls: int = 3, interval: float = 2.0, timeout: float = 60.0
) -> None:
    """Require follower qty unchanged AND exactly one stop across N polls.

    _wait_followers_protected returns on the first poll with qty != 0, which
    can snapshot a partial fill (or a background-trim transient). Snapshotting
    qty_before only after the copy has settled removes that flake.
    """
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


# --- S1: queued TRADE during a real REST rebuild ---------------------------------


async def test_rebuild_holds_queued_trade_no_double_apply(tmp_path):
    """S1: live TRADE held in the ingest queue across a real REST rebuild.

    The frame keeps its pre-boundary generation, so the completed snapshot is
    never mutated by it; the authoritative REST read covers the fill exactly
    once (one master episode, one follower Stop each).
    """
    settings, followers = await _prepare_scratch(tmp_path, "master_b1", max_followers=2)
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

        # Capture the real WS TRADE frame for the receipt assertion below.
        seen: list[dict] = []
        original_enqueue = app._enqueue

        def _tap(raw: dict, *, received_at: float | None = None):
            if isinstance(raw, dict) and raw.get("e") == "ORDER_TRADE_UPDATE":
                order = raw.get("o", {})
                if (
                    isinstance(order, dict)
                    and order.get("s") == SYMBOL
                    and order.get("x") == "TRADE"
                ):
                    seen.append(dict(raw))
            return original_enqueue(raw, received_at=received_at)

        app._enqueue = _tap  # type: ignore[method-assign]
        try:
            gen_before = app.projector.generation
            position_side = await _position_side(client)
            boundary = asyncio.create_task(app._establish_master_boundary("pre-mainnet-b1-hold"))
            await asyncio.sleep(1.0)  # let the REST snapshot reads go in flight
            opened = await _open_master_long(client, position_side, "pm-b1")
            await boundary
        finally:
            app._enqueue = original_enqueue  # type: ignore[method-assign]

        await _wait_followers_protected(app, followers)
        await _wait_for(lambda: _ready_flag(app), 120, "READY after held-trade boundary")

        # The live TRADE arrived (receipt) yet applied exactly once.
        assert seen, "no real WS TRADE frame observed for the master fill"
        assert app.projector.generation > gen_before
        assert await _copy_trade_count(app, "OPEN") == 1
        for account_id in followers:
            assert await _symbol_qty(client, account_id) != 0
            assert len(await _follower_stops(client, account_id)) == 1

        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
        await _close_master_long(client, position_side, "pm-b1")
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


# --- S2: delayed stale TRADE past the snapshot cutoff ------------------------------


async def test_delayed_stale_trade_post_snapshot_no_mutation(tmp_path):
    """S2: delayed real TRADE redelivered after a newer snapshot -> cutoff.

    The replayed bytes are the unmodified real WS frame; only its local ledger
    receipt is erased (simulating transport loss + late redelivery, a real
    network condition). The ``stale-event-after-snapshot`` cutoff must discard
    it: no double-count, no new episode, recovery converges to REST.
    """
    settings, followers = await _prepare_scratch(tmp_path, "master_b2", max_followers=1)
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

        seen: list[dict] = []
        original_enqueue = app._enqueue

        def _tap(raw: dict, *, received_at: float | None = None):
            if isinstance(raw, dict) and raw.get("e") == "ORDER_TRADE_UPDATE":
                order = raw.get("o", {})
                if (
                    isinstance(order, dict)
                    and order.get("s") == SYMBOL
                    and order.get("x") == "TRADE"
                ):
                    seen.append(dict(raw))
            return original_enqueue(raw, received_at=received_at)

        app._enqueue = _tap  # type: ignore[method-assign]
        try:
            position_side = await _position_side(client)
            opened = await _open_master_long(client, position_side, "pm-b2")

            async def _captured() -> bool:
                return len(seen) > 0

            await _wait_for(_captured, 60, "real WS TRADE frame")
        finally:
            app._enqueue = original_enqueue  # type: ignore[method-assign]

        await _wait_followers_protected(app, followers)
        await _wait_followers_settled(app, followers)
        follower = followers[0]
        qty_before = await _symbol_qty(client, follower)
        stops_before = len(await _follower_stops(client, follower))
        trades_before = await _copy_trade_count(app)

        raw = seen[0]
        order = raw["o"]
        event_time = int(raw.get("E", 0))
        assert event_time > 0, "real TRADE frame carries no exchange timestamp"
        dedup_key = (
            f"ORDER_TRADE_UPDATE:{event_time}:{order.get('i')}:{order.get('x')}:{order.get('t')}"
        )
        # Simulate transport loss of this exact delivery; the bytes stay real.
        await app.db.execute_write("DELETE FROM master_events WHERE dedup_key = ?", (dedup_key,))

        # A newer authoritative snapshot moves the cutoff past the frame's E.
        await app.projector.rebuild_full(client, "main")
        assert event_time <= app.projector._authoritative_cutoff_ms

        # Late redelivery with the CURRENT generation: cutoff must discard it.
        await app.projector.apply_raw(dict(raw), generation=app.projector.generation)

        # No mutation, proven stable: every sub-condition must hold across
        # consecutive polls (a live App never truly quiesces single-shot).
        async def _s2_no_mutation() -> tuple[bool, str]:
            failures = []
            proj_qty = _projector_symbol_qty(app)
            rest_qty = await _symbol_qty(client, "main")
            if proj_qty != rest_qty:
                failures.append(f"projector {proj_qty} != REST {rest_qty}")
            count = await _copy_trade_count(app)
            if count != trades_before:
                failures.append(f"copy_trades {count} != {trades_before}")
            fqty = await _symbol_qty(client, follower)
            if fqty != qty_before:
                failures.append(f"follower {follower} {fqty} != {qty_before}")
            fstops = len(await _follower_stops(client, follower))
            if fstops != stops_before:
                failures.append(f"stops {fstops} != {stops_before}")
            if not await _ready_flag(app):
                failures.append("ready flag != true")
            return (not failures, "; ".join(failures))

        await _wait_stable(_s2_no_mutation, what="S2 no-mutation")

        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
        await _close_master_long(client, position_side, "pm-b2")
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


# --- W1: reconnect with a fill committed in the gap ---------------------------------


async def test_reconnect_gap_fill_converges_no_duplicates(tmp_path):
    """W1: forced listenKey drop, real fill in the gap, resnapshot converges.

    Events missed while the transport is down are covered by the reconnect
    boundary; followers open exactly once (single qty application, one Stop).
    """
    settings, followers = await _prepare_scratch(tmp_path, "master_b3", max_followers=2)
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

        reconnects_before = app._stream_reconnect_count
        await client.close_listen_key("main")

        async def _reconnected() -> bool:
            return app._stream_reconnect_count > reconnects_before

        await _wait_for(_reconnected, 90, "master user stream reconnect")

        # Commit the real fill while the stream/boundary is still converging.
        position_side = await _position_side(client)
        opened = await _open_master_long(client, position_side, "pm-b3")

        await _wait_followers_protected(app, followers)
        await _wait_for(lambda: _ready_flag(app), 120, "READY after reconnect boundary")

        # Convergence, proven stable across consecutive polls (see _wait_stable).
        async def _w1_converged() -> tuple[bool, str]:
            failures = []
            open_count = await _copy_trade_count(app, "OPEN")
            if open_count != 1:
                failures.append(f"OPEN episodes {open_count} != 1")
            proj_qty = _projector_symbol_qty(app)
            rest_qty = await _symbol_qty(client, "main")
            if proj_qty != rest_qty:
                failures.append(f"projector {proj_qty} != REST {rest_qty}")
            for account_id in followers:
                fqty = await _symbol_qty(client, account_id)
                if fqty == 0:
                    failures.append(f"{account_id} flat")
                fstops = len(await _follower_stops(client, account_id))
                if fstops != 1:
                    failures.append(f"{account_id} stops {fstops} != 1")
            return (not failures, "; ".join(failures))

        await _wait_stable(_w1_converged, what="W1 convergence")

        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
        await _close_master_long(client, position_side, "pm-b3")
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


# --- W2: failed resnapshot closes admission, recovery equals REST ----------------------


async def test_failed_resnapshot_closes_admission_then_recovers_to_rest(tmp_path):
    """W2: one-shot REST failure around the real client fails the boundary.

    Entry admission must close (``ready=false``) rather than admit on a stale
    view; after the transport heals, the recovery boundary converges exactly to
    real REST (qty + protective level), with no duplicated follower state.
    """
    settings, followers = await _prepare_scratch(tmp_path, "master_b4", max_followers=2)
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
        opened = await _open_master_long(client, position_side, "pm-b4")
        await _wait_followers_protected(app, followers)

        original_balance = client.balance
        calls = 0

        async def _flaky_balance(account_id: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected-resnapshot-failure")
            return await original_balance(account_id)

        client.balance = _flaky_balance  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="injected-resnapshot-failure"):
                await app._establish_master_boundary("pre-mainnet-b4-injected")
        finally:
            client.balance = original_balance  # type: ignore[method-assign]
        assert calls == 1

        # Admission closed, fail-closed on the stale view.
        assert not app.ready
        assert await app.settings.get_flag("ready") == "false"
        assert not app._master_entry_admitted()

        recovered = await app._establish_master_boundary("pre-mainnet-b4-recovery")
        assert recovered
        await _wait_for(lambda: _ready_flag(app), 120, "READY after recovery boundary")

        # Recovery equals REST, proven stable across consecutive polls (see _wait_stable).
        async def _w2_matches_rest() -> tuple[bool, str]:
            failures = []
            proj_qty = _projector_symbol_qty(app)
            rest_qty = await _symbol_qty(client, "main")
            if not (proj_qty == rest_qty != 0):
                failures.append(f"projector {proj_qty} vs REST {rest_qty} (expected equal nonzero)")
            rest_stops = [
                order
                for order in await client.open_orders("main", SYMBOL)
                if order.type == "STOP_MARKET"
            ]
            if len(rest_stops) != 1:
                failures.append(f"REST stops {len(rest_stops)} != 1")
            nonzero_keys = [
                key
                for key, position in app.projector.state.positions.items()
                if key.symbol == SYMBOL and position.qty != 0
            ]
            if len(nonzero_keys) != 1:
                failures.append(f"projector nonzero keys {len(nonzero_keys)} != 1")
            elif len(rest_stops) == 1:
                projector_stop = app.projector.state.positions[nonzero_keys[0]].stop
                if projector_stop is None:
                    failures.append("projector stop is None")
                elif projector_stop.stop_price != rest_stops[0].stop_price:
                    failures.append(
                        f"stop {projector_stop.stop_price} != REST {rest_stops[0].stop_price}"
                    )
            for account_id in followers:
                fqty = await _symbol_qty(client, account_id)
                if fqty == 0:
                    failures.append(f"{account_id} flat")
                fstops = len(await _follower_stops(client, account_id))
                if fstops != 1:
                    failures.append(f"{account_id} stops {fstops} != 1")
            return (not failures, "; ".join(failures))

        await _wait_stable(_w2_matches_rest, what="W2 recovery-equals-REST")

        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
        await _close_master_long(client, position_side, "pm-b4")
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


# --- P1: transient master change during slow startup ---------------------------------


async def test_startup_transient_master_change_no_stale_follower_entry(tmp_path):
    """P1: real master open+close between the initial snapshot and WS READY.

    The startup boundary must coalesce the transient (quiescence + second REST
    cutover) so REAL followers never take a stale entry for a master position
    that no longer exists: no episodes, no links, followers flat.
    """
    # Phase 1: flatten through a first boot so the restart begins flat.
    settings1, followers = await _prepare_scratch(tmp_path, "master_b5a", max_followers=2)
    app1 = App(settings1)
    audit_http1 = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app1.start()
        for account_id in ["main", *followers]:
            await _flatten_account(app1.master_client, account_id)
        await _wait_for(lambda: _ready_flag(app1), 90, "READY after pre-flatten")
    finally:
        with contextlib.suppress(Exception):
            for account_id in ["main", *followers]:
                await _flatten_account(app1.master_client, account_id)
        with contextlib.suppress(Exception):
            await app1.stop()
        with contextlib.suppress(Exception):
            await audit_http1.aclose()

    # Phase 2: slow restart on a FRESH scratch DB; transient fires mid-boundary.
    settings2, followers2 = await _prepare_scratch(tmp_path, "master_b5b", max_followers=2)
    assert [fid for fid in followers2] == [fid for fid in followers][: len(followers2)]
    app2 = App(settings2)
    started = False
    audit_http2 = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    audit_http2_client = None  # bound once READY; finally falls back to master_client
    try:
        start_task = asyncio.create_task(app2.start())

        async def _snapshot_done_not_ready() -> bool:
            return app2.projector.generation >= 1 and not app2.ready

        await _wait_for(_snapshot_done_not_ready, 120, "startup boundary window", interval=0.2)
        if app2.ready:  # pragma: no cover - window already closed
            pytest.skip("startup boundary reached READY before the transient fired")
        # Real transient: open, hold briefly mid-boundary, then flatten master.
        position_side = await _position_side(app2.master_client)
        await app2.master_client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=QTY,
                position_side=position_side,
                new_client_order_id=f"pm-b5-open-{time.time_ns()}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        await asyncio.sleep(2.0)
        await _close_master_long(app2.master_client, position_side, "pm-b5")
        await start_task
        started = True
        assert app2._ready
        audit_http2_client = _audit_client(app2, audit_http2)
        await _wait_for(lambda: _ready_flag(app2), 120, "READY after slow startup")

        # No stale entry: no episodes ever opened, no links, followers flat.
        assert await _copy_trade_count(app2) == 0
        link_rows = await app2.db.fetchall("SELECT id FROM order_links", ())
        assert len(link_rows) == 0
        await _assert_flat(audit_http2_client, ["main", *followers2])
    finally:
        if started:
            audit = audit_http2_client if audit_http2_client is not None else app2.master_client
            await _flatten_audit_stop(app2, audit, audit_http2, ["main", *followers2])
        else:  # pragma: no cover - restart never reached READY
            with contextlib.suppress(Exception):
                await app2.stop()
            with contextlib.suppress(Exception):
                await audit_http2.aclose()
