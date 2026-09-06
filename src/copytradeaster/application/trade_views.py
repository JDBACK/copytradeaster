"""Shared safe presenters over proven history facts (S1.4, plan §10.8 B7).

Orchestration and ownership stay in the application service; every
transformation of episodes, fills and user events into UI DTOs lives here
so the Mini App, the bot and notifications share one meaning. No I/O,
no exchange calls, no guessing: unknown directions stay null, net stays
absent until full attribution, and money travels as canonical strings.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from copytradeaster.application.models import (
    AccountOverview,
    ActivityFilter,
    EpisodeSource,
    FillFacts,
    HistoryQuality,
    PositionSide,
    PositionSummaryDTO,
    RiskCoverage,
    RiskPreviewStep,
    SnapshotFreshness,
    TradeDetailDTO,
    TradeEventItem,
    TradeListItem,
    TradeState,
)
from copytradeaster.aster.models import UserTradeDTO
from copytradeaster.history.models import Episode
from copytradeaster.storage.episodes import StoredEpisode
from copytradeaster.storage.events import EVENT_KINDS, StoredEvent

_TRADE_FEED_KINDS = frozenset(
    {
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
        "PROTECTION_SET",
        "PROTECTION_CHANGED",
        "PROTECTION_UNCONFIRMED",
    }
)


def feed_kinds(activity_filter: ActivityFilter) -> list[str] | None:
    """Event kinds for one feed filter; None means the whole feed."""
    if activity_filter is ActivityFilter.TRADE:
        return sorted(_TRADE_FEED_KINDS)
    if activity_filter is ActivityFilter.SYSTEM:
        return sorted(EVENT_KINDS - _TRADE_FEED_KINDS)
    return None


def episode_side(direction: str) -> PositionSide | None:
    """Proven direction only; an unknown direction is never guessed."""
    if direction == "long":
        return PositionSide.LONG
    if direction == "short":
        return PositionSide.SHORT
    return None


def present_trade(episode: Episode, *, quantity_asset: str | None) -> TradeListItem:
    """One episode as a list card; money is canonical, net is absent."""
    closed = episode.closed_at is not None
    executed = episode.exit_qty if closed else episode.remaining_qty
    return TradeListItem(
        trade_ref=episode.public_ref,
        symbol=episode.symbol,
        quantity_asset=quantity_asset,
        side=episode_side(episode.direction),
        state=TradeState.CLOSED if closed else TradeState.OPEN,
        source=EpisodeSource(episode.source),
        opened_at=_as_datetime(episode.opened_at),
        closed_at=_as_datetime(episode.closed_at),
        executed_qty=_money(executed) if executed is not None else None,
        realized_pnl_gross_usdt=_money(episode.realized_pnl_gross),
        net_pnl_usdt=None,
        quality=HistoryQuality(episode.quality),
    )


def present_trade_detail(stored: StoredEpisode, *, quantity_asset: str | None) -> TradeDetailDTO:
    """Episode card plus its proven money breakdown and provenance."""
    episode = stored.episode
    duration = None
    if episode.opened_at is not None and episode.closed_at is not None:
        duration = episode.closed_at - episode.opened_at
    return TradeDetailDTO(
        trade=present_trade(episode, quantity_asset=quantity_asset),
        entry_qty=_money(episode.entry_qty),
        exit_qty=_money(episode.exit_qty),
        remaining_qty=_money(episode.remaining_qty) if episode.remaining_qty is not None else None,
        average_entry=_money(episode.average_entry) if episode.average_entry is not None else None,
        average_exit=_money(episode.average_exit) if episode.average_exit is not None else None,
        duration_ms=duration,
        revision=stored.revision,
        position_side=episode.position_side.value,
    )


def present_fill(fill: UserTradeDTO) -> FillFacts:
    """One exchange execution with string ids and canonical money."""
    return FillFacts(
        trade_id=str(fill.trade_id),
        order_id=str(fill.order_id),
        side=fill.side.value,
        quantity=_money(fill.qty),
        price=_money(fill.price),
        realized_pnl=_money(fill.realized_pnl),
        executed_at=_as_datetime(fill.time),
    )


def present_event(stored: StoredEvent) -> TradeEventItem:
    """One immutable user event with its structured facts."""
    event = stored.event
    return TradeEventItem(
        event_ref=event.event_ref,
        kind=event.kind,
        trade_ref=event.trade_ref,
        occurred_at=_as_datetime(event.occurred_at),
        facts=dict(event.facts),
    )


def build_overview(
    summaries: Sequence[PositionSummaryDTO],
    *,
    available_usdt: Decimal | None,
    as_of: datetime | None,
    freshness: SnapshotFreshness,
) -> AccountOverview:
    """Aggregate one consistent snapshot; never invent missing numbers."""
    unrealized = sum((item.unrealized_profit for item in summaries), Decimal(0))
    protected = sum(1 for item in summaries if item.protection_state.value == "protected")
    return AccountOverview(
        as_of=as_of,
        freshness=freshness,
        open_count=len(summaries),
        unrealized_pnl_usdt=_money(unrealized),
        available_usdt=_money(available_usdt) if available_usdt is not None else None,
        protected_count=protected,
        risk_coverage=RiskCoverage.COMPLETE
        if freshness is SnapshotFreshness.FRESH
        else RiskCoverage.PARTIAL,
    )


def quantity_assets(
    symbols: Sequence[str], assets: Mapping[str, str | None]
) -> dict[str, str | None]:
    """Per-symbol quantity units; unknown stays None, never guessed."""
    return {symbol: assets.get(symbol) for symbol in symbols}


def _as_datetime(moment_ms: int | None) -> datetime | None:
    if moment_ms is None:
        return None
    if type(moment_ms) is not int or moment_ms < 0:
        raise ValueError("history time must be a nonnegative integer")
    return datetime.fromtimestamp(moment_ms / 1000, tz=UTC)


def risk_budget_steps(
    available: Decimal,
    *,
    min_percent: Decimal,
    max_percent: Decimal,
    step: Decimal,
) -> tuple[RiskPreviewStep, ...]:
    """Server-computed budget for every allowed risk step, exact Decimal math.

    The ruler screen switches these ready rows instead of requesting
    per-pixel math from the backend.
    """
    for value in (available, min_percent, max_percent, step):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("risk preview inputs must be finite Decimals")
    if available < 0 or min_percent <= 0 or max_percent < min_percent or step <= 0:
        raise ValueError("risk preview range is invalid")
    steps: list[RiskPreviewStep] = []
    percent = min_percent
    while percent <= max_percent:
        steps.append(RiskPreviewStep(percent, available * percent))
        percent += step
    return tuple(steps)


def _money(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text
