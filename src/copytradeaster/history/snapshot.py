"""Position boundary evidence for a serial history capture/import/recheck cycle."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from copytradeaster.aster.models import PositionDTO, UserTradeDTO
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.reader import Window
from copytradeaster.history.reconcile import HistoryReconciliationError


@dataclass(frozen=True, slots=True)
class SnapshotLeg:
    side: PosSide
    quantity: Decimal  # signed BOTH, positive magnitude for HEDGE
    entry_price: Decimal
    updated_at: int


@dataclass(frozen=True, slots=True)
class PositionBoundary:
    symbol: str
    request_started: int
    response_received: int
    legs: tuple[SnapshotLeg, ...]

    @classmethod
    def from_positions(
        cls,
        symbol: str,
        rows: Sequence[PositionDTO],
        *,
        request_started: int,
        response_received: int,
    ) -> PositionBoundary:
        if not 0 <= request_started <= response_received:
            raise HistoryReconciliationError("invalid snapshot clock interval")
        legs = []
        for row in rows:
            if row.symbol != symbol or row.update_time is None:
                raise HistoryReconciliationError("snapshot scope/time unavailable")
            if not row.position_amt.is_finite() or not row.entry_price.is_finite():
                raise HistoryReconciliationError("invalid snapshot quantity/price")
            if row.position_side == PosSide.LONG and row.position_amt < 0:
                raise HistoryReconciliationError("invalid LONG sign")
            if row.position_side == PosSide.SHORT and row.position_amt > 0:
                raise HistoryReconciliationError("invalid SHORT sign")
            quantity = (
                row.position_amt if row.position_side == PosSide.BOTH else abs(row.position_amt)
            )
            legs.append(SnapshotLeg(row.position_side, quantity, row.entry_price, row.update_time))
        sides = {leg.side for leg in legs}
        if len(sides) != len(legs) or sides not in ({PosSide.BOTH}, {PosSide.LONG, PosSide.SHORT}):
            raise HistoryReconciliationError("snapshot does not contain one complete position mode")
        return cls(
            symbol,
            request_started,
            response_received,
            tuple(sorted(legs, key=lambda leg: leg.side)),
        )

    def prove_unchanged(
        self, after: PositionBoundary, cutoff: int, *, clock_margin_ms: int = 2000
    ) -> None:
        if clock_margin_ms < 0:
            raise ValueError("negative clock margin")
        if self.symbol != after.symbol or self.legs != after.legs:
            raise HistoryReconciliationError("position changed during history capture")
        if cutoff < self.response_received or after.request_started < cutoff + clock_margin_ms:
            raise HistoryReconciliationError("history cutoff is not bracketed by snapshots")
        if any(leg.updated_at > cutoff for leg in self.legs):
            raise HistoryReconciliationError("snapshot contains changes after history cutoff")

    def quantities(self) -> dict[PosSide, Decimal]:
        return {leg.side: leg.quantity for leg in self.legs}


def infer_starting_seeds(
    boundary: PositionBoundary,
    window: Window,
    fills: Sequence[UserTradeDTO],
    *,
    placeholder_ref: Callable[[PosSide], str],
) -> dict[PosSide, PositionSeed]:
    """Only after unchanged-boundary and complete-window proofs by the caller.

    A nonzero inferred starting position has an unknown entry and remains partial.
    This does not establish cost coverage or historical ownership of any order.
    """
    quantities = boundary.quantities()
    seen = {}
    with localcontext() as context:
        context.prec = 80
        for fill in fills:
            if fill.symbol != boundary.symbol or fill.position_side not in quantities:
                raise HistoryReconciliationError("fill outside snapshot scope")
            if not window.start <= fill.time <= window.end:
                raise HistoryReconciliationError("fill outside import boundary")
            if fill.trade_id in seen:
                if seen[fill.trade_id] != fill:
                    raise HistoryReconciliationError("conflicting fill identity")
                continue
            seen[fill.trade_id] = fill
            delta = fill.qty if fill.side == Side.BUY else -fill.qty
            if fill.position_side == PosSide.SHORT:
                delta = -delta
            quantities[fill.position_side] -= delta
    if any(leg != PosSide.BOTH and qty < 0 for leg, qty in quantities.items()):
        raise HistoryReconciliationError("history cannot explain hedge snapshot")
    return {
        leg: PositionSeed(qty, placeholder_ref(leg) if qty != 0 else None)
        for leg, qty in quantities.items()
    }
