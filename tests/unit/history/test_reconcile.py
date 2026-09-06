from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.reader import Window
from copytradeaster.history.reconcile import (
    HistoryReconciliationError,
    require_coverage,
    require_ending_quantities,
)
from tests.unit.history.test_projector import fill, project


def test_contiguous_coverage_inclusive_boundary():
    require_coverage(Window(5, 20), [(0, 10), (11, 30)])
    with pytest.raises(HistoryReconciliationError):
        require_coverage(Window(5, 20), [(0, 10), (12, 30)])


def test_one_way_short_matches_signed_snapshot():
    episodes = project([fill(1, "SELL", "2")])
    require_ending_quantities(episodes, {PosSide.BOTH: D(-2)})
    with pytest.raises(HistoryReconciliationError):
        require_ending_quantities(episodes, {PosSide.BOTH: D(2)})


def test_unknown_remainder_never_certified_by_current_flat_snapshot():
    episodes = project([], {PosSide.BOTH: PositionSeed(None, "partial")})
    with pytest.raises(HistoryReconciliationError):
        require_ending_quantities(episodes, {PosSide.BOTH: D(0)})


def test_two_hedge_legs_verified_independently():
    episodes = project(
        [fill(1, "BUY", "2", leg="LONG"), fill(2, "SELL", "3", leg="SHORT")],
        {PosSide.LONG: PositionSeed(D(0)), PosSide.SHORT: PositionSeed(D(0))},
    )
    require_ending_quantities(episodes, {PosSide.LONG: D(2), PosSide.SHORT: D(3)})
    with pytest.raises(HistoryReconciliationError):
        require_ending_quantities(episodes, {PosSide.LONG: D(2)})
