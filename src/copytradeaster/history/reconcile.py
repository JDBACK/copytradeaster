"""Pure coverage/quantity proof before publishing complete episode history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import Episode
from copytradeaster.history.reader import Window


class HistoryReconciliationError(ValueError):
    """Current evidence cannot certify the projected history."""


def require_coverage(window: Window, complete: Sequence[tuple[int, int]]) -> None:
    next_time = window.start
    for start, end in sorted(complete):
        if end < next_time:
            continue
        if start > next_time:
            break
        next_time = end + 1
        if next_time > window.end:
            return
    raise HistoryReconciliationError("fill coverage has a gap")


def require_ending_quantities(
    episodes: Sequence[Episode],
    ending: Mapping[PosSide, Decimal],
) -> None:
    quantities = {leg: Decimal(0) for leg in ending}
    for leg, qty in ending.items():
        if not isinstance(qty, Decimal) or not qty.is_finite():
            raise HistoryReconciliationError("ending quantity must be exact")
        if leg != PosSide.BOTH and qty < 0:
            raise HistoryReconciliationError("negative hedge quantity")
    active_legs = set()
    for episode in episodes:
        if episode.position_side not in ending:
            raise HistoryReconciliationError("ending snapshot missing leg")
        if episode.closed_at is not None:
            if episode.remaining_qty != 0:
                raise HistoryReconciliationError("closed episode has nonzero remainder")
            continue
        if episode.remaining_qty is None:
            raise HistoryReconciliationError("projected remainder is unknown")
        if episode.position_side in active_legs:
            raise HistoryReconciliationError("multiple open episodes on one leg")
        active_legs.add(episode.position_side)
        signed = episode.remaining_qty
        if episode.position_side == PosSide.BOTH and episode.direction == "short":
            signed = -signed
        quantities[episode.position_side] = signed
    if quantities != dict(ending):
        raise HistoryReconciliationError("projected quantity differs from authoritative snapshot")
