"""C2 trade/events regressions: allocation-bound fills, family events, cursors."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from copytradeaster.application.models import (
    HistoryPageChangedError,
    TradeSplitError,
    TradeUnavailableError,
    ValidationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.domain.models import PosSide, RiskPolicy
from copytradeaster.history.models import PositionSeed, TradeResolutionDTO
from copytradeaster.history.projector import project_episodes
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.unit.history.test_projector import fill

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
USER_ID = 7
USER_ADDRESS = "0x1111111111111111111111111111111111111111"


class FakeRuntime:
    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.cfg = SimpleNamespace(reconcile_interval_s=15, fee_buffer_pct="0.001")
        self.snapshot: dict | None = {"positions": {}, "available_usdt": "1240"}
        self.snapshot_at: datetime | None = NOW
        self.assets: dict[str, str | None] = {"BTCUSDT": "BTC"}

    async def cached_snapshot(self, account_id: str):
        del account_id
        return (self.snapshot, self.snapshot_at) if self.snapshot is not None else None

    async def refresh_snapshot(self, account_id: str):
        del account_id
        return (self.snapshot, self.snapshot_at) if self.snapshot is not None else None

    async def symbol_assets(self, account_id: str, symbols: list[str]):
        del account_id
        return {symbol: self.assets.get(symbol) for symbol in symbols}


@pytest.fixture
async def c2_env(tmp_path):
    db = Database(tmp_path / "trade_events_c2.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("h1", "follower", "h1", USER_ADDRESS)
    await TelegramUserRepo(db).upsert(USER_ID, 700, account_id="h1")
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
    await SettingsRepo(db).set_flag("ready", "true")
    await SettingsRepo(db).set_flag("allow_new_entries", "true")
    runtime = FakeRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        testnet_check=True,
    )
    yield db, service
    await db.close()


def project(rows, seeds):
    return project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds=seeds,
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )


async def publish_all(db, scope, episodes):
    repo = EpisodeRepository(db)
    refs = [
        await repo.reserve_ref(scope, "BTCUSDT", episode.position_side, episode.public_ref)
        for episode in episodes
    ]
    return await repo.publish(
        scope,
        "BTCUSDT",
        [replace(episode, public_ref=ref) for episode, ref in zip(episodes, refs, strict=True)],
        {ref: 0 for ref in refs},
    )


async def commit_rows(db, scope, rows):
    history = HistoryRepository(db)
    start = min(row.time for row in rows)
    end = max(row.time for row in rows)
    window = Window(start, end)
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(tuple(rows), (window,), (), (), 1), checked_at=end + 1
    )


async def record(db, scope, ref, seq_key, occurred=1):
    await EventRepository(db).record(
        scope,
        kind="POSITION_OPENED",
        trade_ref=ref,
        occurred_at=occurred,
        recorded_at=occurred + 1,
        facts={"symbol": "BTCUSDT"},
        dedup_key=f"evt:{seq_key}",
    )


async def test_hedge_overlap_returns_only_own_leg_with_roles(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    rows = [
        fill(101, "BUY", "2", leg="LONG"),
        fill(102, "SELL", "3", leg="SHORT"),
        fill(103, "SELL", "2", "140", "60", leg="LONG"),
    ]
    long, short = sorted(
        project(rows, {PosSide.LONG: PositionSeed(D(0)), PosSide.SHORT: PositionSeed(D(0))}),
        key=lambda e: e.direction,
    )
    assert (long.direction, short.direction) == ("long", "short")
    stored_long, stored_short = await publish_all(db, scope, [long, short])
    await commit_rows(db, scope, rows)
    long_page = await service.trade_events(USER_ID, stored_long.episode.public_ref)
    short_page = await service.trade_events(USER_ID, stored_short.episode.public_ref)
    assert [(item.trade_id, item.role) for item in long_page.fills] == [
        ("101", "entry"),
        ("103", "exit"),
    ]
    assert [(item.trade_id, item.role) for item in short_page.fills] == [("102", "entry")]
    assert all(item.position_side == "LONG" for item in long_page.fills)
    assert all(item.position_side == "SHORT" for item in short_page.fills)
    assert long_page.fills[0].quantity == "2" and long_page.fills[0].allocated_quantity == "2"


async def test_shared_flip_fill_splits_and_sums_to_source(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    rows = [fill(201, "BUY", "2"), fill(202, "SELL", "3", "110", "20")]
    long, short = project(rows, {PosSide.BOTH: PositionSeed(D(0))})
    stored_long, stored_short = await publish_all(db, scope, [long, short])
    await commit_rows(db, scope, rows)
    long_page = await service.trade_events(USER_ID, stored_long.episode.public_ref)
    short_page = await service.trade_events(USER_ID, stored_short.episode.public_ref)
    shared_long = next(item for item in long_page.fills if item.trade_id == "202")
    shared_short = next(item for item in short_page.fills if item.trade_id == "202")
    assert shared_long.quantity == "3" and shared_long.allocated_quantity == "2"
    assert shared_short.quantity == "3" and shared_short.allocated_quantity == "1"
    assert D(shared_long.allocated_quantity) + D(shared_short.allocated_quantity) == D("3")
    assert shared_long.allocated_realized_pnl == "20"
    assert shared_short.allocated_realized_pnl == "0"


async def test_alias_and_canonical_share_family_events(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    rows = [
        fill(301, "BUY", "1"),
        fill(302, "SELL", "1", "110", "10"),
        fill(303, "BUY", "1"),
        fill(304, "SELL", "1", "110", "10"),
    ]
    first, second = project(rows, {PosSide.BOTH: PositionSeed(D(0))})
    target, alias = await publish_all(db, scope, [first, second])
    await commit_rows(db, scope, rows)
    await EpisodeRepository(db).merge_ref(
        scope, alias.episode.public_ref, target.episode.public_ref
    )
    await record(db, scope, target.episode.public_ref, "target")
    await record(db, scope, alias.episode.public_ref, "alias")
    via_target = await service.trade_events(USER_ID, target.episode.public_ref)
    via_alias = await service.trade_events(USER_ID, alias.episode.public_ref)
    assert [item.event_ref for item in via_target.events] == [
        item.event_ref for item in via_alias.events
    ]
    assert len(via_target.events) == 2
    # The alias detail still resolves to the surviving episode.
    detail = await service.trade(USER_ID, alias.episode.public_ref)
    assert detail.trade.trade_ref == target.episode.public_ref


async def test_revision_change_mid_paging_returns_history_page_changed(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    rows = [fill(401, "BUY", "1"), fill(402, "BUY", "1")]
    (episode,) = project(rows, {PosSide.BOTH: PositionSeed(D(0))})
    (stored,) = await publish_all(db, scope, [episode])
    await commit_rows(db, scope, rows)
    ref = stored.episode.public_ref
    first = await service.trade_events(USER_ID, ref, fills_limit=1, events_limit=50)
    assert len(first.fills) == 1
    assert first.fills_cursor is not None and first.next_fills_cursor == first.fills_cursor
    changed = replace(stored.episode, source="external")
    await EpisodeRepository(db).publish(scope, "BTCUSDT", [changed], {ref: 1})
    with pytest.raises(HistoryPageChangedError) as exc_info:
        await service.trade_events(USER_ID, ref, fills_cursor=first.fills_cursor)
    assert exc_info.value.public_code == "history_page_changed"


async def test_legacy_cursor_pages_events_only(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    rows = [fill(501, "BUY", "2"), fill(502, "SELL", "2", "140", "60")]
    (episode,) = project(rows, {PosSide.BOTH: PositionSeed(D(0))})
    (stored,) = await publish_all(db, scope, [episode])
    await commit_rows(db, scope, rows)
    ref = stored.episode.public_ref
    await record(db, scope, ref, "first", occurred=1)
    await record(db, scope, ref, "second", occurred=2)
    first = await service.trade_events(USER_ID, ref, limit=1)
    assert len(first.events) == 1
    assert first.next_cursor is not None and first.next_events_cursor == first.next_cursor
    # The legacy shared limit bounds both arrays; fills resume via fills_cursor.
    assert [item.trade_id for item in first.fills] == ["501"]
    assert first.fills_cursor is not None
    rest = await service.trade_events(USER_ID, ref, fills_cursor=first.fills_cursor, limit=50)
    assert [item.trade_id for item in rest.fills] == ["502"]
    second = await service.trade_events(USER_ID, ref, cursor=int(first.next_cursor), limit=1)
    assert len(second.events) == 1
    assert second.next_cursor is None
    assert [item.event_ref for item in first.events] != [item.event_ref for item in second.events]


async def test_split_ref_is_explicit_not_silent(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    rows = [
        fill(601, "BUY", "1"),
        fill(602, "SELL", "1", "110", "10"),
        fill(603, "BUY", "1"),
        fill(604, "SELL", "1", "110", "10"),
    ]
    episodes = project(rows, {PosSide.BOTH: PositionSeed(D(0))})
    stored = await publish_all(db, scope, list(episodes))
    refs = [item.episode.public_ref for item in stored]
    repo = EpisodeRepository(db)
    current = await repo.publish(
        scope,
        "BTCUSDT",
        [item.episode for item in stored],
        {ref: 1 for ref in refs},
        resolutions=[
            TradeResolutionDTO(
                ref="old-split",
                kind="split",
                targets=tuple(refs),
                reason="review",
                revision=2,
            )
        ],
        now_ms=int(NOW.timestamp() * 1000),
    )
    assert len(current) == 2
    with pytest.raises(TradeSplitError) as exc_info:
        await service.trade_events(USER_ID, "old-split")
    assert exc_info.value.public_code == "trade_split"


async def test_foreign_and_missing_refs_stay_indistinguishable(c2_env):
    db, service = c2_env
    scope = HistoryScope("h1", "testnet")
    with pytest.raises(TradeUnavailableError):
        await service.trade_events(USER_ID, "missing-ref")
    with pytest.raises(ValidationError):
        await service.trade_events(USER_ID, "")
    with pytest.raises(ValidationError):
        await service.trade_events(USER_ID, "x", fills_limit=51)
    # Opaque cursor content is checked only after the ref resolves: a bogus
    # cursor against a missing ref stays indistinguishable from missing.
    with pytest.raises(TradeUnavailableError):
        await service.trade_events(USER_ID, "x", fills_cursor="bogus-cursor")
    rows = [fill(701, "BUY", "1")]
    (episode,) = project(rows, {PosSide.BOTH: PositionSeed(D(0))})
    (stored,) = await publish_all(db, scope, [episode])
    await commit_rows(db, scope, rows)
    with pytest.raises(ValidationError):
        await service.trade_events(USER_ID, stored.episode.public_ref, fills_cursor="bogus")
