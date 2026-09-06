"""Follower episode values. Exchange facts remain separate from projections."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from copytradeaster.domain.models import PosSide

TradeSource = Literal["copy", "external", "mixed", "unknown"]


@dataclass(frozen=True, slots=True)
class PositionSeed:
    """Authoritative quantity at the import boundary, or None if not known.

    Quantity is signed for BOTH, nonnegative for LONG/SHORT. A nonzero seed
    needs a stable placeholder ref so backfill does not replace its public link.
    """

    quantity: Decimal | None
    public_ref: str | None = None

    def __post_init__(self) -> None:
        if self.quantity is not None and (
            not isinstance(self.quantity, Decimal) or not self.quantity.is_finite()
        ):
            raise ValueError("seed quantity must be a finite Decimal or unknown")
        if self.quantity != Decimal(0) and not self.public_ref:
            raise ValueError("partial history requires a stable placeholder ref")


@dataclass(frozen=True, slots=True)
class FillAllocation:
    trade_id: int
    order_id: int
    quantity: Decimal
    role: Literal["entry", "exit", "unknown"]
    occurred_at: int


@dataclass(frozen=True, slots=True)
class Episode:
    public_ref: str
    symbol: str
    position_side: PosSide
    direction: Literal["long", "short", "unknown"]
    opened_at: int | None
    closed_at: int | None
    remaining_qty: Decimal | None
    entry_qty: Decimal
    entry_quote: Decimal
    exit_qty: Decimal
    exit_quote: Decimal
    realized_pnl_gross: Decimal
    source: TradeSource
    quality: Literal["complete", "partial_history"]
    allocations: tuple[FillAllocation, ...]

    @property
    def average_entry(self) -> Decimal | None:
        if self.quality != "complete" or not self.entry_qty:
            return None
        return self.entry_quote / self.entry_qty

    @property
    def average_exit(self) -> Decimal | None:
        return self.exit_quote / self.exit_qty if self.exit_qty else None
