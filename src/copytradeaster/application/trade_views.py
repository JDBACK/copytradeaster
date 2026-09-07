"""Shared safe presenters over proven history facts (S1.4, plan §10.8 B7).

Orchestration and ownership stay in the application service; every
transformation of episodes, fills and user events into UI DTOs lives here
so the Mini App, the bot and notifications share one meaning. No I/O,
no exchange calls, no guessing: unknown directions stay null, net stays
absent until full attribution, and money travels as canonical strings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from copytradeaster.application.models import (
    AccountOverview,
    ActivityFilter,
    EpisodeMoneyView,
    EpisodeSource,
    FillFacts,
    HistoryQuality,
    MoneyComponent,
    MoneyState,
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
from copytradeaster.history.accounting import EpisodeMoney
from copytradeaster.history.models import Episode, TradeResolutionDTO
from copytradeaster.notifications.trade_cards import format_user_event
from copytradeaster.storage.episodes import AllocatedFill, EpisodeRepository, StoredEpisode
from copytradeaster.storage.events import EVENT_KINDS, StoredEvent
from copytradeaster.storage.history import HistoryScope

_COMMISSION_ISSUES = frozenset(
    {
        "partial_history",
        "income_incomplete",
        "fill_missing",
        "shared_fill_commission",
        "commission_missing",
        "commission_mismatch",
        "non_usdt_cost",
    }
)
_FUNDING_ISSUES = frozenset(
    {
        "partial_history",
        "income_incomplete",
        "funding_unattributed",
        "non_usdt_cost",
    }
)
_BUILDER_ISSUES = frozenset(
    {
        "partial_history",
        "builder_unknown",
        "builder_unattributed",
        "builder_policy_conflict",
        "non_usdt_cost",
    }
)

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


def present_trade(
    episode: Episode,
    *,
    quantity_asset: str | None,
    money: EpisodeMoney | None = None,
    revision: int = 0,
    as_of: datetime | None = None,
) -> TradeListItem:
    """One episode as a list card; money is canonical, net needs proof."""
    closed = episode.closed_at is not None
    executed = episode.exit_qty if closed else episode.remaining_qty
    view = _money_view(money, revision=revision, as_of=as_of) if money is not None else None
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
        net_pnl_usdt=view.net if view is not None else None,
        quality=HistoryQuality(episode.quality),
        result_state=view.result_state if view is not None else "gross_only",
        money=view,
        average_entry=_money(episode.average_entry) if episode.average_entry is not None else None,
        average_exit=_money(episode.average_exit) if episode.average_exit is not None else None,
    )


def present_trade_detail(
    stored: StoredEpisode,
    *,
    quantity_asset: str | None,
    money: EpisodeMoney | None = None,
    as_of: datetime | None = None,
) -> TradeDetailDTO:
    """Episode card plus its proven money breakdown and provenance."""
    episode = stored.episode
    duration = None
    if episode.opened_at is not None and episode.closed_at is not None:
        duration = episode.closed_at - episode.opened_at
    view = _money_view(money, revision=stored.revision, as_of=as_of) if money is not None else None
    return TradeDetailDTO(
        trade=present_trade(
            episode,
            quantity_asset=quantity_asset,
            money=money,
            revision=stored.revision,
            as_of=as_of,
        ),
        entry_qty=_money(episode.entry_qty),
        exit_qty=_money(episode.exit_qty),
        remaining_qty=_money(episode.remaining_qty) if episode.remaining_qty is not None else None,
        average_entry=_money(episode.average_entry) if episode.average_entry is not None else None,
        average_exit=_money(episode.average_exit) if episode.average_exit is not None else None,
        duration_ms=duration,
        revision=stored.revision,
        position_side=episode.position_side.value,
        result_state=view.result_state if view is not None else "gross_only",
        money=view,
    )


def _money_component(
    known: Decimal, relevant: frozenset[str], flat: frozenset[str]
) -> MoneyComponent:
    """One component's proven amount plus its own complete/partial/unavailable state."""
    own = tuple(sorted(flat & relevant))
    if not own:
        state = MoneyState.COMPLETE
    elif known != 0:
        state = MoneyState.PARTIAL
    else:
        state = MoneyState.UNAVAILABLE
    return MoneyComponent(known_amount=_money(known), state=state, issues=own)


def _money_view(money: EpisodeMoney, *, revision: int, as_of: datetime | None) -> EpisodeMoneyView:
    """Proof-driven view: net only with sufficient proof, else null plus gaps."""
    flat = frozenset(money.issues)
    result_state = "net_complete" if money.net is not None else "gross_only"
    return EpisodeMoneyView(
        gross=_money(money.gross),
        net=_money(money.net) if money.net is not None else None,
        commission=_money_component(money.commission, _COMMISSION_ISSUES, flat),
        funding=_money_component(money.funding, _FUNDING_ISSUES, flat),
        builder=_money_component(money.builder, _BUILDER_ISSUES, flat),
        result_state=result_state,
        issues=money.issues,
        as_of=as_of,
        revision=revision,
    )


def present_allocated_fill(row: AllocatedFill) -> FillFacts:
    """One episode share of one exchange execution (C2, plan §23.3).

    Exchange qty/price travel untouched next to the episode share; money is
    canonical strings, ids stay strings.
    """
    return FillFacts(
        trade_id=str(row.exchange_trade_id),
        order_id=str(row.order_id),
        side=row.side,
        quantity=_money(row.quantity),
        price=_money(row.price),
        realized_pnl=_money(row.exchange_realized_pnl),
        executed_at=_as_datetime(row.occurred_at),
        allocated_quantity=_money(row.allocated_qty),
        role=row.role,
        allocated_realized_pnl=_money(row.allocated_realized_gross),
        position_side=row.position_side,
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
        text=format_user_event(event, quantity_asset=event.facts.get("quantity_asset")),
    )


@dataclass(frozen=True, slots=True)
class OwnedTradeRef:
    """One owned ref resolved for a details/fills/events request.

    ``canonical_ref`` is the surviving episode; ``family_refs`` adds every
    proven alias/merge ref so event selection covers the whole proven
    family. ``resolution`` carries the recorded verdict when one exists.
    """

    canonical_ref: str
    family_refs: tuple[str, ...]
    stored: StoredEpisode
    resolution: TradeResolutionDTO | None


async def resolve_owned_ref(
    episodes: EpisodeRepository, scope: HistoryScope, ref: str
) -> OwnedTradeRef | TradeResolutionDTO | None:
    """Resolve once per request: direct → canonical, merge/alias → canonical.

    Lives here (not in the service) so every transport shares one meaning;
    the service maps the outcome to the response and never re-resolves.
    Returns the split :class:`TradeResolutionDTO` itself so the caller maps
    it to an explicit response instead of silently picking a child; None
    covers missing and foreign refs alike so existence never leaks.
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError("trade reference is invalid")
    resolution = await episodes.get_resolution(scope, ref)
    if resolution is not None and resolution.kind == "split":
        return resolution
    stored = await episodes.get(scope, ref)
    if stored is None:
        return None
    canonical = stored.episode.public_ref
    family = await episodes.alias_family(scope, canonical)
    return OwnedTradeRef(
        canonical_ref=canonical,
        family_refs=family,
        stored=stored,
        resolution=resolution,
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
