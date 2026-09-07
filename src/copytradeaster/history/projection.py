"""Publish a seeded, quantity-reconciled history projection from durable fills.

The caller owns the authoritative boundary snapshots and must serialize this
operation against importing the same account/symbol. Replay always restarts
at the last proven boundary (or the accepted origin after a late change);
completed episodes outside the replay range are never rewritten. One atomic
commit carries episodes, lineage resolutions, events and the new boundary:
a crash before commit leaves the previous publication and a dirty range.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal

from copytradeaster.domain.models import PosSide
from copytradeaster.history.event_builder import episode_events
from copytradeaster.history.models import (
    Episode,
    ImportDelta,
    PositionSeed,
    ProjectionBoundary,
    ProjectionResult,
    ReplayDecision,
    TradeResolutionDTO,
    TradeSource,
)
from copytradeaster.history.projector import project_with_transitions
from copytradeaster.history.protection import load_protection_facts, protection_events
from copytradeaster.history.reader import Span, Window
from copytradeaster.history.reconcile import (
    HistoryReconciliationError,
    require_coverage,
    require_ending_quantities,
)
from copytradeaster.history.replay import choose_replay_range
from copytradeaster.history.snapshot import derive_starting_seeds
from copytradeaster.storage.episodes import EpisodeRepository, StoredEpisode
from copytradeaster.storage.events import EventRepository, StoredEvent
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import CommandRepo


class HistoryProjection:
    """Publish projections with order-link sources and protection events.

    Order sources resolve from durable observations/journal/links unless the
    caller passes proven ones explicitly; unknown stays unknown. Protection
    drafts need the journal, so they emit only when ``commands`` is wired.
    """

    def __init__(
        self,
        history: HistoryRepository,
        episodes: EpisodeRepository,
        events: EventRepository | None = None,
        commands: CommandRepo | None = None,
    ) -> None:
        self._history = history
        self._episodes = episodes
        self._events = events
        self._commands = commands

    async def publish_window(
        self,
        scope: HistoryScope,
        symbol: str,
        window: Window | Span,
        *,
        seeds: Mapping[PosSide, PositionSeed],
        ending: Mapping[PosSide, Decimal],
        order_sources: Mapping[int, TradeSource] | None = None,
        recorded_at: int | None = None,
        notify_eligible: bool = False,
        delta: ImportDelta | None = None,
    ) -> tuple[StoredEpisode, ...]:
        """Project one window, replaying from the durable boundary when present.

        The first acceptance uses the caller seeds and anchors them as the
        origin; later calls resume from the stored boundary seeds so a
        rolling import window never mints a second identity for a known
        episode. With the import ``delta`` a late change at or before the
        proven cutoff rolls replay back to the accepted origin — and the
        origin seeds are re-derived from the authoritative ending over all
        retained fills, because the first inference may itself have missed
        the late fact. Without a delta the call simply resumes after the
        latest cutoff. Partial (open-start) projections publish with
        ``partial_history`` quality instead of being refused.
        """
        if not symbol:
            raise ValueError("projection symbol required")
        latest = await self._episodes.load_boundary(scope, symbol)
        if latest is None:
            return await self.publish_projection(
                scope,
                symbol,
                replay_start_ms=window.start,
                cutoff_ms=window.end,
                seeds=dict(seeds),
                ending=ending,
                coverage_revision=await self._history.facts_revision(scope, symbol),
                boundary_version=None,
                origin=(window.start, dict(seeds)),
                order_sources=order_sources,
                recorded_at=recorded_at,
                notify_eligible=notify_eligible,
            )
        origin = await self._episodes.load_boundary(scope, symbol, kind="origin")
        current_revision, dirty_from = await self._history.projection_state(scope, symbol)
        effective_delta = (
            ImportDelta(
                inserted_count=0,
                changed_from_ms=None,
                coverage_changed=False,
                coverage_changed_from_ms=None,
                verified_through_ms=None,
                facts_revision=current_revision,
                dirty_from_ms=dirty_from,
            )
            if delta is None
            else replace(delta, facts_revision=current_revision, dirty_from_ms=dirty_from)
        )
        decision = choose_replay_range(
            latest=latest, origin=origin, delta=effective_delta, fallback_start_ms=window.start
        )
        if decision.from_origin and origin is None:
            raise HistoryReconciliationError("open/corrected replay requires an accepted origin")
        replay_start = decision.start_ms
        correction: tuple[int, dict[PosSide, PositionSeed]] | None = None
        if decision.from_origin and decision.refresh_origin_seed and origin is not None:
            # The first inference may have missed this late fact, so walk
            # the authoritative ending back over everything retained.
            origin_refs = {
                leg: seed.public_ref
                for leg, seed in origin.seed_map().items()
                if seed.public_ref is not None
            }
            replay_fills = await self._history.fills(
                scope, symbol, Span(origin.cutoff_ms, window.end)
            )
            correction = (
                origin.cutoff_ms,
                derive_starting_seeds(
                    symbol,
                    ending,
                    replay_fills,
                    placeholder_ref=lambda leg: origin_refs.get(
                        leg, f"seed:{origin.cutoff_ms}:{leg.value}"
                    ),
                ),
            )
        if replay_start > window.end:
            # The proven frontier already covers the requested range: there is
            # nothing new to project, so return what the range already shows.
            # At the exact frontier the ending snapshot must still agree with
            # the proven seeds; a contradiction fails instead of passing.
            if window.end == latest.cutoff_ms and (
                set(ending) != set(latest.seed_map())
                or any(ending[leg] != seed.quantity for leg, seed in latest.seed_map().items())
            ):
                raise HistoryReconciliationError("stored projection disagrees with ending snapshot")
            return tuple(await self._episodes.list_range(scope, symbol, window.start, window.end))
        seeds_for_replay = (
            dict(correction[1])
            if correction is not None
            else origin.seed_map()
            if decision.from_origin and origin is not None
            else latest.seed_map()
        )
        saved = await self.publish_projection(
            scope,
            symbol,
            replay_start_ms=replay_start,
            cutoff_ms=window.end,
            seeds=seeds_for_replay,
            ending=ending,
            coverage_revision=current_revision,
            boundary_version=latest.version,
            origin=correction,
            order_sources=order_sources,
            recorded_at=recorded_at,
            notify_eligible=notify_eligible,
        )
        if saved:
            return saved
        # The replay grew the proven frontier but projected nothing new (no
        # fills in the added range): report what the requested window
        # already shows instead of an empty publication.
        return tuple(await self._episodes.list_range(scope, symbol, window.start, window.end))

    async def publish_projection(
        self,
        scope: HistoryScope,
        symbol: str,
        *,
        replay_start_ms: int,
        cutoff_ms: int,
        seeds: Mapping[PosSide, PositionSeed],
        ending: Mapping[PosSide, Decimal],
        coverage_revision: int,
        boundary_version: int | None,
        origin: tuple[int, dict[PosSide, PositionSeed]] | None,
        order_sources: Mapping[int, TradeSource] | None = None,
        recorded_at: int | None = None,
        notify_eligible: bool = False,
        silent_refs: frozenset[str] = frozenset(),
        now_ms: int | None = None,
    ) -> tuple[StoredEpisode, ...]:
        """Project ``[replay_start_ms, cutoff_ms]`` and commit one publication."""
        if not symbol:
            raise ValueError("projection symbol required")
        if type(replay_start_ms) is not int or type(cutoff_ms) is not int:
            raise ValueError("projection range must be integer times")
        if replay_start_ms < 0 or cutoff_ms < replay_start_ms:
            raise ValueError("projection range must be ordered")
        replay = Span(replay_start_ms, cutoff_ms)
        for leg, seed in seeds.items():
            if seed.public_ref is not None:
                ref = await self._episodes.resolve_ref(scope, symbol, leg, seed.public_ref)
                if ref != seed.public_ref:
                    seeds = dict(seeds)
                    seeds[leg] = replace(seed, public_ref=ref)
        windows = await self._history.windows(scope, "fills", symbol)
        relevant = [
            row
            for row in windows
            if row["start_ms"] <= replay.end and row["end_ms"] >= replay.start
        ]
        if any(row["state"] != "complete" for row in relevant):
            raise HistoryReconciliationError("import window is still incomplete")
        require_coverage(replay, [(row["start_ms"], row["end_ms"]) for row in relevant])
        fills = await self._history.fills(scope, symbol, replay)
        if order_sources is None:
            order_sources = await self._history.proven_order_sources(scope, symbol)
        projection = project_with_transitions(
            fills,
            symbol=symbol,
            seeds=seeds,
            order_sources=order_sources,
            reference=lambda fill, direction: f"fill:{fill.trade_id}:{direction}",
        )
        require_ending_quantities(projection.episodes, ending)
        seed_refs = {seed.public_ref for seed in seeds.values() if seed.public_ref is not None}
        resolved = []
        revisions = {}
        previous: dict[str, Episode] = {}
        for episode in projection.episodes:
            ref = episode.public_ref
            if ref not in seed_refs:
                ref = await self._episodes.reserve_ref(scope, symbol, episode.position_side, ref)
            stored = await self._episodes.get(scope, ref)
            if stored is not None:
                ref = stored.episode.public_ref
                previous[ref] = stored.episode
            revisions[ref] = stored.revision if stored else 0
            resolved.append(replace(episode, public_ref=ref))
        anchors = {
            projected.public_ref: stored.public_ref
            for projected, stored in zip(projection.episodes, resolved, strict=True)
        }
        result = ProjectionResult(
            episodes=tuple(resolved),
            transitions=tuple(
                replace(transition, ref=anchors[transition.ref])
                for transition in projection.transitions
            ),
        )
        affected = {
            stored.episode.public_ref: stored.episode
            for stored in await self._episodes.list_range(scope, symbol, replay_start_ms, cutoff_ms)
        }
        resolutions = self._corrections(scope, symbol, resolved, affected)
        # A correction continues an already announced episode under its
        # surviving ref: repeat corrections create no new aliases or events.
        quiet = set(silent_refs)
        for resolution in resolutions:
            if resolution.kind in ("alias", "merge"):
                quiet.update(resolution.targets)
        drafts = []
        if self._events is not None:
            if recorded_at is None:
                raise ValueError("event emission requires recorded_at")
            fills_by_id = {fill.trade_id: fill for fill in fills}
            refs = [episode.public_ref for episode in resolved]
            existing = await self._existing_events(scope, refs)
            drafts = [
                draft
                for draft in episode_events(
                    previous,
                    result,
                    fills_by_id,
                    existing,
                    account_id=scope.account_id,
                    environment=scope.environment,
                    notify_eligible=notify_eligible,
                    recorded_at=recorded_at,
                )
                # Alias/merge continuations under a surviving ref stay quiet:
                # no repeat events and no correction for an already announced
                # episode; the lineage resolution records the rename.
                if draft.trade_ref not in quiet
            ]
            if self._commands is not None:
                baseline, facts = await load_protection_facts(
                    self._history, self._commands, scope, symbol, replay.start, replay.end
                )
                drafts.extend(
                    draft
                    for draft in protection_events(
                        symbol,
                        baseline,
                        facts,
                        recorded_at=recorded_at,
                        notify_eligible=notify_eligible,
                    )
                    if draft.trade_ref not in quiet
                )
        open_refs = {
            episode.position_side: episode.public_ref
            for episode in resolved
            if episode.closed_at is None
        }
        end_seeds = {
            leg: PositionSeed(qty, open_refs.get(leg) if qty != 0 else None)
            for leg, qty in ending.items()
        }
        boundary = ProjectionBoundary(
            account_id=scope.account_id,
            environment=scope.environment,
            symbol=symbol,
            cutoff_ms=cutoff_ms,
            mode="partial" if any(qty != 0 for qty in ending.values()) else "settled",
            seeds=tuple(end_seeds.items()),
            coverage_revision=coverage_revision,
            version=(1 if boundary_version is None else boundary_version + 1),
        )
        stamp = recorded_at if recorded_at is not None else now_ms
        saved = await self._episodes.publish(
            scope,
            symbol,
            resolved,
            revisions,
            events=drafts,
            resolutions=resolutions,
            boundary=boundary,
            origin_seeds=tuple(origin[1].items()) if origin is not None else None,
            origin_start_ms=origin[0] if origin is not None else None,
            now_ms=cutoff_ms if stamp is None else stamp,
            expected_facts_revision=coverage_revision,
            consume_dirty_through_ms=cutoff_ms,
        )
        if saved or boundary_version is None:
            return saved
        # The replay grew the proven frontier but projected nothing new
        # (no fills in the added range): report what the replayed range
        # already shows instead of an empty publication.
        return tuple(await self._episodes.list_range(scope, symbol, replay_start_ms, cutoff_ms))

    async def _existing_events(
        self, scope: HistoryScope, trade_refs: Sequence[str]
    ) -> list[StoredEvent]:
        """Stored rows for resolved trades, all pages, oldest first.

        Detection input for the transition event builder: modern
        ``hist1:fill:`` rows prove a transition already published, legacy
        ``evt:*`` rows mark superseded lineage to link. Paged on the
        per-trade index so large episodes never truncate the check.
        """
        assert self._events is not None
        rows: list[StoredEvent] = []
        seen: set[str] = set()
        for trade_ref in trade_refs:
            if trade_ref in seen:
                continue
            seen.add(trade_ref)
            after_seq = 0
            while True:
                page = await self._events.list_by_trade(
                    scope, trade_ref, after_seq=after_seq, limit=200
                )
                rows.extend(page)
                if len(page) < 200:
                    break
                after_seq = page[-1].seq
        return rows

    def _corrections(
        self,
        scope: HistoryScope,
        symbol: str,
        resolved: Sequence[Episode],
        previous: Mapping[str, Episode],
    ) -> list[TradeResolutionDTO]:
        """Alias superseded stored refs onto matching fresh episodes.

        A stored episode overlapping the replay that the new projection no
        longer produces is a stale duplicate: when its entry fills reappear
        under a surviving ref it becomes an alias (same revision chain
        continues there); otherwise it is recorded as a split for review and
        left untouched. Repeat corrections resolve identically and create no
        new aliases.
        """
        del scope, symbol
        fresh_refs = {episode.public_ref for episode in resolved}
        entry_of: dict[str, set[int]] = {}
        for episode in resolved:
            entry_of.setdefault(episode.public_ref, set()).update(
                alloc.trade_id for alloc in episode.allocations if alloc.role == "entry"
            )
        resolutions = []
        for ref, old in previous.items():
            if ref in fresh_refs:
                continue
            old_entries = {alloc.trade_id for alloc in old.allocations if alloc.role == "entry"}
            match = next(
                (fresh for fresh, entries in entry_of.items() if old_entries & entries),
                None,
            )
            if match is not None:
                resolutions.append(
                    TradeResolutionDTO(
                        ref=ref,
                        kind="alias",
                        targets=(match,),
                        reason="replay correction: entry fills continue under surviving ref",
                        revision=0,
                    )
                )
            else:
                resolutions.append(
                    TradeResolutionDTO(
                        ref=ref,
                        kind="split",
                        targets=(),
                        reason="replay correction: no surviving episode continues this ref",
                        revision=0,
                    )
                )
        return resolutions

    async def rebuild_scope(
        self,
        scope: HistoryScope,
        symbol: str,
        *,
        ending: Mapping[PosSide, Decimal],
        recorded_at: int,
        order_sources: Mapping[int, TradeSource] | None = None,
    ) -> tuple[StoredEpisode, ...]:
        """Managed replay from saved facts; silent for push.

        Reprojects everything from the accepted origin over retained fills,
        corrects superseded duplicates via resolutions, and emits events only
        for brand-new refs. Never deletes fills, cashflows or the journal.
        """
        origin = await self._episodes.load_boundary(scope, symbol, kind="origin")
        if origin is None:
            raise HistoryReconciliationError("rebuild requires an accepted origin")
        latest = await self._episodes.load_boundary(scope, symbol)
        cutoff = latest.cutoff_ms if latest is not None else origin.cutoff_ms
        stored = await self._episodes.list_symbol(scope, symbol)
        fresh = await self.publish_projection(
            scope,
            symbol,
            replay_start_ms=origin.cutoff_ms,
            cutoff_ms=cutoff,
            seeds=origin.seed_map(),
            ending=ending,
            coverage_revision=await self._history.facts_revision(scope, symbol),
            boundary_version=latest.version if latest is not None else None,
            origin=None,
            order_sources=order_sources,
            recorded_at=recorded_at,
            notify_eligible=False,
            silent_refs=frozenset(ep.episode.public_ref for ep in stored),
            now_ms=recorded_at,
        )
        return fresh

    async def plan_replay(
        self,
        scope: HistoryScope,
        symbol: str,
        delta: ImportDelta,
        fallback_start_ms: int,
    ) -> ReplayDecision:
        """Pure replay start from durable boundaries and one import delta."""
        latest = await self._episodes.load_boundary(scope, symbol)
        origin = await self._episodes.load_boundary(scope, symbol, kind="origin")
        return choose_replay_range(
            latest=latest, origin=origin, delta=delta, fallback_start_ms=fallback_start_ms
        )
