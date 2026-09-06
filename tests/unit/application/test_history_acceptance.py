"""S1.6 offline acceptance: one trade, four surfaces, one truth (§10.9.5/6).

A synthetic closed episode travels the real projection path, then the
trades list, the trade detail, the activity feed and the notification
recovery must all reference the same ``trade_ref`` with the same
quantity, prices and gross. A foreign user sees nothing on any surface.
Companion coverage: HTTP boundary in test_history_server.py, keyboard attach
in test_worker_attaches_button_only_for_structured_rows, republish stability
in test_publish_zero_seed_open_episode_and_republish_is_stable; signed live
§10.9 in tests/testnet/test_history_acceptance_live.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.application.models import TradeUnavailableError
from copytradeaster.application.service import UserApplicationService
from copytradeaster.domain.models import PosSide, RiskPolicy
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.unit.application.test_history_contracts import (
    OTHER_ADDRESS,
    OTHER_ID,
    USER_ADDRESS,
    USER_ID,
    FakeHistoryRuntime,
)
from tests.unit.history.test_projector import fill

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
FLOOR_MS = int(datetime(2026, 9, 5, 12, 0, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture
async def accepted_trade(tmp_path: Path):
    """One closed BTCUSDT episode through the real publication path."""
    db = Database(tmp_path / "history_acceptance.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("h1", "follower", "h1", USER_ADDRESS)
    await AccountRepo(db).upsert("h2", "follower", "h2", OTHER_ADDRESS)
    await TelegramUserRepo(db).upsert(USER_ID, 700, account_id="h1")
    await TelegramUserRepo(db).upsert(OTHER_ID, 800, account_id="h2")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=D("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        "h1",
    )
    settings = SettingsRepo(db)
    await settings.set_flag("ready", "true")
    await settings.set_flag("allow_new_entries", "true")

    scope = HistoryScope("h1", "testnet")
    history = HistoryRepository(db)
    fills = (
        fill(1, "BUY", "1", price="100", pnl="0", leg="BOTH"),
        fill(2, "BUY", "1", price="100", pnl="0", leg="BOTH"),
        fill(3, "SELL", "1", price="140", pnl="40", leg="BOTH"),
        fill(4, "SELL", "1", price="140", pnl="40", leg="BOTH"),
    )
    window = Window(0, 4)
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(fills, (window,), (), (), 1), checked_at=3
    )
    projection = HistoryProjection(history, EpisodeRepository(db), EventRepository(db))
    both = PosSide.BOTH
    (stored,) = await projection.publish_window(
        scope,
        "BTCUSDT",
        window,
        seeds={both: PositionSeed(D(0))},
        ending={both: D(0)},
        recorded_at=FLOOR_MS + 1,
        notify_eligible=True,
    )
    runtime = FakeHistoryRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        testnet_check=True,
    )
    yield db, service, stored.episode.public_ref
    await db.close()


async def test_all_surfaces_share_one_ref_and_facts(accepted_trade):
    db, service, ref = accepted_trade
    (item,) = (await service.trades(USER_ID, period="all")).items
    detail = await service.trade(USER_ID, ref)
    feed_item = next(
        i for i in (await service.activity_feed(USER_ID)).items if i.kind == "POSITION_CLOSED"
    )
    router = NotificationRouter(db, admin_ids=[100])
    recovered = await router.recover_user_events(
        EventRepository(db), [HistoryScope("h1", "testnet")], since_ms=FLOOR_MS
    )
    row = await db.fetchone("SELECT * FROM notification_outbox WHERE kind='user.POSITION_CLOSED'")

    assert recovered == 2

    assert (
        item.trade_ref == detail.trade.trade_ref == feed_item.trade_ref == row["trade_ref"] == ref
    )
    assert item.executed_qty == detail.trade.executed_qty == "2"
    assert detail.average_entry == "100" and detail.average_exit == "140"
    assert item.realized_pnl_gross_usdt == detail.trade.realized_pnl_gross_usdt == "80"
    assert feed_item.facts["realized_gross"] == "80"
    assert feed_item.facts["entry_price"] == "100" and feed_item.facts["exit_price"] == "140"
    # Single-shot publication batches one round into OPENED+CLOSED: the
    # increase/partial fills above still settle the same episode (qty, entry,
    # exit, gross all prove it), while per-leg INCREASED/REDUCED rows belong
    # to multi-publish continuation, covered at unit level in
    # test_event_builder.py. Locking the batching rule here keeps the
    # silence explicit instead of accidental.
    stored_kinds = {
        row.event.kind
        for row in await EventRepository(db).list_owned(HistoryScope("h1", "testnet"))
    }
    assert stored_kinds == {"POSITION_OPENED", "POSITION_CLOSED"}


async def test_foreign_user_sees_nothing_on_any_surface(accepted_trade):
    db, service, ref = accepted_trade
    assert (await service.trades(OTHER_ID)).items == ()
    with pytest.raises(TradeUnavailableError):
        await service.trade(OTHER_ID, ref)
    assert (await service.activity_feed(OTHER_ID)).items == ()
    router = NotificationRouter(db, admin_ids=[100])
    assert (
        await router.recover_user_events(
            EventRepository(db), [HistoryScope("h2", "testnet")], since_ms=FLOOR_MS
        )
        == 0
    )
