"""Read one episode's proven money for notification copy.

The close card must not print a second competing number: when every cost is
proven it prints the net result, and while the costs are still unproven it says
so instead of showing a gross figure the user would read as final. This module
is the read port behind that decision, so the notification layer never talks to
history repositories itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from copytradeaster.application.models import EpisodeMoneyView
from copytradeaster.history.accounting import EpisodeMoneyStale, load_episode_money
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope


@runtime_checkable
class EpisodeMoneyReader(Protocol):
    async def money_for(
        self, scope: HistoryScope, trade_ref: str
    ) -> Mapping[str, EpisodeMoneyView]:
        """Proven money for one owned trade ref; empty when it is not provable."""
        ...


class SqliteEpisodeMoneyReader:
    """Batch accountant over durable storage; never guesses a missing component.

    ``load_episode_money`` raises when the durable facts moved under the read, so
    a concurrent publication falls back to the honest gross copy instead of
    printing mixed-revision money.
    """

    def __init__(
        self,
        episodes: EpisodeRepository,
        history: HistoryRepository,
        *,
        quantity_assets: Mapping[str, str | None] | None = None,
    ) -> None:
        self._episodes = episodes
        self._history = history
        self._quantity_assets = dict(quantity_assets or {})

    async def money_for(
        self, scope: HistoryScope, trade_ref: str
    ) -> Mapping[str, EpisodeMoneyView]:
        if not isinstance(trade_ref, str) or not trade_ref:
            return {}
        stored = await self._episodes.get(scope, trade_ref)
        if stored is None:
            return {}
        try:
            batch = await load_episode_money(
                scope,
                (stored.episode.public_ref,),
                history_repo=self._history,
                episodes_repo=self._episodes,
            )
        except (EpisodeMoneyStale, ValueError):
            # Concurrent publication or an unusable ref: the card falls back to
            # the honest gross copy rather than printing mixed-revision money.
            return {}
        money = batch.money.get(stored.episode.public_ref)
        if money is None:
            return {}
        # Imported here, not at module scope: trade_views imports the
        # notification cards, which import this package's router.
        from copytradeaster.application.trade_views import present_trade

        view = present_trade(
            stored.episode,
            quantity_asset=self._quantity_assets.get(stored.episode.symbol),
            money=money,
            revision=stored.revision,
        ).money
        if view is None:
            return {}
        return {stored.episode.public_ref: view, trade_ref: view}


__all__ = ["EpisodeMoneyReader", "SqliteEpisodeMoneyReader"]


__all__ = ["EpisodeMoneyReader", "SqliteEpisodeMoneyReader"]
