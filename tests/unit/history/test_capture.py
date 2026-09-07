"""Boundary capture: proven snapshots bracket the import, then publish once."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from copytradeaster.aster.models import OrderDTO, PositionDTO
from copytradeaster.history.capture import HistoryCapture, read_boundary
from copytradeaster.history.reader import Span, Window, WindowRead
from copytradeaster.history.reconcile import HistoryReconciliationError
from copytradeaster.history.snapshot import PositionBoundary
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo
from tests.unit.aster.test_trading_api import ORDER_RESPONSE
from tests.unit.history.test_projector import fill


def position(**updates):
    return PositionDTO.model_validate(
        {
            "symbol": "BTCUSDT",
            "positionAmt": "1",
            "entryPrice": "100",
            "markPrice": "110",
            "leverage": 10,
            "liquidationPrice": "0",
            "marginType": "cross",
            "positionSide": "BOTH",
            "unRealizedProfit": "10",
            "updateTime": 50,
            **updates,
        }
    )


def boundary(row=None, started=0, received=100):
    return PositionBoundary.from_positions(
        "BTCUSDT", [row or position()], request_started=started, response_received=received
    )


def after_boundary(row=None):
    return boundary(row, started=2200, received=2300)


def capture_for(db, history):
    return HistoryCapture(history, EpisodeRepository(db))


async def commit(history, scope, fills, window):
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(tuple(fills), (window,), (), (), 1), checked_at=3
    )


async def test_publish_zero_seed_open_episode_and_republish_is_stable(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
    )
    first = await capture.publish_reconciled(**kwargs)
    assert len(first) == 1
    episode = first[0].episode
    assert episode.quality == "complete" and episode.remaining_qty == D(1)
    assert episode.opened_at == 60 and episode.closed_at is None
    assert await capture.publish_reconciled(**kwargs) == first


async def test_changed_position_refuses_without_publishing(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    moved = after_boundary(position(positionAmt="2", updateTime=150))
    with pytest.raises(HistoryReconciliationError):
        await capture.publish_reconciled(
            scope, "BTCUSDT", window, before=boundary(), after=moved, cutoff=100
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_unbracketed_cutoff_refuses_without_publishing(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    early = boundary(started=0, received=100)
    late = replace(early, request_started=150, response_received=160)
    with pytest.raises(HistoryReconciliationError, match="bracketed"):
        await capture.publish_reconciled(
            scope, "BTCUSDT", window, before=early, after=late, cutoff=100
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_incomplete_coverage_refuses_without_publishing(storage):
    db, history, scope = storage
    narrow = Window(50, 70)
    await commit(history, scope, [fill(60, "BUY", "1")], narrow)
    capture = capture_for(db, history)
    with pytest.raises(HistoryReconciliationError, match="gap"):
        await capture.publish_reconciled(
            scope, "BTCUSDT", Window(0, 100), before=boundary(), after=after_boundary(), cutoff=100
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_nonzero_seed_keeps_stable_placeholder_ref(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "2")], window)
    before = boundary(position(positionAmt="5"))
    after = after_boundary(position(positionAmt="5"))
    capture = capture_for(db, history)
    kwargs = dict(
        scope=scope, symbol="BTCUSDT", window=window, before=before, after=after, cutoff=100
    )
    first = await capture.publish_reconciled(**kwargs)
    assert len(first) == 1
    episode = first[0].episode
    assert episode.quality == "partial_history" and episode.opened_at is None
    assert episode.remaining_qty == D(5)
    second = await capture.publish_reconciled(**kwargs)
    assert second == first
    assert second[0].episode.public_ref == episode.public_ref


async def test_ending_mismatch_refuses_without_erasing_prior(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    good = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
    )
    first = await capture.publish_reconciled(**good)
    wrong = after_boundary(position(positionAmt="9"))
    with pytest.raises(HistoryReconciliationError):
        await capture.publish_reconciled(
            scope, "BTCUSDT", window, before=boundary(), after=wrong, cutoff=100
        )
    repo = EpisodeRepository(db)
    assert await repo.get(scope, first[0].episode.public_ref) == first[0]


async def test_concurrent_captures_serialize_to_one_result(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
    )
    first, second = await asyncio.gather(
        capture.publish_reconciled(**kwargs), capture.publish_reconciled(**kwargs)
    )
    assert first == second


async def test_episode_is_owned_by_account_and_environment(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    (stored,) = await capture.publish_reconciled(
        scope, "BTCUSDT", window, before=boundary(), after=after_boundary(), cutoff=100
    )
    repo = EpisodeRepository(db)
    assert await repo.get(HistoryScope("b", "testnet"), stored.episode.public_ref) is None
    assert await repo.get(HistoryScope("a", "mainnet"), stored.episode.public_ref) is None


async def test_empty_symbol_rejected_before_exchange_read(storage):
    db, history, scope = storage
    capture = capture_for(db, history)
    with pytest.raises(ValueError):
        await read_boundary(AsyncMock(), scope, "")
    with pytest.raises(ValueError):
        await capture.publish_reconciled(
            scope, "", Window(0, 100), before=boundary(), after=after_boundary(), cutoff=100
        )


async def test_foreign_symbol_snapshot_cannot_certify_boundary(storage):
    _, _, scope = storage

    class Foreign:
        async def position_risk(self, account_id, symbol):
            assert account_id == "a"
            return [position(symbol="ETHUSDT")]

    with pytest.raises(HistoryReconciliationError):
        await read_boundary(Foreign(), scope, "BTCUSDT", clock_ms=lambda: 7)


class StaticSnapshots:
    def __init__(self, rows):
        self._rows = rows

    async def position_risk(self, account_id, symbol):
        assert account_id == "a"
        return list(self._rows)


class ScopeSnapshots:
    def __init__(self, rows):
        self._rows = rows

    async def position_risk(self, account_id, symbol):
        return list(self._rows)


class MutableFillReader:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def fills(self, account_id, symbol, window, request_budget=8):
        rows = tuple(
            row
            for row in self.rows
            if row.symbol == symbol and window.start <= row.time <= window.end
        )
        return WindowRead(rows, (window,), (), (), 1)


def manual_clock(values):
    it = iter(values)
    return lambda: next(it)


async def test_capture_and_publish_wires_import_then_proven_publish(storage):
    db, history, scope = storage
    window = Window(0, 100)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)
    capture = capture_for(db, history)
    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([0, 100, 3000, 3100]),
    )
    assert published == Span(0, 100)
    assert len(stored) == 1 and stored[0].episode.remaining_qty == D(1)
    assert len(await history.fills(scope, "BTCUSDT", window)) == 1


async def test_fast_import_waits_out_margin_then_publishes(storage):
    db, history, scope = storage
    window = Window(0, 100)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)
    capture = capture_for(db, history)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([0, 100, 150, 160, 2150, 2160]),
        sleep=fake_sleep,
    )
    assert published == Span(0, 100)
    assert len(stored) == 1
    assert slept == [pytest.approx(1.95)]
    assert len(await history.fills(scope, "BTCUSDT", window)) == 1


async def test_short_coverage_defers_without_waiting(storage):
    db, history, scope = storage
    await commit(history, scope, [], Window(0, 50))
    window = Window(0, 100)
    reader = AsyncMock()
    capture = capture_for(db, history)
    reader.fills.return_value = WindowRead((), (Window(0, 50),), (Window(51, 100),), (), 1)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    with pytest.raises(HistoryReconciliationError):
        await capture.capture_and_publish(
            scope,
            "BTCUSDT",
            window,
            snapshots=StaticSnapshots([position()]),
            synchronizer=HistorySynchronizer(reader, history),
            clock_ms=manual_clock([0, 60, 70, 80]),
            sleep=fake_sleep,
        )
    assert slept == []
    assert len(await history.fills(scope, "BTCUSDT", window)) == 0
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_stale_window_extends_to_live_edge_and_publishes(storage):
    db, history, scope = storage
    await commit(history, scope, [], Window(0, 1000))
    reader = AsyncMock()
    seen: list[Window] = []

    def _read(account_id, symbol, window, request_budget=8):
        seen.append(window)
        return WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)

    reader.fills.side_effect = _read
    capture = capture_for(db, history)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        Window(0, 1000),
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([1_000_000, 1_000_050, 1_000_100, 1_000_150, 1_002_100, 1_002_150]),
        sleep=fake_sleep,
    )
    assert seen == [Window(0, 1_000_050)]
    assert published == Span(0, 1_000_050)
    assert len(stored) == 1 and stored[0].episode.remaining_qty == D(1)
    assert slept == [pytest.approx(1.95)]


def capture_with_events(db, history):
    return HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))


async def test_publish_reconciled_emits_linked_open_event_once(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_with_events(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
        recorded_at=5000,
        notify_eligible=True,
    )
    (stored,) = await capture.publish_reconciled(**kwargs)
    (row,) = await EventRepository(db).list_owned(scope)
    assert row.event.kind == "POSITION_OPENED"
    assert row.event.trade_ref == stored.episode.public_ref
    assert row.event.recorded_at == 5000 and row.event.notify_eligible is True
    await capture.publish_reconciled(**kwargs)
    assert len(await EventRepository(db).list_owned(scope)) == 1


async def test_capture_and_publish_stamps_record_time_on_events(storage):
    db, history, scope = storage
    window = Window(0, 100)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)
    capture = capture_with_events(db, history)
    await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([0, 100, 3000, 3100]),
        notify_eligible=True,
    )
    (row,) = await EventRepository(db).list_owned(scope)
    assert row.event.kind == "POSITION_OPENED" and row.event.recorded_at == 3100


async def commit_empty(history, scope, window):
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead((), (window,), (), (), 1), checked_at=3
    )


def scheduled_capture(db, history):
    return HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))


async def test_prepare_returns_none_without_complete_coverage(storage):
    db, history, scope = storage
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 100000) is None


async def test_prepare_defers_fresh_frontier_without_exchange_reads(storage):
    db, history, scope = storage
    await commit_empty(history, scope, Window(0, 1000))
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 2999) is None
    assert await capture.prepare_window(scope, "BTCUSDT", 3000) == Span(0, 1000)


async def test_prepare_checkpoint_cannot_hide_dirty_and_clean_frontier_skips(storage):
    db, history, scope = storage
    await commit_empty(history, scope, Window(0, 1000))
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=1000, now_ms=5000)
    capture = scheduled_capture(db, history)
    # A checkpoint alone is not proof that the current facts revision was
    # atomically published; durable dirt keeps the scheduled capture armed.
    assert await capture.prepare_window(scope, "BTCUSDT", 20000) == Span(0, 1000)
    flat = position(positionAmt="0")
    assert (
        await capture.publish_reconciled(
            scope,
            "BTCUSDT",
            Window(0, 1000),
            before=boundary(flat),
            after=boundary(flat, started=3100, received=3200),
            cutoff=1000,
            recorded_at=3200,
        )
        == ()
    )
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=1000, now_ms=5500)
    assert await capture.prepare_window(scope, "BTCUSDT", 20000) is None
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=500, now_ms=6000)
    assert await capture.prepare_window(scope, "BTCUSDT", 20000) == Span(0, 1000)


async def test_corrective_g1_t1_scheduled_bootstrap_uses_continuous_symbol_coverage(storage):
    db, history, scope = storage
    reader = MutableFillReader([fill(1500, "BUY", "1")])
    sync = HistorySynchronizer(reader, history, clock_ms=lambda: 2100)

    # Ordinary import job establishes retained coverage away from epoch.
    await sync.sync_fills(scope, "BTCUSDT", Window(1000, 2000), request_budget=1)
    # A wider unrelated symbol must not move BTC's bootstrap origin.
    await history.commit_fills(
        scope,
        "ETHUSDT",
        Window(0, 5000),
        WindowRead((), (Window(0, 5000),), (), (), 1),
        checked_at=2100,
    )
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 20_000) == Span(1000, 2000)
    assert await capture.run_scheduled(
        scope,
        "BTCUSDT",
        snapshots=ScopeSnapshots([position(positionAmt="1", updateTime=1500)]),
        synchronizer=sync,
        now_ms=20_000,
        request_budget=1,
        clock_ms=manual_clock([1800, 1900, 4100, 4200]),
    )
    origin = await EpisodeRepository(db).load_boundary(scope, "BTCUSDT", kind="origin")
    assert origin is not None and origin.cutoff_ms == 1000
    assert len(await EpisodeRepository(db).list_open(scope, symbol="BTCUSDT")) == 1

    # Legacy failed checkpoint start=0 is a hint only: without an accepted
    # origin, a second scope recomputes bootstrap from its own complete span.
    other = HistoryScope("b", "testnet")
    reader.rows = [fill(3500, "BUY", "1")]
    other_sync = HistorySynchronizer(reader, history, clock_ms=lambda: 4100)
    await other_sync.sync_fills(other, "BTCUSDT", Window(3000, 4000), request_budget=1)
    await history.record_capture(
        other,
        "BTCUSDT",
        start_ms=0,
        end_ms=None,
        now_ms=4200,
        error="HistoryReconciliationError",
    )
    assert await capture.prepare_window(other, "BTCUSDT", 30_000) == Span(3000, 4000)
    assert await capture.run_scheduled(
        other,
        "BTCUSDT",
        snapshots=ScopeSnapshots([position(positionAmt="1", updateTime=3500)]),
        synchronizer=other_sync,
        now_ms=30_000,
        request_budget=1,
        clock_ms=manual_clock([3800, 3900, 6100, 6200]),
    )
    other_origin = await EpisodeRepository(db).load_boundary(other, "BTCUSDT", kind="origin")
    assert other_origin is not None and other_origin.cutoff_ms == 3000


async def test_corrective_g1_t3_dirty_survives_restart_and_same_frontier_noop_import(storage):
    db, history, scope = storage
    flat = position(positionAmt="0", updateTime=9000)
    reader = MutableFillReader()
    sync = HistorySynchronizer(reader, history, clock_ms=lambda: 10_100)
    capture = scheduled_capture(db, history)

    await sync.sync_fills(scope, "BTCUSDT", Window(9000, 10_000), request_budget=1)
    assert await capture.run_scheduled(
        scope,
        "BTCUSDT",
        snapshots=ScopeSnapshots([flat]),
        synchronizer=sync,
        now_ms=20_000,
        request_budget=1,
        clock_ms=manual_clock([9800, 9900, 12_100, 12_200]),
    )
    assert (await history.projection_state(scope, "BTCUSDT"))[1] is None

    # Late facts land behind an already accepted flat cutoff; frontier stays 10_000.
    reader.rows = [
        fill(9400, "BUY", "1", "100", "0"),
        fill(9600, "SELL", "1", "140", "40"),
    ]
    late = await sync.sync_fills(scope, "BTCUSDT", Window(9000, 10_000), request_budget=1)
    assert late.inserted_count == 2 and late.dirty_from_ms == 9400
    revision = late.facts_revision

    await db.close()
    await db.connect()
    history = type(history)(db)
    capture = scheduled_capture(db, history)
    sync = HistorySynchronizer(reader, history, clock_ms=lambda: 10_200)
    assert await capture.prepare_window(scope, "BTCUSDT", 30_000) == Span(9000, 10_000)
    assert await capture.run_scheduled(
        scope,
        "BTCUSDT",
        snapshots=ScopeSnapshots([flat]),
        synchronizer=sync,
        now_ms=30_000,
        request_budget=1,
        clock_ms=manual_clock([9800, 9900, 12_100, 12_200]),
    )
    state = await history.projection_state(scope, "BTCUSDT")
    assert state == (revision, None)
    (closed,) = (await EpisodeRepository(db).list_closed(scope, symbol="BTCUSDT"))[0]
    assert closed.episode.closed_at == 9600 and closed.episode.realized_pnl_gross == D(40)
    assert len(await db.fetchall("SELECT * FROM follower_episodes WHERE closed_at IS NULL")) == 0
    assert (
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=ScopeSnapshots([flat]),
            synchronizer=sync,
            now_ms=40_000,
            request_budget=1,
            clock_ms=manual_clock([9800, 9900, 12_100, 12_200]),
        )
        is False
    )


async def test_prepare_returns_unclamped_import_hint(storage):
    db, history, scope = storage
    await commit_empty(history, scope, Window(0, 500_000_000))
    await commit_empty(history, scope, Window(500_000_000, 800_000_000))
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=None, now_ms=1)
    capture = scheduled_capture(db, history)
    # Seven days bound the exchange request, never the replay: the hint
    # spans the full retained coverage and identity comes from boundaries.
    assert await capture.prepare_window(scope, "BTCUSDT", 900_000_000) == Span(0, 800_000_000)


async def test_partial_start_publishes_close_of_unknown_open(storage):
    db, history, scope = storage
    flat = position(positionAmt="0")
    capture = scheduled_capture(db, history)
    window = Window(200, 300)
    await commit(history, scope, [fill(260, "SELL", "2", "100", "20")], window)
    (stored,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=boundary(flat, started=2400, received=2500),
        cutoff=300,
        recorded_at=5000,
    )
    assert stored.episode.closed_at == 260 and stored.episode.quality == "partial_history"


async def test_mid_position_window_publishes_partial_once(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "2")], window)
    row = position(positionAmt="5")
    capture = scheduled_capture(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(row),
        after=after_boundary(row),
        cutoff=100,
        recorded_at=5000,
    )
    (stored,) = await capture.publish_reconciled(**kwargs)
    assert stored.episode.quality == "partial_history"
    # A second publication of the same proven range is a no-op that returns
    # the same episode instead of minting a second identity.
    assert await capture.publish_reconciled(**kwargs) == (stored,)
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 1
    assert [row.event.kind for row in await EventRepository(db).list_owned(scope)] == [
        "POSITION_OPENED"
    ]


async def test_flat_window_publishes_settled_close(storage):
    db, history, scope = storage
    flat = position(positionAmt="0")
    capture = scheduled_capture(db, history)
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1"), fill(70, "SELL", "1", "100", "5")], window)
    (stored,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
        recorded_at=5000,
    )
    assert stored.episode.closed_at == 70
    opened, closed = await EventRepository(db).list_owned(scope)
    assert (opened.event.kind, closed.event.kind) == ("POSITION_OPENED", "POSITION_CLOSED")
    assert closed.event.trade_ref == stored.episode.public_ref


async def test_run_scheduled_publishes_and_records_checkpoint(storage):
    db, history, scope = storage
    window = Window(0, 1000)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((), (window,), (), (), 1)
    capture = scheduled_capture(db, history)
    published = await capture.run_scheduled(
        scope,
        "BTCUSDT",
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        now_ms=10000,
        clock_ms=manual_clock([0, 500, 4000, 4100]),
    )
    assert published is True
    state = await history.capture_state(scope, "BTCUSDT")
    assert (state["cap_start_ms"], state["cap_end_ms"], state["last_error"]) == (0, 1000, None)
    (row,) = await EventRepository(db).list_owned(scope)
    assert row.event.notify_eligible is True
    assert (
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=StaticSnapshots([position()]),
            synchronizer=HistorySynchronizer(reader, history),
            now_ms=20000,
            clock_ms=manual_clock([0, 500, 4000, 4100]),
        )
        is False
    )


async def test_run_scheduled_skips_without_coverage_leaving_no_trace(storage):
    db, history, scope = storage
    capture = scheduled_capture(db, history)
    assert (
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=StaticSnapshots([position()]),
            synchronizer=HistorySynchronizer(AsyncMock(), history),
            now_ms=10000,
        )
        is False
    )
    assert await history.capture_state(scope, "BTCUSDT") is None


async def test_run_scheduled_deferral_records_error_and_raises(storage):
    db, history, scope = storage
    window = Window(0, 1000)
    await commit(history, scope, [fill(60, "BUY", "2")], window)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((), (window,), (), (), 1)

    class MovingSnapshots:
        def __init__(self):
            self.calls = 0

        async def position_risk(self, account_id, symbol):
            self.calls += 1
            return [position(positionAmt="2" if self.calls == 1 else "5")]

    capture = scheduled_capture(db, history)
    with pytest.raises(HistoryReconciliationError, match="position changed"):
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=MovingSnapshots(),
            synchronizer=HistorySynchronizer(reader, history),
            now_ms=10000,
            clock_ms=manual_clock([0, 500, 4000, 4100]),
        )
    state = await history.capture_state(scope, "BTCUSDT")
    assert state["cap_start_ms"] == 0 and state["cap_end_ms"] is None
    assert state["last_error"] == "HistoryReconciliationError"


async def link_order(db, history, scope, order_id, coid, *, kind="MARKET_OPEN", payload=None):
    import json

    commands, links = CommandRepo(db), OrderLinkRepo(db)
    await db.execute_write(
        "INSERT INTO copy_trades(trade_id, symbol, pos_side, opened_at)"
        " VALUES (7,'BTCUSDT','BOTH','t')",
    )
    await commands.insert(
        f"cmd-{coid}",
        scope.account_id,
        None,
        kind,
        json.dumps(payload or {"symbol": "BTCUSDT"}),
        1,
        coid,
    )
    await commands.set_status(f"cmd-{coid}", "CONFIRMED")
    order = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": order_id, "clientOrderId": coid})
    await history.observe_order(scope, order, kind=kind, command_key=f"cmd-{coid}", observed_at=50)
    await links.add(7, scope.account_id, kind, coid)


def wired_capture(db, history):
    return HistoryCapture(
        history,
        EpisodeRepository(db),
        events=EventRepository(db),
        commands=CommandRepo(db),
    )


async def test_linked_fill_marks_episode_copy(storage):
    db, history, scope = storage
    await link_order(db, history, scope, 60, "ct-x")
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    (stored,) = await wired_capture(db, history).publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
        recorded_at=5000,
    )
    assert stored.episode.source == "copy"


async def test_partially_linked_window_stays_unknown(storage):
    db, history, scope = storage
    await link_order(db, history, scope, 60, "ct-x")
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1"), fill(61, "BUY", "1")], window)
    (stored,) = await wired_capture(db, history).publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(position(positionAmt="2")),
        after=after_boundary(position(positionAmt="2")),
        cutoff=100,
        recorded_at=5000,
    )
    assert stored.episode.source == "unknown"


async def test_confirmed_stop_observation_emits_protection_set(storage):
    db, history, scope = storage
    await link_order(
        db,
        history,
        scope,
        70,
        "ct-stop-1",
        kind="PLACE_STOP",
        payload={"symbol": "BTCUSDT", "stopPrice": "61900"},
    )
    await db.execute_write(
        "UPDATE follower_order_observations SET observed_at=60"
        " WHERE account_id=? AND client_order_id='ct-stop-1'",
        (scope.account_id,),
    )
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    await wired_capture(db, history).publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
        recorded_at=5000,
    )
    kinds = [row.event.kind for row in await EventRepository(db).list_owned(scope)]
    assert kinds == ["POSITION_OPENED", "PROTECTION_SET"]
    protection = (await EventRepository(db).list_owned(scope))[1].event
    assert protection.trade_ref is None and protection.occurred_at == 60
    command = await db.fetchone(
        "SELECT created_at FROM execution_commands WHERE command_key='cmd-ct-stop-1'"
    )
    assert protection.facts == {
        "symbol": "BTCUSDT",
        "slot": "stop",
        "new_price": "61900",
        "leg": "BOTH",
        "command_key": "cmd-ct-stop-1",
        "command_created_at": command["created_at"],
        "time_source": "observation",
    }
