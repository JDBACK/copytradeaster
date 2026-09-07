"""C1 stable projection: one identity across rolling windows (R2).

A rolling seven-day import must never mint a second episode for already
published facts; a late fact rolls replay back to the accepted origin and
re-derives the origin seeds instead of duplicating the trade.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

from copytradeaster.domain.models import PosSide
from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.models import ImportDelta, PositionSeed, ProjectionBoundary
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.history.replay import choose_replay_range
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from tests.unit.history.test_capture import (
    MutableFillReader,
    ScopeSnapshots,
    after_boundary,
    boundary,
    commit,
    manual_clock,
    position,
)
from tests.unit.history.test_projector import fill


def delta(changed_from=None, coverage_from=None, revision=1):
    return ImportDelta(
        inserted_count=0 if changed_from is None else 1,
        changed_from_ms=changed_from,
        coverage_changed=coverage_from is not None,
        coverage_changed_from_ms=coverage_from,
        verified_through_ms=100,
        facts_revision=revision,
    )


def latest_cutoff(cutoff=100, revision=1):
    return ProjectionBoundary(
        account_id="a",
        environment="testnet",
        symbol="BTCUSDT",
        cutoff_ms=cutoff,
        mode="settled",
        seeds=(),
        coverage_revision=revision,
        version=2,
    )


def origin_at(start=0):
    boundary = latest_cutoff()
    return ProjectionBoundary(
        account_id=boundary.account_id,
        environment=boundary.environment,
        symbol=boundary.symbol,
        cutoff_ms=start,
        mode=boundary.mode,
        seeds=(),
        coverage_revision=boundary.coverage_revision,
        version=1,
    )


async def test_replay_first_run_uses_fallback(storage):
    _, _, _ = storage
    decision = choose_replay_range(latest=None, origin=None, delta=delta(), fallback_start_ms=50)
    assert (decision.start_ms, decision.from_origin) == (50, False)


async def test_replay_resumes_after_proven_cutoff(storage):
    _, _, _ = storage
    decision = choose_replay_range(
        latest=latest_cutoff(100), origin=origin_at(), delta=delta(), fallback_start_ms=0
    )
    assert (decision.start_ms, decision.from_origin) == (101, False)


async def test_replay_late_fact_rolls_back_to_origin(storage):
    _, _, _ = storage
    decision = choose_replay_range(
        latest=latest_cutoff(100),
        origin=origin_at(),
        delta=delta(changed_from=40),
        fallback_start_ms=50,
    )
    assert (decision.start_ms, decision.from_origin) == (0, True)


async def test_replay_late_coverage_rolls_back_to_origin(storage):
    _, _, _ = storage
    decision = choose_replay_range(
        latest=latest_cutoff(100),
        origin=origin_at(),
        delta=delta(coverage_from=70),
        fallback_start_ms=50,
    )
    assert (decision.start_ms, decision.from_origin) == (0, True)


async def test_replay_growth_past_frontier_does_not_roll_back(storage):
    _, _, _ = storage
    decision = choose_replay_range(
        latest=latest_cutoff(100),
        origin=origin_at(),
        delta=delta(coverage_from=150),
        fallback_start_ms=50,
    )
    assert (decision.start_ms, decision.from_origin) == (101, False)


async def test_open_frontier_replays_from_origin_without_rederiving_seed(storage):
    db, history, scope = storage
    repo = EpisodeRepository(db)
    capture = HistoryCapture(history, repo)
    first_window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "2")], first_window)
    held = position(positionAmt="5")
    (first,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        first_window,
        before=boundary(held),
        after=after_boundary(held),
        cutoff=100,
    )
    origin = await repo.load_boundary(scope, "BTCUSDT", kind="origin")
    assert origin is not None and origin.mode == "partial"
    origin_seed = origin.seed_map()[PosSide.BOTH]
    assert origin_seed.quantity == D(3) and origin_seed.public_ref == first.episode.public_ref
    refs_before = len(await db.fetchall("SELECT * FROM follower_episode_refs"))

    second_window = Window(0, 200)
    await commit(
        history,
        scope,
        [fill(60, "BUY", "2"), fill(160, "BUY", "1", "130")],
        second_window,
    )
    held_more = position(positionAmt="6", updateTime=160)
    (second,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        second_window,
        before=boundary(held_more),
        after=boundary(held_more, started=2300, received=2400),
        cutoff=200,
    )
    assert second.episode.public_ref == first.episode.public_ref
    assert second.episode.quality == "partial_history"
    assert second.episode.remaining_qty == D(6) and second.episode.entry_qty == D(3)
    assert len(await db.fetchall("SELECT * FROM follower_episode_refs")) == refs_before


async def _scheduled_stage(
    capture,
    sync,
    reader,
    history,
    scope,
    rows,
    *,
    cutoff,
    quantity,
):
    reader.rows = list(rows)
    await sync.sync_fills(scope, "BTCUSDT", Window(9000, cutoff), request_budget=1)
    snapshots = ScopeSnapshots(
        [position(positionAmt=str(quantity), updateTime=rows[-1].time if rows else 9000)]
    )
    assert await capture.run_scheduled(
        scope,
        "BTCUSDT",
        snapshots=snapshots,
        synchronizer=sync,
        now_ms=cutoff + 10_000,
        request_budget=1,
        clock_ms=manual_clock([cutoff - 200, cutoff - 100, cutoff + 2100, cutoff + 2200]),
    )
    repo = EpisodeRepository(history._db)
    opens = await repo.list_open(scope, symbol="BTCUSDT")
    closed, _ = await repo.list_closed(scope, symbol="BTCUSDT")
    return opens[0] if opens else closed[0]


async def test_incremental_open_increase_partial_close_keeps_one_ref_and_full_basis(storage):
    db, history, scope = storage
    reader = MutableFillReader()
    sync = HistorySynchronizer(reader, history, clock_ms=lambda: 50_000)
    capture = HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))
    rows = [
        fill(10_000, "BUY", "2", "100", "0"),
        fill(11_000, "BUY", "1", "130", "0"),
        fill(12_000, "SELL", "1", "140", "10"),
        fill(13_000, "SELL", "2", "140", "60"),
    ]

    first = await _scheduled_stage(
        capture, sync, reader, history, scope, rows[:1], cutoff=10_000, quantity=D(2)
    )
    ref = first.episode.public_ref
    assert first.episode.entry_qty == D(2) and first.episode.average_entry == D(100)

    increased = await _scheduled_stage(
        capture, sync, reader, history, scope, rows[:2], cutoff=11_000, quantity=D(3)
    )
    assert increased.episode.public_ref == ref
    assert increased.episode.entry_qty == D(3) and increased.episode.average_entry == D(110)

    partial = await _scheduled_stage(
        capture, sync, reader, history, scope, rows[:3], cutoff=12_000, quantity=D(2)
    )
    assert partial.episode.public_ref == ref
    assert partial.episode.remaining_qty == D(2) and partial.episode.exit_qty == D(1)

    final = await _scheduled_stage(
        capture, sync, reader, history, scope, rows, cutoff=13_000, quantity=D(0)
    )
    episode = final.episode
    assert episode.public_ref == ref and episode.closed_at == 13_000
    assert episode.entry_qty == D(3) and episode.average_entry == D(110)
    assert episode.exit_qty == D(3) and episode.average_exit == D(140)
    assert episode.realized_pnl_gross == D(70) and episode.remaining_qty == D(0)
    assert [item.role for item in episode.allocations] == ["entry", "entry", "exit", "exit"]
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 1
    assert await EpisodeRepository(db).list_open(scope, symbol="BTCUSDT") == []
    assert len(await db.fetchall("SELECT * FROM follower_episode_allocations")) == 4
    assert [row.event.kind for row in await EventRepository(db).list_owned(scope)] == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
    ]


async def test_incremental_open_episode_ref_survives_restart_and_matches_batch(storage, tmp_path):
    db, history, scope = storage
    rows = [
        fill(10_000, "BUY", "2", "100", "0"),
        fill(11_000, "BUY", "1", "130", "0"),
        fill(12_000, "SELL", "1", "140", "10"),
        fill(13_000, "SELL", "2", "140", "60"),
    ]
    reader = MutableFillReader()
    sync = HistorySynchronizer(reader, history, clock_ms=lambda: 50_000)
    capture = HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))
    first = await _scheduled_stage(
        capture, sync, reader, history, scope, rows[:1], cutoff=10_000, quantity=D(2)
    )
    second = await _scheduled_stage(
        capture, sync, reader, history, scope, rows[:2], cutoff=11_000, quantity=D(3)
    )
    assert second.episode.public_ref == first.episode.public_ref

    # Restart exactly between increase and partial close.
    await db.close()
    await db.connect()
    history = HistoryRepository(db)
    sync = HistorySynchronizer(reader, history, clock_ms=lambda: 50_000)
    capture = HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))
    partial = await _scheduled_stage(
        capture, sync, reader, history, scope, rows[:3], cutoff=12_000, quantity=D(2)
    )
    final = await _scheduled_stage(
        capture, sync, reader, history, scope, rows, cutoff=13_000, quantity=D(0)
    )
    assert partial.episode.public_ref == final.episode.public_ref == first.episode.public_ref
    events_before = await EventRepository(db).list_owned(scope)
    allocations_before = await db.fetchall("SELECT * FROM follower_episode_allocations")
    assert (
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=ScopeSnapshots([position(positionAmt="0", updateTime=13_000)]),
            synchronizer=sync,
            now_ms=40_000,
            request_budget=1,
        )
        is False
    )
    assert await EventRepository(db).list_owned(scope) == events_before
    assert await db.fetchall("SELECT * FROM follower_episode_allocations") == allocations_before

    # Independent batch publication over the same facts must have identical
    # financial/position truth and logical transitions (UUIDs are DB-local).
    batch_db = Database(tmp_path / "history-batch.sqlite3")
    await batch_db.connect()
    await batch_db.migrate()
    await batch_db.execute_write(
        "INSERT INTO accounts(id,role,label,user_address) VALUES "
        "('batch','follower','batch','batch')"
    )
    try:
        batch_scope = HistoryScope("batch", "testnet")
        batch_history = HistoryRepository(batch_db)
        full = Window(9000, 13_000)
        await batch_history.commit_fills(
            batch_scope,
            "BTCUSDT",
            full,
            WindowRead(tuple(rows), (full,), (), (), 1),
            checked_at=50_000,
        )
        batch_events = EventRepository(batch_db)
        (batch,) = await HistoryProjection(
            batch_history, EpisodeRepository(batch_db), batch_events
        ).publish_window(
            batch_scope,
            "BTCUSDT",
            full,
            seeds={PosSide.BOTH: PositionSeed(D(0))},
            ending={PosSide.BOTH: D(0)},
            recorded_at=60_000,
        )
        assert replace(batch.episode, public_ref=final.episode.public_ref) == final.episode
        logical_incremental = [
            (item.event.kind, item.event.occurred_at, item.event.facts) for item in events_before
        ]
        logical_batch = [
            (item.event.kind, item.event.occurred_at, item.event.facts)
            for item in await batch_events.list_owned(batch_scope)
        ]
        assert logical_batch == logical_incremental
    finally:
        await batch_db.close()


async def test_rolling_window_keeps_single_episode(storage):
    """R2 repro: [0,100] then [50,100] must not duplicate the closed trade."""
    db, history, scope = storage
    window = Window(0, 100)
    await commit(
        history,
        scope,
        [fill(10, "BUY", "2"), fill(60, "SELL", "2", "110", "20")],
        window,
    )
    flat = position(positionAmt="0")
    capture = HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
        recorded_at=5000,
    )
    (first,) = await capture.publish_reconciled(window=window, **kwargs)
    assert first.episode.realized_pnl_gross == D(20)
    (second,) = await capture.publish_reconciled(window=Window(50, 100), **kwargs)
    assert second.episode.public_ref == first.episode.public_ref
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 1
    kinds = [row.event.kind for row in await EventRepository(db).list_owned(scope)]
    assert kinds == ["POSITION_OPENED", "POSITION_CLOSED"]


async def test_passage_past_cutoff_extends_boundary_without_rewrite(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(
        history,
        scope,
        [fill(10, "BUY", "2"), fill(60, "SELL", "2", "110", "20")],
        window,
    )
    flat = position(positionAmt="0")
    capture = HistoryCapture(history, EpisodeRepository(db))
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
    )
    (first,) = await capture.publish_reconciled(window=window, **kwargs)
    await commit(history, scope, [], Window(100, 200))
    # Nothing new to project: the call extends the proven frontier and
    # returns no episodes instead of rewriting the closed trade.
    assert await capture.publish_reconciled(window=Window(100, 200), **kwargs) == ()
    repo = EpisodeRepository(db)
    assert (await repo.load_boundary(scope, "BTCUSDT")).cutoff_ms == 200
    assert (await repo.get(scope, first.episode.public_ref)).episode == first.episode


async def test_late_fill_replays_from_origin_without_duplicate(storage):
    """A fill arriving after its window was proven joins the same episode.

    The late entry moves the episode start, so the replay mints the
    entry-anchored ref and records the proven predecessor as an alias:
    one surviving row, entry 2, no second card.
    """
    db, history, scope = storage
    await commit(history, scope, [fill(60, "BUY", "1")], Window(0, 100))
    held = position(positionAmt="1")
    capture = HistoryCapture(history, EpisodeRepository(db))
    (first,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        Window(0, 100),
        before=boundary(held),
        after=after_boundary(held),
        cutoff=100,
        recorded_at=5000,
    )
    assert first.episode.opened_at == 60 and first.episode.quality == "complete"
    # The late entry surfaces with the same coverage; the delta proves the
    # change sits before the proven cutoff, so replay restarts at the origin
    # and re-derives a zero start instead of stacking a second episode.
    late = await history.commit_fills(
        scope,
        "BTCUSDT",
        Window(0, 100),
        WindowRead((fill(40, "BUY", "1"), fill(60, "BUY", "1")), (Window(0, 100),), (), (), 1),
        checked_at=4,
    )
    assert late.changed_from_ms == 40 and late.inserted_count == 1
    held2 = position(positionAmt="2")
    (second,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        Window(0, 100),
        before=boundary(held2),
        after=after_boundary(held2),
        cutoff=100,
        recorded_at=6000,
        delta=late,
    )
    assert second.episode.entry_qty == D(2) and second.episode.remaining_qty == D(2)
    assert second.episode.opened_at == 40
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 1
    repo = EpisodeRepository(db)
    resolution = await repo.get_resolution(scope, first.episode.public_ref)
    assert resolution.kind == "alias" and resolution.targets == (second.episode.public_ref,)
    assert await repo.get(scope, first.episode.public_ref) == await repo.get(
        scope, second.episode.public_ref
    )


async def test_repeat_import_reports_no_change(storage):
    _, history, scope = storage
    window = Window(0, 100)
    rows = (fill(60, "BUY", "1"),)
    first = await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(rows, (window,), (), (), 1), checked_at=3
    )
    assert (first.inserted_count, first.coverage_changed) == (1, True)
    revision = first.facts_revision
    second = await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(rows, (window,), (), (), 1), checked_at=4
    )
    assert second.inserted_count == 0 and second.coverage_changed is False
    assert second.changed_from_ms is None and second.facts_revision == revision


async def test_rebuild_silently_corrects_legacy_duplicate(storage):
    """Managed replay from saved facts merges a stale duplicate via alias."""
    from dataclasses import replace as dc_replace

    from copytradeaster.domain.models import PosSide
    from copytradeaster.history.projection import HistoryProjection

    db, history, scope = storage
    window = Window(0, 100)
    await commit(
        history,
        scope,
        [fill(10, "BUY", "2"), fill(60, "SELL", "2", "110", "20")],
        window,
    )
    flat = position(positionAmt="0")
    events = EventRepository(db)
    capture = HistoryCapture(history, EpisodeRepository(db), events=events)
    (first,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
        recorded_at=5000,
    )
    # A legacy duplicate: same entry fill under a second ref, as the rolling
    # window used to mint before C1.
    repo = EpisodeRepository(db)
    dup_ref = await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, "legacy:dup")
    dup = dc_replace(first.episode, public_ref=dup_ref)
    await repo.save(scope, dup, expected_revision=0)
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 2
    before_events = len(await events.list_owned(scope))
    projection = HistoryProjection(history, repo, events)
    fresh = await projection.rebuild_scope(
        scope, "BTCUSDT", ending={PosSide.BOTH: D(0)}, recorded_at=6000
    )
    assert [stored.episode.public_ref for stored in fresh] == [first.episode.public_ref]
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 1
    resolution = await repo.get_resolution(scope, dup_ref)
    assert resolution.kind == "alias" and resolution.targets == (first.episode.public_ref,)
    # The correction is silent for push: no new events for either ref.
    assert len(await events.list_owned(scope)) == before_events


async def test_rebuild_requires_accepted_origin(storage):
    import pytest

    from copytradeaster.domain.models import PosSide
    from copytradeaster.history.projection import HistoryProjection
    from copytradeaster.history.reconcile import HistoryReconciliationError

    db, history, scope = storage
    projection = HistoryProjection(history, EpisodeRepository(db))
    with pytest.raises(HistoryReconciliationError, match="accepted origin"):
        await projection.rebuild_scope(
            scope, "BTCUSDT", ending={PosSide.BOTH: D(0)}, recorded_at=6000
        )


async def test_ambiguous_duplicate_records_split_and_keeps_row(storage):
    """No surviving episode continues the ref: verdict without deletion."""
    from dataclasses import replace as dc_replace

    from copytradeaster.domain.models import PosSide
    from copytradeaster.history.projection import HistoryProjection

    db, history, scope = storage
    window = Window(0, 100)
    await commit(
        history,
        scope,
        [fill(10, "BUY", "2"), fill(60, "SELL", "2", "110", "20")],
        window,
    )
    flat = position(positionAmt="0")
    capture = HistoryCapture(history, EpisodeRepository(db))
    (first,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
        recorded_at=5000,
    )
    # A stale row whose entry fill exists nowhere in the replay: neither an
    # update nor an alias, so the row survives with a split verdict.
    repo = EpisodeRepository(db)
    stray_ref = await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, "legacy:stray")
    unknown_entry = dc_replace(first.episode.allocations[0], trade_id=9999)
    stray = dc_replace(
        first.episode,
        public_ref=stray_ref,
        allocations=(unknown_entry,) + first.episode.allocations[1:],
    )
    await repo.save(scope, stray, expected_revision=0)
    projection = HistoryProjection(history, repo)
    fresh = await projection.rebuild_scope(
        scope, "BTCUSDT", ending={PosSide.BOTH: D(0)}, recorded_at=6000
    )
    assert [s.episode.public_ref for s in fresh] == [first.episode.public_ref]
    assert len(await db.fetchall("SELECT * FROM follower_episodes")) == 2
    resolution = await repo.get_resolution(scope, stray_ref)
    assert resolution.kind == "split" and resolution.targets == ()
    assert resolution.reason


async def test_recapture_after_frontier_growth_returns_stored_episode(storage):
    """Restart with a stale hint window: the grown frontier projects nothing
    new, but the call reports the requested window overlap, not emptiness."""
    from unittest.mock import AsyncMock

    from copytradeaster.history.capture import HistoryCapture
    from copytradeaster.history.reader import WindowRead
    from copytradeaster.history.sync import HistorySynchronizer
    from tests.unit.history.test_capture import StaticSnapshots

    db, history, scope = storage
    window = Window(0, 100)
    await commit(
        history,
        scope,
        [fill(10, "BUY", "2"), fill(60, "SELL", "2", "110", "20")],
        window,
    )
    flat = position(positionAmt="0")
    capture = HistoryCapture(history, EpisodeRepository(db))
    (first,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
        recorded_at=5000,
    )
    # Fresh objects over the same file, coverage grown over an empty range.
    await commit(history, scope, [], Window(100, 200))
    reader = AsyncMock()

    def _read(account_id, symbol, requested, request_budget=8):
        return WindowRead((), (requested,), (), (), 1)

    reader.fills.side_effect = _read
    clocks = [0, 100, 2200, 2300]
    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([flat]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=lambda: clocks.pop(0) if clocks else 2300,
    )
    assert published.end == 200
    assert [s.episode.public_ref for s in stored] == [first.episode.public_ref]
