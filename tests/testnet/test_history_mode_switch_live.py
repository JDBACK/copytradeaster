"""Live acceptance: a REAL HEDGE <-> ONE_WAY switch must not lose history.

Merge-review corrective P1-1 fixed a projection that reused the retired
book's seeds after an accepted boundary: the first round trip of the new book
(positionSide=BOTH in ONE_WAY) was dropped, while the boundary still advanced
past its fills and the dirty range was consumed. This scenario switches
copyc2 on the REAL testnet HEDGE -> ONE_WAY -> HEDGE, drives the production
import/capture seam after every round trip, and proves on the exchange's own
fills that:

  * every book publishes its own episode and keeps its public ref,
  * the boundary advances to the book the ending snapshot describes,
  * the dirty range is consumed only together with a published replay,
  * every imported fill in the window is allocated to an episode.

Testnet funds and position mode are disposable, so the scenario mutates the
account's mode on purpose; finally restores the original mode and leaves the
account flat.

Run: CT_TESTNET=1 pytest tests/testnet/test_history_mode_switch_live.py -v

MODE_SWITCH_ACCOUNT selects the signed account (default copyc2) so the same
scenario can run on an idle follower without touching an account another
harness traded seconds ago; MODE_SWITCH_QUIET_MS overrides the 5-minute
recent-trade preflight for back-to-back harness runs.

Each book is accepted only after a quiet stretch longer than the importer's
60 s overlap (QUIET_GAP_S). The importer re-reads the last 60 s before the
published frontier, so a fill of the retired book inside that window alone
re-triggers the late-fact replay and hides the defect; the production database
hit it in exactly the shape this gap reproduces, where the incoming book's
batch carries no fill of the book that was just retired.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import HistoryReader, Window
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    NonceStore,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]

ACCOUNT = os.environ.get("MODE_SWITCH_ACCOUNT", "copyc2")
SYMBOL = "BTCUSDT"
QTY = D("0.001")
POLL_TIMEOUT_S = 90.0
CAPTURE_TIMEOUT_S = 60.0
RECENT_TRADES_MS = int(os.environ.get("MODE_SWITCH_QUIET_MS", "300000"))
MIN_FREE_USDT = D("100")
PNL_TOLERANCE = D("0.00000001")
QUIET_GAP_S = 65.0


def _leg_qtys(positions) -> dict[str, D]:
    return {str(row.position_side): D(str(row.position_amt)) for row in positions}


async def _wait_flat(client: AsterClient, timeout_s: float = POLL_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        qtys = _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL))
        if all(qty == 0 for qty in qtys.values()):
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"position did not settle flat: {qtys}")
        await asyncio.sleep(2.0)


async def _wait_qty(client: AsterClient, expected: D, timeout_s: float = POLL_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        qtys = _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL))
        total = sum((abs(qty) for qty in qtys.values()), D("0"))
        if total == expected:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"position did not settle at {expected}: {qtys}")
        await asyncio.sleep(2.0)


def _unknown_outcome(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, AsterError):
        return False
    if exc.code in {-1000, -1006, -1007} or exc.http_status in {503, 504}:
        return True
    return exc.code == 0 and "timeout" in exc.msg.lower()


async def _place(client: AsterClient, req: PlaceOrderRequest):
    """Signed order with unknown-outcome query recovery (never blind-retry)."""
    for attempt in range(3):
        try:
            return await client.place_order(ACCOUNT, req)
        except Exception as exc:  # noqa: BLE001 - classify unknown outcomes only
            if not _unknown_outcome(exc) or attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
            found = await client.query_order(
                ACCOUNT, req.symbol, orig_client_order_id=req.new_client_order_id
            )
            if found is not None:
                return found
    raise RuntimeError("unreachable")


async def _flatten(client: AsterClient, hedge: bool) -> None:
    for order in await client.open_orders(ACCOUNT, SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order(ACCOUNT, SYMBOL, orig_client_order_id=order.client_order_id)
    for index, row in enumerate(await client.position_risk(ACCOUNT, SYMBOL)):
        qty = D(str(row.position_amt))
        if qty == 0:
            continue
        stamp = int(time.time() * 1000)
        await _place(
            client,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL if qty > 0 else Side.BUY,
                type="MARKET",
                quantity=abs(qty),
                reduce_only=not hedge,
                position_side=row.position_side,
                new_order_resp_type="RESULT",
                new_client_order_id=f"msw-flat-{stamp}-{index}"[:36],
            ),
        )


@pytest.fixture
async def live(tmp_path: Path):
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail("signed tests are restricted to testnet")
    # Credentials are read connect-only (never migrated here). The shared
    # nonce counter advances by design: the exchange rejects reuse (-4226)
    # while a later bot restart safely continues the same counter.
    creds_db = Database(settings.db_path)
    await creds_db.connect()
    history_db = Database(tmp_path / "mode-switch.sqlite3")
    await history_db.connect()
    await history_db.migrate()
    try:
        creds = SqliteCredentials(creds_db, settings.encryption_key_bytes())
        await creds.load()
        if ACCOUNT not in creds.account_ids():
            pytest.skip(f"{ACCOUNT} credentials are not imported")
        http = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        limiter = RateLimits(
            settings.weight_limit_per_min,
            settings.orders_limit_per_min,
            settings.orders_limit_per_10s,
        )
        client = AsterClient(
            http, settings.rest_base_url, limiter, creds, creds, NonceStore(creds_db)
        )
        try:
            yield settings, client, history_db
        finally:
            await http.aclose()
    finally:
        await history_db.close()
        await creds_db.close()


async def test_real_mode_switch_publishes_both_books(live):
    _settings, client, history_db = live

    original_hedge = await client.get_position_mode(ACCOUNT)
    if any(qty != 0 for qty in _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL)).values()):
        pytest.skip(f"{ACCOUNT} is not flat on {SYMBOL}")
    if await client.open_orders(ACCOUNT, SYMBOL):
        pytest.skip(f"{ACCOUNT} has open orders on {SYMBOL}: possibly managed elsewhere")
    probe_now = int(time.time() * 1000)
    recent = await client.user_trades(
        ACCOUNT, SYMBOL, start_time=probe_now - RECENT_TRADES_MS, end_time=probe_now
    )
    if recent:
        pytest.skip(f"{ACCOUNT} traded in the last 5 minutes: possibly managed elsewhere")
    balances = await client.balance(ACCOUNT)
    free_usdt = next((D(str(b.available_balance)) for b in balances if b.asset == "USDT"), D("0"))
    if free_usdt < MIN_FREE_USDT:
        pytest.skip(f"{ACCOUNT} testnet faucet is too low ({free_usdt} USDT free)")

    await AccountRepo(history_db).upsert(ACCOUNT, "follower", ACCOUNT, "0x" + "2" * 40)
    commands = CommandRepo(history_db)
    scope = HistoryScope(ACCOUNT, "testnet")
    history = HistoryRepository(history_db)
    episodes = EpisodeRepository(history_db)
    events = EventRepository(history_db)
    synchronizer = HistorySynchronizer(HistoryReader(client), history)
    capture = HistoryCapture(
        history,
        episodes,
        events=events,
        commands=commands,
        projection=HistoryProjection(history, episodes, events, commands=commands),
    )
    t0 = int(time.time() * 1000) - 120_000

    async def set_mode(hedge: bool) -> None:
        if await client.get_position_mode(ACCOUNT) is hedge:
            return
        try:
            await client.set_position_mode(ACCOUNT, hedge)
        except AsterError as exc:
            # The exchange refuses a no-op switch with -4059; a concurrent
            # writer may also have already applied the requested mode.
            if exc.code != -4059 and await client.get_position_mode(ACCOUNT) is not hedge:
                raise
        deadline = time.monotonic() + 30.0
        while await client.get_position_mode(ACCOUNT) is not hedge:
            if time.monotonic() > deadline:
                raise TimeoutError(f"position mode did not switch to hedge={hedge}")
            await asyncio.sleep(1.0)

    async def round_trip(tag: str, pside: PosSide, hedge: bool) -> None:
        stamp = int(time.time() * 1000)
        open_side = Side.BUY if pside in {PosSide.LONG, PosSide.BOTH} else Side.SELL
        close_side = Side.SELL if open_side is Side.BUY else Side.BUY
        await _place(
            client,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=open_side,
                type="MARKET",
                quantity=QTY,
                position_side=pside,
                new_order_resp_type="RESULT",
                new_client_order_id=f"msw-{tag}-o-{stamp}"[:36],
            ),
        )
        await _wait_qty(client, QTY)
        await _place(
            client,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=close_side,
                type="MARKET",
                quantity=QTY,
                reduce_only=not hedge,
                position_side=pside,
                new_order_resp_type="RESULT",
                new_client_order_id=f"msw-{tag}-c-{stamp}"[:36],
            ),
        )
        await _wait_flat(client)

    async def capture_until(predicate, *, timeout_s: float = CAPTURE_TIMEOUT_S):
        deadline = time.monotonic() + timeout_s
        last: tuple = ()
        while True:
            last, _published = await capture.capture_and_publish(
                scope,
                SYMBOL,
                Window(t0, int(time.time() * 1000)),
                snapshots=client,
                synchronizer=synchronizer,
                notify_eligible=True,
            )
            if last and predicate(last):
                return last
            if time.monotonic() > deadline:
                raise TimeoutError(f"capture did not prove the step: {last}")
            await asyncio.sleep(3.0)

    async def fills_between(start_ms: int, end_ms: int) -> list:
        return await history_db.fetchall(
            "SELECT exchange_trade_id, position_side, side, qty, realized_pnl, executed_at "
            "FROM follower_fills WHERE account_id=? AND environment=? AND symbol=? "
            "AND executed_at BETWEEN ? AND ? ORDER BY executed_at, exchange_trade_id",
            (ACCOUNT, scope.environment, SYMBOL, start_ms, end_ms),
        )

    async def unallocated_between(start_ms: int, end_ms: int) -> list:
        """Fills inside the window with no allocation: the P1-1 signature."""
        return await history_db.fetchall(
            "SELECT f.exchange_trade_id, f.position_side, f.realized_pnl FROM follower_fills f "
            "WHERE f.account_id=? AND f.environment=? AND f.symbol=? "
            "AND f.executed_at BETWEEN ? AND ? AND NOT EXISTS ("
            "SELECT 1 FROM follower_episode_allocations a WHERE a.account_id=f.account_id "
            "AND a.environment=f.environment AND a.symbol=f.symbol "
            "AND a.exchange_trade_id=f.exchange_trade_id) "
            "ORDER BY f.executed_at, f.exchange_trade_id",
            (ACCOUNT, scope.environment, SYMBOL, start_ms, end_ms),
        )

    def pick(stored, side: PosSide, *, exclude_ref: str | None = None):
        matches = [
            stored_episode.episode
            for stored_episode in stored
            if stored_episode.episode.position_side is side
            and stored_episode.episode.closed_at is not None
            and stored_episode.episode.public_ref != exclude_ref
        ]
        assert matches, f"no closed {side.value} episode published: {stored}"
        return max(matches, key=lambda episode: episode.closed_at or 0)

    try:
        await set_mode(True)

        # --- book 1: HEDGE LONG round trip, accepted boundary ---------------- #
        hedge_mark = int(time.time() * 1000)
        await round_trip("h1", PosSide.LONG, True)
        # Quiet tail first: the 60 s import overlap must not reach back into
        # this book's fills when the next book is imported.
        await asyncio.sleep(QUIET_GAP_S)
        stored = await capture_until(
            lambda rows: any(
                row.episode.position_side is PosSide.LONG and row.episode.closed_at is not None
                for row in rows
            )
        )
        hedge_episode = pick(stored, PosSide.LONG)
        hedge_ref = hedge_episode.public_ref
        hedge_fills = await fills_between(hedge_mark, int(time.time() * 1000))
        assert hedge_fills, "no HEDGE fills were imported"
        assert {row["position_side"] for row in hedge_fills} == {"LONG"}
        hedge_sum = sum((D(row["realized_pnl"]) for row in hedge_fills), D("0"))
        assert abs(hedge_episode.realized_pnl_gross - hedge_sum) <= PNL_TOLERANCE
        latest = await episodes.load_boundary(scope, SYMBOL)
        assert latest is not None
        assert {leg.value for leg in latest.seed_map()} == {"LONG", "SHORT"}

        # --- switch the REAL book: HEDGE -> ONE_WAY --------------------------- #
        await set_mode(False)
        assert await client.get_position_mode(ACCOUNT) is False

        one_way_mark = int(time.time() * 1000)
        await round_trip("w1", PosSide.BOTH, False)
        stored = await capture_until(
            lambda rows: any(
                row.episode.position_side is PosSide.BOTH and row.episode.closed_at is not None
                for row in rows
            )
        )
        one_way_episode = pick(stored, PosSide.BOTH)
        one_way_fills = await fills_between(one_way_mark, int(time.time() * 1000))
        assert one_way_fills, "no ONE_WAY fills were imported"
        assert {row["position_side"] for row in one_way_fills} == {"BOTH"}
        one_way_sum = sum((D(row["realized_pnl"]) for row in one_way_fills), D("0"))
        assert abs(one_way_episode.realized_pnl_gross - one_way_sum) <= PNL_TOLERANCE

        latest = await episodes.load_boundary(scope, SYMBOL)
        assert latest is not None
        assert {leg.value for leg in latest.seed_map()} == {"BOTH"}
        assert latest.cutoff_ms >= max(int(row["executed_at"]) for row in one_way_fills)
        revision, dirty_from = await history.projection_state(scope, SYMBOL)
        assert revision > 0
        assert dirty_from is None, "the new book advanced dirty without publishing it"
        # The retired book keeps its published episode and its ref.
        retired = await episodes.get(scope, hedge_ref)
        assert retired is not None
        assert retired.episode.closed_at == hedge_episode.closed_at
        assert retired.episode.realized_pnl_gross == hedge_episode.realized_pnl_gross
        unallocated = await unallocated_between(t0, int(time.time() * 1000))
        assert not unallocated, f"fills without an episode allocation: {unallocated}"

        # Re-accept the ONE_WAY book so its own fills leave the import
        # overlap before the next book arrives for the same reason.
        await asyncio.sleep(QUIET_GAP_S)
        stored = await capture_until(lambda rows: bool(rows))
        assert stored, "the accepted ONE_WAY frontier disappeared"

        # --- switch back: ONE_WAY -> HEDGE, both books coexist ---------------- #
        await set_mode(True)
        hedge2_mark = int(time.time() * 1000)
        await round_trip("h2", PosSide.LONG, True)
        stored = await capture_until(
            lambda rows: any(
                row.episode.position_side is PosSide.LONG
                and row.episode.public_ref != hedge_ref
                and row.episode.closed_at is not None
                for row in rows
            )
        )
        hedge2 = pick(stored, PosSide.LONG, exclude_ref=hedge_ref)
        assert hedge2.closed_at is not None and hedge2.closed_at >= hedge2_mark - 5_000
        latest = await episodes.load_boundary(scope, SYMBOL)
        assert latest is not None
        assert {leg.value for leg in latest.seed_map()} == {"LONG", "SHORT"}
        revision, dirty_from = await history.projection_state(scope, SYMBOL)
        assert dirty_from is None
        one_way_retired = await episodes.get(scope, one_way_episode.public_ref)
        assert one_way_retired is not None
        assert one_way_retired.episode.realized_pnl_gross == one_way_episode.realized_pnl_gross
        unallocated = await unallocated_between(t0, int(time.time() * 1000))
        assert not unallocated, f"fills without an episode allocation: {unallocated}"
    finally:
        with contextlib.suppress(Exception):
            await _flatten(client, bool(await client.get_position_mode(ACCOUNT)))
        with contextlib.suppress(Exception):
            await _wait_flat(client, timeout_s=30.0)
        with contextlib.suppress(Exception):
            await set_mode(original_hedge)
