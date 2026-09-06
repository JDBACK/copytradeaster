"""Publish a seeded, quantity-reconciled history window from durable fills.

The caller owns the authoritative boundary snapshots and must serialize this
operation against importing the same account/symbol. Runtime snapshot acquisition
and bounded incremental checkpoints are separate from this pure/read-model seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

from copytradeaster.domain.models import PosSide
from copytradeaster.history.event_builder import episode_events
from copytradeaster.history.models import Episode, PositionSeed, TradeSource
from copytradeaster.history.projector import project_episodes
from copytradeaster.history.protection import load_protection_facts, protection_events
from copytradeaster.history.reader import Window
from copytradeaster.history.reconcile import (
    HistoryReconciliationError,
    require_coverage,
    require_ending_quantities,
)
from copytradeaster.storage.episodes import EpisodeRepository, StoredEpisode
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import CommandRepo


class HistoryProjection:
    """Publish windows with order-link sources and protection events.

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
        window: Window,
        *,
        seeds: Mapping[PosSide, PositionSeed],
        ending: Mapping[PosSide, Decimal],
        order_sources: Mapping[int, TradeSource] | None = None,
        recorded_at: int | None = None,
        notify_eligible: bool = False,
    ) -> tuple[StoredEpisode, ...]:
        windows = await self._history.windows(scope, "fills", symbol)
        relevant = [
            row
            for row in windows
            if row["start_ms"] <= window.end and row["end_ms"] >= window.start
        ]
        if any(row["state"] != "complete" for row in relevant):
            raise HistoryReconciliationError("import window is still incomplete")
        require_coverage(window, [(row["start_ms"], row["end_ms"]) for row in relevant])
        fills = await self._history.fills(scope, symbol, window)
        if order_sources is None:
            order_sources = await self._history.proven_order_sources(scope, symbol)
        projected = project_episodes(
            fills,
            symbol=symbol,
            seeds=seeds,
            order_sources=order_sources,
            reference=lambda fill, direction: f"fill:{fill.trade_id}:{direction}",
        )
        require_ending_quantities(projected, ending)
        seed_refs = {seed.public_ref for seed in seeds.values() if seed.public_ref is not None}
        resolved = []
        revisions = {}
        previous: dict[str, Episode] = {}
        for episode in projected:
            ref = episode.public_ref
            if ref not in seed_refs:
                ref = await self._episodes.reserve_ref(scope, symbol, episode.position_side, ref)
            stored = await self._episodes.get(scope, ref)
            if stored is not None:
                ref = stored.episode.public_ref
                previous[ref] = stored.episode
            revisions[ref] = stored.revision if stored else 0
            resolved.append(replace(episode, public_ref=ref))
        drafts = []
        if self._events is not None:
            if recorded_at is None:
                raise ValueError("event emission requires recorded_at")
            fills_by_id = {fill.trade_id: fill for fill in fills}
            drafts = episode_events(
                previous,
                resolved,
                fills_by_id,
                notify_eligible=notify_eligible,
                recorded_at=recorded_at,
            )
            if self._commands is not None:
                baseline, facts = await load_protection_facts(
                    self._history, self._commands, scope, symbol, window.start, window.end
                )
                drafts.extend(
                    protection_events(
                        symbol,
                        baseline,
                        facts,
                        recorded_at=recorded_at,
                        notify_eligible=notify_eligible,
                    )
                )
        return await self._episodes.publish(scope, symbol, resolved, revisions, events=drafts)
