from dataclasses import replace
from decimal import Decimal as D

import pytest

from copytradeaster.aster.models import PositionDTO
from copytradeaster.domain.models import PosSide
from copytradeaster.history.reader import Window
from copytradeaster.history.reconcile import HistoryReconciliationError
from copytradeaster.history.snapshot import PositionBoundary, infer_starting_seeds
from tests.unit.history.test_projector import fill


def position(**updates):
    return PositionDTO.model_validate(
        {
            "symbol": "BTCUSDT",
            "positionAmt": "1",
            "entryPrice": "100",
            "markPrice": "110",
            "leverage": 10,
            "liquidationPrice": "0",
            "marginType": "cross",
            "positionSide": "BOTH",
            "unRealizedProfit": "10",
            "updateTime": 10,
            **updates,
        }
    )


def boundary(row=None):
    return PositionBoundary.from_positions(
        "BTCUSDT", [row or position()], request_started=99, response_received=100
    )


def test_unchanged_position_brackets_history_cutoff():
    before = boundary()
    before.prove_unchanged(replace(before, request_started=2200, response_received=2201), 100)


def test_round_trip_same_quantity_but_new_update_time_rejected():
    before = boundary()
    after = replace(
        boundary(position(updateTime=120)), request_started=2200, response_received=2201
    )
    with pytest.raises(HistoryReconciliationError, match="changed"):
        before.prove_unchanged(after, 100)


def test_missing_timestamp_cannot_certify_snapshot():
    with pytest.raises(HistoryReconciliationError):
        boundary(position(updateTime=None))


def test_future_cutoff_not_yet_elapsed_rejected():
    before = boundary()
    with pytest.raises(HistoryReconciliationError, match="bracketed"):
        before.prove_unchanged(replace(before, request_started=200, response_received=201), 100)


def test_seed_inferred_only_as_quantity_preserves_unknown_entry_for_nonzero_start():
    before = boundary()
    seeds = infer_starting_seeds(
        before,
        Window(0, 100),
        [fill(1, "SELL", "1", pnl="10")],
        placeholder_ref=lambda leg: "existing",
    )
    assert seeds[PosSide.BOTH].quantity == D(2)
    assert seeds[PosSide.BOTH].public_ref == "existing"


def test_duplicate_fill_does_not_change_inferred_start():
    before = boundary()
    trade = fill(1, "BUY", "1")
    seeds = infer_starting_seeds(
        before, Window(0, 100), [trade, trade], placeholder_ref=lambda leg: "x"
    )
    assert seeds[PosSide.BOTH].quantity == 0
