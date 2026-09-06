"""C1 stable projection: one identity across rolling windows (R2).

A rolling seven-day import must never mint a second episode for already
published facts; a late fact rolls replay back to the accepted origin and
re-derives the origin seeds instead of duplicating the trade.
"""

from __future__ import annotations

from decimal import Decimal as D

from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.models import ImportDelta, ProjectionBoundary
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.history.replay import choose_replay_range
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from tests.unit.history.test_capture import after_boundary, boundary, commit, position
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
