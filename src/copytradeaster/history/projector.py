"""Pure follower quantity projection. No trading actions, I/O or guessed seed.

Caller proves coverage and supplies quantity at the boundary. 'complete' here
means a known episode beginning, not complete financial accounting or freshness.
Published reference reconciliation for late backfill belongs to persistence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, localcontext

from copytradeaster.aster.models import UserTradeDTO
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.history.models import (
    Episode,
    EpisodeTransition,
    FillAllocation,
    PositionSeed,
    ProjectionResult,
    TradeSource,
)

ZERO = Decimal(0)


def project_with_transitions(
    fills: Sequence[UserTradeDTO],
    *,
    symbol: str,
    seeds: Mapping[PosSide, PositionSeed],
    reference: Callable[[UserTradeDTO, str], str],
    order_sources: Mapping[int, TradeSource] | None = None,
) -> ProjectionResult:
    """Project episodes and the semantic transitions that built them.

    Reference factory receives the first fill and direction, including flip
    remainders. Every nonzero ``entry``/``exit`` allocation appends one
    transition carrying the state right after it was applied, so events
    derived later describe the transition itself instead of re-reading
    episode totals (which diverge between one batch and sequential
    publications of the same fills).
    """
    sources = order_sources or {}
    ordered = sorted(fills, key=lambda fill: (fill.time, fill.trade_id))
    seen: dict[int, UserTradeDTO] = {}
    active: dict[PosSide, Episode] = {}
    quantities = {leg: seed.quantity for leg, seed in seeds.items()}
    completed: list[Episode] = []
    transitions: list[EpisodeTransition] = []
    if PosSide.BOTH in seeds and len(seeds) > 1:
        raise ValueError("mixed position modes require an explicit boundary")
    for leg, seed in seeds.items():
        if leg != PosSide.BOTH and seed.quantity is not None and seed.quantity < 0:
            raise ValueError("hedge seed quantity must be nonnegative")
        if seed.quantity != ZERO:
            direction = (
                "unknown"
                if seed.quantity is None
                else "short"
                if leg == PosSide.SHORT or seed.quantity < 0
                else "long"
            )
            active[leg] = _empty(
                seed.public_ref or "",
                symbol,
                leg,
                direction,
                None,
                abs(seed.quantity) if seed.quantity is not None else None,
                "partial_history",
            )
    with localcontext() as context:
        context.prec = 80
        for fill in ordered:
            if fill.symbol != symbol or fill.position_side not in seeds:
                raise ValueError("fill scope has no authoritative seed")
            if fill.trade_id in seen:
                if seen[fill.trade_id] != fill:
                    raise ValueError("conflicting fill identity")
                continue
            seen[fill.trade_id] = fill
            leg = fill.position_side
            quantity = quantities[leg]
            source = sources.get(fill.order_id, "unknown")
            if quantity is None:
                episode = active[leg]
                active[leg] = replace(
                    episode,
                    realized_pnl_gross=episode.realized_pnl_gross + fill.realized_pnl,
                    source=_source(episode, source),
                    allocations=episode.allocations + (_allocation(fill, fill.qty, "unknown"),),
                )
                continue
            delta = fill.qty if fill.side == Side.BUY else -fill.qty
            if leg == PosSide.SHORT:
                delta = -delta
            if leg != PosSide.BOTH and quantity + delta < ZERO:
                raise ValueError("hedge reduction exceeds known quantity; history has a gap")
            closing = quantity != ZERO and (quantity > ZERO) != (delta > ZERO)
            close_qty = min(abs(quantity), fill.qty) if closing else ZERO
            entry_qty = fill.qty - close_qty
            if not closing and fill.realized_pnl != ZERO:
                raise ValueError("realized PnL on entry contradicts known starting position")
            if closing:
                episode = active[leg]
                remaining = abs(quantity) - close_qty
                if close_qty != ZERO:
                    transitions.append(
                        EpisodeTransition(
                            ref=episode.public_ref,
                            symbol=symbol,
                            leg=leg,
                            trade_id=fill.trade_id,
                            order_id=fill.order_id,
                            role="exit",
                            occurred_at=fill.time,
                            qty=close_qty,
                            remainder=remaining,
                            price=fill.price,
                            closed_gross=_closed_gross(fill, close_qty),
                        )
                    )
                episode = replace(
                    episode,
                    remaining_qty=remaining,
                    exit_qty=episode.exit_qty + close_qty,
                    exit_quote=episode.exit_quote + close_qty * fill.price,
                    realized_pnl_gross=episode.realized_pnl_gross + fill.realized_pnl,
                    source=_source(episode, source),
                    closed_at=fill.time if remaining == ZERO else None,
                    allocations=episode.allocations + (_allocation(fill, close_qty, "exit"),),
                )
                if remaining == ZERO:
                    completed.append(episode)
                    del active[leg]
                else:
                    active[leg] = episode
            if entry_qty:
                if leg not in active:
                    direction = "short" if leg == PosSide.SHORT or delta < ZERO else "long"
                    active[leg] = _empty(
                        reference(fill, direction),
                        symbol,
                        leg,
                        direction,
                        fill.time,
                        ZERO,
                        "complete",
                    )
                episode = active[leg]
                remainder_after = (episode.remaining_qty or ZERO) + entry_qty
                transitions.append(
                    EpisodeTransition(
                        ref=episode.public_ref,
                        symbol=symbol,
                        leg=leg,
                        trade_id=fill.trade_id,
                        order_id=fill.order_id,
                        role="entry",
                        occurred_at=fill.time,
                        qty=entry_qty,
                        remainder=remainder_after,
                        price=fill.price,
                        closed_gross=ZERO,
                    )
                )
                active[leg] = replace(
                    episode,
                    remaining_qty=(episode.remaining_qty or ZERO) + entry_qty,
                    entry_qty=episode.entry_qty + entry_qty,
                    entry_quote=episode.entry_quote + entry_qty * fill.price,
                    source=_source(episode, source),
                    allocations=episode.allocations + (_allocation(fill, entry_qty, "entry"),),
                )
            quantities[leg] = quantity + delta
    episodes = tuple(
        sorted(
            completed + list(active.values()),
            key=lambda e: (e.opened_at if e.opened_at is not None else -1, e.public_ref),
        )
    )
    ordered_transitions = tuple(
        sorted(transitions, key=lambda item: (item.occurred_at, item.trade_id))
    )
    return ProjectionResult(episodes=episodes, transitions=ordered_transitions)


def project_episodes(
    fills: Sequence[UserTradeDTO],
    *,
    symbol: str,
    seeds: Mapping[PosSide, PositionSeed],
    reference: Callable[[UserTradeDTO, str], str],
    order_sources: Mapping[int, TradeSource] | None = None,
) -> tuple[Episode, ...]:
    """Project episodes only; transitions are available via project_with_transitions."""
    return project_with_transitions(
        fills, symbol=symbol, seeds=seeds, reference=reference, order_sources=order_sources
    ).episodes


def _closed_gross(fill: UserTradeDTO, close_qty: Decimal) -> Decimal:
    """Gross of exactly the closed share of one exit allocation.

    A fill that closes exactly its own quantity carries its full realized
    PnL; a flip fill that also opens the next episode attributes only the
    closed share, pro-rata by ``close_qty`` over the fill quantity. This is
    the fill-level share of this transition, distinct from the episode-level
    pro-rata split of the episode total kept in the allocation index.
    """
    if close_qty == fill.qty:
        return fill.realized_pnl
    return fill.realized_pnl * close_qty / fill.qty


def _empty(ref, symbol, leg, direction, opened_at, quantity, quality) -> Episode:
    return Episode(
        ref,
        symbol,
        leg,
        direction,
        opened_at,
        None,
        quantity,
        ZERO,
        ZERO,
        ZERO,
        ZERO,
        ZERO,
        "unknown",
        quality,
        (),
    )


def _allocation(fill: UserTradeDTO, qty: Decimal, role) -> FillAllocation:
    return FillAllocation(fill.trade_id, fill.order_id, qty, role, fill.time)


def _source(episode: Episode, new: TradeSource) -> TradeSource:
    if not episode.allocations:
        return new if episode.quality == "complete" else "unknown"
    if "unknown" in (episode.source, new):
        return "unknown"
    return new if episode.source == new else "mixed"
