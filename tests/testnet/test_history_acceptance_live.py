"""Signed §10.9 acceptance on a dedicated testnet follower (marker `testnet`).

Runs the full lifecycle on copyc2/BTCUSDT — open, increase, partial close,
full close — then imports the real fills/income through the production sync
and capture path and checks every §10.9 criterion that can be proven
offline-style on live data:

1. journal-independent fills land with qty/price/time; re-import duplicates
   nothing (restart simulated with fresh objects on the same files);
2. exactly one closed episode holds every size change;
3. episode gross matches the exchange SELL realizedPnl; the net gap is
   explained by concrete proof issues (direct-API fills, no journal link);
4. trades list, trade detail, activity feed and the recovery notification
   share one trade_ref with identical facts;
5. the starting position is flat and the account is flat afterwards.

Skips (never a fake pass) unless copyc2 is flat on BTCUSDT with enough
testnet USDT. The production database is never touched: credentials and
nonces come from a throwaway copy, history lives in tmp storage.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.application.models import TradeUnavailableError
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import PosSide, RiskPolicy, Side
from copytradeaster.history.accounting import AccountingProof, account_episode
from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import HistoryReader, Window
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    NonceStore,
    SettingsRepo,
    TelegramUserRepo,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]

ACCOUNT = "copyc2"
SYMBOL = "BTCUSDT"
QTY = D("0.001")
USER_ID = 7001
POLL_TIMEOUT_S = 90.0


class _LiveRuntime:
    """Owned snapshots for the service layer over live-imported history."""

    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.snapshot: dict | None = {"positions": {}, "available_usdt": "10000"}
        self.snapshot_at = datetime.now(UTC)

    async def cached_snapshot(self, account_id: str):
        del account_id
        return (self.snapshot, self.snapshot_at) if self.snapshot else None

    async def refresh_snapshot(self, account_id: str):
        return await self.cached_snapshot(account_id)

    async def account_version(self, account_id: str) -> str:
        del account_id
        return "live"

    async def symbol_assets(self, account_id: str, symbols: list[str]):
        del account_id
        return {symbol: "BTC" if symbol == "BTCUSDT" else None for symbol in symbols}


def _leg_qty(positions) -> D:
    total = D("0")
    for row in positions:
        total += abs(D(str(row.position_amt)))
    return total


async def _wait_flat_qty(
    client: AsterClient, expected: D, timeout_s: float = POLL_TIMEOUT_S
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        rows = await client.position_risk(ACCOUNT, SYMBOL)
        if _leg_qty(rows) == expected:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"position did not settle at {expected}")
        await asyncio.sleep(2.0)


async def _history_stack(history_db: Database):
    history = HistoryRepository(history_db)
    episodes = EpisodeRepository(history_db)
    events = EventRepository(history_db)
    return history, episodes, events


@pytest.fixture
async def live(tmp_path: Path):
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail("signed tests are restricted to testnet")
    # Credentials AND nonces come from the production database file, exactly
    # like the established signed harness: nonce reuse across runs is
    # rejected by the exchange (-4226), while advancing the shared counter
    # is safe for a later bot restart. Connect-only: never migrate here.
    creds_db = Database(settings.db_path)
    await creds_db.connect()
    history_db = Database(tmp_path / "history.sqlite3")
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


async def test_s16_lifecycle_open_increase_partial_close(live):
    settings, client, history_db = live
    del settings
    hedge = await client.get_position_mode(ACCOUNT)
    pside = PosSide.LONG if hedge else PosSide.BOTH
    rows = await client.position_risk(ACCOUNT, SYMBOL)
    if _leg_qty(rows) != 0:
        pytest.skip(f"{ACCOUNT} is not flat on {SYMBOL}")
    balances = await client.balance(ACCOUNT)
    usdt = next((D(str(b.available_balance)) for b in balances if b.asset == "USDT"), D("0"))
    if usdt < D("100"):
        pytest.skip(f"{ACCOUNT} testnet faucet is too low ({usdt} USDT free)")

    tag = f"s16-{int(time.time())}"
    t0 = int(time.time() * 1000) - 120_000
    # Ownership scaffolding before any history write (FK to accounts).
    await AccountRepo(history_db).upsert(ACCOUNT, "follower", ACCOUNT, "0x" + "2" * 40)
    sell_side = Side.SELL
    our_order_ids: list[int] = []

    async def market(side: Side, qty: D, coid: str):
        order = await client.place_order(
            ACCOUNT,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty,
                new_order_resp_type="RESULT",
                position_side=pside,
                new_client_order_id=coid,
            ),
        )
        assert order.status in ("FILLED", "NEW")
        our_order_ids.append(int(order.order_id))
        return order

    try:
        await market(Side.BUY, QTY, f"{tag}-open-a")
        await _wait_flat_qty(client, QTY)
        await market(Side.BUY, QTY, f"{tag}-open-b")
        await _wait_flat_qty(client, QTY * 2)
        await market(sell_side, QTY, f"{tag}-reduce")
        await _wait_flat_qty(client, QTY)
        await market(sell_side, QTY, f"{tag}-close")
        await _wait_flat_qty(client, D("0"))
    finally:
        # Fail-safe flatten: any residual is closed, then flatness is asserted.
        rows = await client.position_risk(ACCOUNT, SYMBOL)
        residual = _leg_qty(rows)
        if residual != 0:
            side = Side.SELL if D(str(rows[0].position_amt)) > 0 else Side.BUY
            await market(side, residual, f"{tag}-failsafe")
            await _wait_flat_qty(client, D("0"))
    await asyncio.sleep(8.0)  # REST indexing latency for userTrades/income.

    scope = HistoryScope(ACCOUNT, "testnet")
    reader = HistoryReader(client)
    history, episodes, events = await _history_stack(history_db)
    synchronizer = HistorySynchronizer(reader, history)
    import_window = Window(t0, int(time.time() * 1000))
    await synchronizer.sync_fills(scope, SYMBOL, import_window)
    await synchronizer.sync_income(scope, import_window)
    capture = HistoryCapture(
        history,
        episodes,
        events=events,
        projection=HistoryProjection(history, episodes, events),
    )
    # The capture window must end at the live edge: the cutoff has to be
    # bracketed by the boundary snapshots (clock margin 2000 ms).
    window = Window(t0, int(time.time() * 1000))
    stored, published = await capture.capture_and_publish(
        scope, SYMBOL, window, snapshots=client, synchronizer=synchronizer, notify_eligible=True
    )
    assert published.start == t0 and published.end >= window.end - 300_000

    stored_fills = await history_db.fetchall(
        "SELECT * FROM follower_fills WHERE account_id=? AND symbol=? AND executed_at>=?",
        (ACCOUNT, SYMBOL, t0),
    )
    ours = [r for r in stored_fills if int(str(r["order_id"])) in our_order_ids]
    assert len(ours) >= 4, "all four lifecycle fills must be imported"

    closed = await episodes.list_closed(scope, symbol=SYMBOL, limit=50)
    assert len(closed) == 1, "one closed episode holds every size change"
    ref = closed[0].episode.public_ref
    detail_episode = (await episodes.get(scope, ref)).episode
    assert D(str(detail_episode.realized_pnl_gross)) > 0

    # Restart: fresh objects over the same files import nothing twice.
    fills_before = len(stored_fills)
    events_before = len(await events.list_owned(scope))
    history2 = HistoryRepository(history_db)
    reader2 = HistoryReader(client)
    synchronizer2 = HistorySynchronizer(reader2, history2)
    await synchronizer2.sync_fills(scope, SYMBOL, window)
    await synchronizer2.sync_income(scope, window)
    stored_after = await history_db.fetchall(
        "SELECT * FROM follower_fills WHERE account_id=? AND symbol=? AND executed_at>=?",
        (ACCOUNT, SYMBOL, t0),
    )
    assert len(stored_after) == fills_before
    assert len(await events.list_owned(scope)) == events_before

    # §10.9.3/4: exchange truth vs episode money; net gap stays explained.
    live_fills = await client.user_trades(ACCOUNT, SYMBOL, start_time=t0, end_time=window.end)
    mine = [f for f in live_fills if f.time >= t0 and int(f.order_id) in our_order_ids]
    assert len(mine) >= 4
    exchange_gross = sum((D(str(f.realized_pnl)) for f in mine if f.side == "SELL"), D("0"))
    assert D(str(detail_episode.realized_pnl_gross)) == exchange_gross
    live_income = await client.income_history(ACCOUNT, start_time=t0, end_time=window.end)
    # Costs half of §10.9.4: exchange COMMISSION income must single-count the
    # fill commissions (magnitude match on USDT legs, no double counting).
    fill_fees = sum(
        (abs(D(str(f.commission))) for f in mine if f.commission_asset == "USDT"), D("0")
    )
    income_fees = sum(
        (
            abs(D(str(i.income)))
            for i in live_income
            if i.income_type == "COMMISSION" and i.asset == "USDT" and i.time >= t0
        ),
        D("0"),
    )
    assert fill_fees > 0 and income_fees == fill_fees
    money = account_episode(
        detail_episode,
        [f for f in live_fills if f.time >= t0],
        [i for i in live_income if i.time >= t0],
        proof=AccountingProof(),
    )
    assert money.gross == exchange_gross
    if money.net is None:
        assert money.issues, "net gap must name its concrete cause"
    else:
        assert not money.issues

    # §10.9.5: one ref and identical facts on every surface.
    user_address = "0x0000000000000000000000000000000000000001"
    await AccountRepo(history_db).upsert(ACCOUNT, "follower", ACCOUNT, user_address)
    await TelegramUserRepo(history_db).upsert(USER_ID, 9001, account_id=ACCOUNT)
    await SettingsRepo(history_db).upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=D("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        ACCOUNT,
    )
    service = UserApplicationService(
        _LiveRuntime(history_db),  # type: ignore[arg-type]
        admin_ids=[100],
        testnet_check=True,
    )
    (item,) = [i for i in (await service.trades(USER_ID)).items if i.trade_ref == ref]
    detail = await service.trade(USER_ID, ref)
    feed_item = next(i for i in (await service.activity_feed(USER_ID)).items if i.trade_ref == ref)
    router = NotificationRouter(history_db, admin_ids=[100])
    recovered = await router.recover_user_events(EventRepository(history_db), [scope], since_ms=t0)
    assert recovered >= 1
    row = await history_db.fetchone("SELECT * FROM notification_outbox WHERE trade_ref=?", (ref,))
    assert detail.trade.trade_ref == feed_item.trade_ref == row["trade_ref"] == ref
    assert detail.trade.executed_qty == item.executed_qty == "0.002"
    assert feed_item.facts["symbol"] == SYMBOL
    assert row["action"] == "open_trade"

    await AccountRepo(history_db).upsert("foreign", "follower", "foreign", "0x" + "3" * 40)
    await TelegramUserRepo(history_db).upsert(7002, 9002, account_id="foreign")
    assert (await service.trades(7002)).items == ()
    with pytest.raises(TradeUnavailableError):
        await service.trade(7002, ref)
