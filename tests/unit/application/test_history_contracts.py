"""S1.4 history contracts over UserApplicationService with real SQLite.

Covers owned trades list/detail/events, the structured activity feed with
pending operations, dashboard overview freshness, snapshot singleflight and
unit resolution — all against durable episodes, fills, events and journal
rows. Foreign and missing refs read alike; invalid input fails closed.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from copytradeaster.application.models import (
    ActivityFilter,
    AuthorizationError,
    TradeUnavailableError,
    ValidationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.unit.history.test_projector import fill, project

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
USER_ID = 7
OTHER_ID = 8
USER_ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_ADDRESS = "0x2222222222222222222222222222222222222222"


class FakeHistoryRuntime:
    """Controllable snapshots and unit metadata for history tests."""

    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.cfg = SimpleNamespace(reconcile_interval_s=15, fee_buffer_pct="0.001")
        self.snapshot: dict | None = {"positions": {}, "available_usdt": "1240"}
        self.snapshot_at: datetime | None = NOW
        self.refresh_calls = 0
        self.release = asyncio.Event()
        self.release.set()
        self.assets: dict[str, str | None] = {"BTCUSDT": "BTC"}

    async def cached_snapshot(self, account_id: str):
        del account_id
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def refresh_snapshot(self, account_id: str):
        del account_id
        self.refresh_calls += 1
        await self.release.wait()
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def symbol_assets(self, account_id: str, symbols: list[str]):
        del account_id
        return {symbol: self.assets.get(symbol) for symbol in symbols}


def _position(symbol="BTCUSDT", **updates):
    row = {
        "symbol": symbol,
        "qty": "0.012",
        "entry_price": "62400",
        "mark_price": "63433.3",
        "unrealized_profit": "12.40",
        "leverage": 10,
        "stop": {
            "side": "SELL",
            "stop_price": "61900",
            "close_position": True,
            "reduce_only": False,
            "orig_qty": "0",
        },
    }
    row.update(updates)
    return row


async def seed_episodes(db: Database, account_id: str = "h1"):
    """Two closed episodes with stable public refs for one account."""
    from copytradeaster.aster.models import UserTradeDTO

    base_ms = int(NOW.timestamp() * 1000)

    def hfill(trade_id, side, qty, price, pnl, at):
        return UserTradeDTO(
            id=trade_id,
            orderId=trade_id,
            symbol="BTCUSDT",
            side=side,
            positionSide="BOTH",
            qty=qty,
            price=price,
            quoteQty=str(D(qty) * D(price)),
            realizedPnl=pnl,
            commission="0.1",
            commissionAsset="USDT",
            time=at,
        )

    day = 86_400_000
    rows = [
        hfill(1, "BUY", "2", "100", "0", base_ms - 2 * day),
        hfill(2, "SELL", "2", "140", "60", base_ms - 2 * day + 3_600_000),
        hfill(3, "BUY", "1", "100", "0", base_ms - day),
        hfill(4, "SELL", "1", "110", "5", base_ms - day + 3_600_000),
    ]
    history_scope = HistoryScope(account_id, "testnet")
    repo = EpisodeRepository(db)
    stored = []
    for episode in project(rows):
        ref = await repo.reserve_ref(
            history_scope, "BTCUSDT", episode.position_side, episode.public_ref
        )
        stored.append(
            await repo.save(
                history_scope,
                dataclasses.replace(episode, public_ref=ref),
                expected_revision=0,
            )
        )
    return stored


@pytest.fixture
async def history_env(tmp_path: Path):
    db = Database(tmp_path / "history_contracts.sqlite3")
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
    runtime = FakeHistoryRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        testnet_check=True,
    )
    yield db, runtime, service
    await db.close()


async def test_trades_lists_closed_newest_first_with_stable_cursor(history_env):
    db, _, service = history_env
    older, newer = await seed_episodes(db)
    page = await service.trades(USER_ID)
    assert [item.trade_ref for item in page.items] == [
        newer.episode.public_ref,
        older.episode.public_ref,
    ]
    assert page.next_cursor is None and page.coverage_complete is False
    assert page.coverage_from is None and page.as_of == NOW
    item = page.items[0]
    assert item.symbol == "BTCUSDT" and item.quantity_asset == "BTC"
    assert item.side.value == "LONG" and item.state.value == "closed"
    assert item.executed_qty == "1" and item.realized_pnl_gross_usdt == "5"
    assert item.net_pnl_usdt is None and item.quality.value == "complete"
    first = await service.trades(USER_ID, limit=1)
    assert [item.trade_ref for item in first.items] == [newer.episode.public_ref]
    assert first.next_cursor is not None
    second = await service.trades(USER_ID, limit=1, cursor=first.next_cursor)
    assert [item.trade_ref for item in second.items] == [older.episode.public_ref]
    assert second.next_cursor is None


async def test_trades_isolates_accounts(history_env):
    db, _, service = history_env
    await seed_episodes(db, "h1")
    await seed_episodes(db, "h2")
    mine = await service.trades(USER_ID)
    theirs = await service.trades(OTHER_ID)
    assert len(mine.items) == 2 and len(theirs.items) == 2
    assert {item.trade_ref for item in mine.items}.isdisjoint(
        {item.trade_ref for item in theirs.items}
    )


async def test_trades_rejects_invalid_input(history_env):
    _, _, service = history_env
    with pytest.raises(ValidationError):
        await service.trades(USER_ID, period="1y")
    with pytest.raises(ValidationError):
        await service.trades(USER_ID, cursor="nope")
    with pytest.raises(ValidationError):
        await service.trades(USER_ID, limit=0)
    with pytest.raises(ValidationError):
        await service.trades(USER_ID, symbol="")
    with pytest.raises(AuthorizationError):
        await service.trades(999)


async def test_trade_resolves_closed_with_breakdown(history_env):
    db, _, service = history_env
    older, _ = await seed_episodes(db)
    detail = await service.trade(USER_ID, older.episode.public_ref)
    assert detail.trade.state.value == "closed" and detail.revision == 1
    assert detail.position_side == "BOTH" and detail.result_state == "gross_only"
    assert (detail.entry_qty, detail.exit_qty) == ("2", "2")
    assert detail.trade.quantity_asset == "BTC"


async def test_trade_hides_foreign_and_missing_refs(history_env):
    db, _, service = history_env
    older, _ = await seed_episodes(db)
    with pytest.raises(TradeUnavailableError):
        await service.trade(USER_ID, "missing-ref")
    with pytest.raises(TradeUnavailableError):
        await service.trade(USER_ID, older.episode.public_ref + "-foreign")
    with pytest.raises(TradeUnavailableError):
        await service.trade(OTHER_ID, older.episode.public_ref)
    with pytest.raises(ValidationError):
        await service.trade(USER_ID, "")


async def test_trade_events_returns_fills_and_event_page(history_env):
    db, _, service = history_env
    older, _ = await seed_episodes(db)
    scope = HistoryScope("h1", "testnet")
    history = HistoryRepository(db)
    base_ms = int(NOW.timestamp() * 1000)
    opened = older.episode.opened_at or 0
    closed = older.episode.closed_at or base_ms
    start = max(opened - 3_600_000, 0)
    rows = [
        fill(1, "BUY", "2"),
        fill(2, "SELL", "2", "140", "60"),
    ]
    rows = [
        row.model_copy(update={"time": opened if index == 0 else closed})
        for index, row in enumerate(rows)
    ]
    window = Window(start, closed + 3_600_000)
    await history.commit_fills(
        scope,
        "BTCUSDT",
        window,
        WindowRead(tuple(rows), (window,), (), (), 1),
        checked_at=closed + 3_600_001,
    )
    events = EventRepository(db)
    await events.record(
        scope,
        kind="POSITION_OPENED",
        trade_ref=older.episode.public_ref,
        occurred_at=1,
        recorded_at=2,
        facts={"symbol": "BTCUSDT"},
        dedup_key="evt:open:test",
    )
    await events.record(
        scope,
        kind="POSITION_CLOSED",
        trade_ref=older.episode.public_ref,
        occurred_at=2,
        recorded_at=3,
        facts={"symbol": "BTCUSDT"},
        dedup_key="evt:close:test",
    )
    page = await service.trade_events(USER_ID, older.episode.public_ref)
    assert [fact.trade_id for fact in page.fills] == ["1", "2"]
    assert [item.kind for item in page.events] == ["POSITION_OPENED", "POSITION_CLOSED"]
    assert page.next_cursor is None
    first = await service.trade_events(USER_ID, older.episode.public_ref, limit=1)
    assert [item.kind for item in first.events] == ["POSITION_OPENED"]
    assert first.next_cursor is not None
    second = await service.trade_events(
        USER_ID, older.episode.public_ref, cursor=int(first.next_cursor), limit=1
    )
    assert [item.kind for item in second.events] == ["POSITION_CLOSED"]
    assert second.next_cursor is None
    with pytest.raises(TradeUnavailableError):
        await service.trade_events(USER_ID, "missing-ref")
    with pytest.raises(ValidationError):
        await service.trade_events(USER_ID, older.episode.public_ref, cursor=-1)


async def test_activity_feed_filters_and_reports_high_water(history_env):
    db, _, service = history_env
    events = EventRepository(db)
    scope = HistoryScope("h1", "testnet")
    for index, kind in enumerate(
        ["POSITION_OPENED", "PROTECTION_SET", "RISK_CHANGED", "COPY_PAUSED"]
    ):
        await events.record(
            scope,
            kind=kind,
            occurred_at=index,
            recorded_at=index + 1,
            facts={"symbol": "BTCUSDT"},
            dedup_key=f"evt:{index}",
        )
    page = await service.activity_feed(USER_ID)
    assert [item.kind for item in page.items] == [
        "POSITION_OPENED",
        "PROTECTION_SET",
        "RISK_CHANGED",
        "COPY_PAUSED",
    ]
    assert page.high_water == 4 and page.next_cursor is None and page.pending == ()
    trade_only = await service.activity_feed(USER_ID, filter="trade")
    assert [item.kind for item in trade_only.items] == ["POSITION_OPENED", "PROTECTION_SET"]
    system_only = await service.activity_feed(USER_ID, filter=ActivityFilter.SYSTEM)
    assert [item.kind for item in system_only.items] == ["RISK_CHANGED", "COPY_PAUSED"]
    first = await service.activity_feed(USER_ID, limit=2)
    assert first.next_cursor == "2" and len(first.items) == 2
    second = await service.activity_feed(USER_ID, cursor=int(first.next_cursor), limit=2)
    assert [item.kind for item in second.items] == ["RISK_CHANGED", "COPY_PAUSED"]
    with pytest.raises(ValidationError):
        await service.activity_feed(USER_ID, filter="bogus")


async def test_activity_feed_reports_pending_operations(history_env):
    import json

    db, _, service = history_env
    commands = CommandRepo(db)
    await commands.insert(
        "cmd-1", "h1", None, "PLACE_STOP", json.dumps({"symbol": "BTCUSDT"}), 1, "ct-1"
    )
    await commands.set_status("cmd-1", "SUBMITTED")
    await commands.insert(
        "cmd-2", "h1", None, "MARKET_CLOSE", json.dumps({"symbol": "ETHUSDT"}), 1, "ct-2"
    )
    await commands.set_status("cmd-2", "UNKNOWN")
    page = await service.activity_feed(USER_ID)
    assert [(item.symbol, item.operation, item.state) for item in page.pending] == [
        ("BTCUSDT", "protection", "pending"),
        ("ETHUSDT", "exit", "unknown"),
    ]
    assert all(item.age_seconds == 0 for item in page.pending)


async def test_dashboard_overview_reflects_fresh_snapshot(history_env):
    _, runtime, service = history_env
    runtime.snapshot = {
        "positions": {
            "a": _position(),
            "b": _position(symbol="ETHUSDT", unrealized_profit="-2.40", stop=None),
        },
        "available_usdt": "1240",
    }
    runtime.snapshot_at = NOW
    dto = await service.dashboard(USER_ID, refresh=False)
    assert dto.overview is not None
    assert dto.overview.open_count == 2 and dto.overview.protected_count == 1
    assert dto.overview.unrealized_pnl_usdt == "10" and dto.overview.available_usdt == "1240"
    assert dto.overview.freshness.value == "fresh"
    assert dto.overview.risk_coverage.value == "complete"


async def test_dashboard_overview_marks_stale_and_missing(history_env):
    _, runtime, service = history_env
    runtime.snapshot = {"positions": {"a": _position()}, "available_usdt": "1240"}
    runtime.snapshot_at = NOW - timedelta(seconds=3600)
    dto = await service.dashboard(USER_ID, refresh=False)
    assert dto.overview is not None and dto.overview.freshness.value == "stale"
    assert dto.overview.risk_coverage.value == "partial"
    runtime.snapshot = None
    runtime.snapshot_at = None
    assert (await service.dashboard(USER_ID, refresh=False)).overview is None


async def test_snapshot_refresh_is_singleflight(history_env):
    _, runtime, service = history_env
    runtime.release.clear()
    first = asyncio.ensure_future(service._snapshot("h1", refresh=True))
    second = asyncio.ensure_future(service._snapshot("h1", refresh=True))
    for _ in range(20):
        await asyncio.sleep(0)
        if runtime.refresh_calls:
            break
    assert runtime.refresh_calls == 1
    runtime.release.set()
    assert await first == await second
    assert runtime.refresh_calls == 1


async def test_quantity_assets_default_to_none_without_runtime_support(history_env):
    _, runtime, service = history_env
    runtime.symbol_assets = None  # type: ignore[method-assign]
    assert await service._quantity_assets("h1", ["BTCUSDT"]) == {"BTCUSDT": None}
    assert await service._quantity_assets("h1", []) == {}
