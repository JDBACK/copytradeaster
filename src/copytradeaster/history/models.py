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


@dataclass(frozen=True, slots=True)
class ImportDelta:
    """What one fills import changed. A repeat import is not history-wide dirt.

    ``changed_from_ms`` is the earliest newly stored fact time, None when the
    import stored nothing new. ``coverage_changed`` reports newly completed
    coverage even with zero new facts; ``coverage_changed_from_ms`` locates
    the earliest newly verified start so replay can tell late verification
    of an old gap from normal growth past the frontier. ``facts_revision``
    is the durable scope/symbol revision after the import transaction.
    """

    inserted_count: int
    changed_from_ms: int | None
    coverage_changed: bool
    coverage_changed_from_ms: int | None
    verified_through_ms: int | None
    facts_revision: int
    requests: int = 0


@dataclass(frozen=True, slots=True)
class ProjectionBoundary:
    """Durable proven replay frontier for one scope/symbol.

    ``seeds`` carry the proven quantity (and stable ref, when nonzero) per
    leg exactly at ``cutoff_ms``; replay resumes from them without rereading
    episode beginnings. ``flat`` is never a default: mode ``settled`` means
    the ending snapshot proved the range closed, ``partial`` means an open
    position continues past the cutoff. ``coverage_revision`` is the facts
    revision this boundary was proven against; a later facts change at or
    before the cutoff invalidates it as an immutable seed.
    """

    account_id: str
    environment: str
    symbol: str
    cutoff_ms: int
    mode: Literal["settled", "partial"]
    seeds: tuple[tuple[PosSide, PositionSeed], ...]
    coverage_revision: int
    version: int = 1

    def seed_map(self) -> dict[PosSide, PositionSeed]:
        return dict(self.seeds)


@dataclass(frozen=True, slots=True)
class TradeResolutionDTO:
    """Owned, reviewable identity verdict for one published ref."""

    ref: str
    kind: Literal["canonical", "alias", "split", "merge"]
    targets: tuple[str, ...]
    reason: str
    revision: int


@dataclass(frozen=True, slots=True)
class ReplayDecision:
    """Pure replay plan: where to restart and whether the origin is needed."""

    start_ms: int
    from_origin: bool


#: Maximum age of a fact (``recorded_at - occurred_at``) that may still push
#: a live notification. Single source for the capture freshness gate and the
#: per-event eligibility rule; both import it from here.
LIVE_ELIGIBLE_MS = 300_000


@dataclass(frozen=True, slots=True)
class EpisodeTransition:
    """One semantic allocation applied to an episode, in projection order.

    Computed by the projector at allocation-apply time, while the state
    after the allocation is available. Only ``entry``/``exit`` allocations
    produce transitions; ``unknown``-role fills (unproven quantity) and
    zero-quantity allocations produce none. Money is Decimal, never float.

    ``closed_gross`` is the gross of exactly the closed share of the fill:
    the full ``fill.realized_pnl`` when the allocation closes exactly the
    fill quantity, otherwise pro-rata by ``qty`` over the fill quantity.
    Entry transitions always carry zero. ``remainder`` is the absolute
    episode quantity immediately after this transition.
    """

    ref: str
    symbol: str
    leg: PosSide
    trade_id: int
    order_id: int
    role: Literal["entry", "exit"]
    occurred_at: int
    qty: Decimal
    remainder: Decimal | None
    price: Decimal
    closed_gross: Decimal


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """Pure projector output: episodes with the transitions that built them.

    ``transitions`` are in stable ``(occurred_at, trade_id)`` order per
    symbol; the two transitions of a flip fill (exit then entry) share one
    ``(occurred_at, trade_id)`` and keep exit-before-entry order.
    """

    episodes: tuple[Episode, ...]
    transitions: tuple[EpisodeTransition, ...]
