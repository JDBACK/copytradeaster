from unittest.mock import AsyncMock

import pytest

from copytradeaster.aster.models import IncomeDTO
from copytradeaster.history.reader import Window, read_windows


def flow(time, id):
    return IncomeDTO(
        symbol="",
        incomeType="TRANSFER",
        income="-1",
        asset="USDT",
        time=time,
        tranId=str(id),
        tradeId="",
    )


async def test_inclusive_split_has_no_holes_or_duplicate_boundary():
    facts = [flow(0, 1), flow(1, 2), flow(2, 3), flow(3, 4)]

    async def fetch(window):
        return [row for row in facts if window.start <= row.time <= window.end][:3]

    result = await read_windows(fetch, Window(0, 3), limit=3)
    assert result.rows == tuple(facts)
    assert result.complete == (Window(0, 1), Window(2, 3))
    assert not result.pending and not result.saturated
    assert result.requests == 3


async def test_budget_preserves_unread_windows_for_restart():
    fetch = AsyncMock(return_value=[flow(0, 1), flow(0, 2)])
    result = await read_windows(fetch, Window(0, 9), limit=2, request_budget=1)
    assert result.pending == (Window(0, 4), Window(5, 9))
    assert result.complete == ()
    assert fetch.await_count == 1


async def test_saturated_millisecond_is_gap_not_success():
    facts = [flow(0, 1), flow(0, 2)]
    result = await read_windows(AsyncMock(return_value=facts), Window(0, 0), limit=2)
    assert result.saturated == (Window(0, 0),)
    assert result.complete == ()
    assert result.rows == tuple(facts)


async def test_empty_window_is_verified_coverage():
    result = await read_windows(AsyncMock(return_value=[]), Window(0, 1))
    assert result.complete == (Window(0, 1),)


async def test_exchange_ignoring_bounds_does_not_advance_coverage():
    with pytest.raises(ValueError, match="outside"):
        await read_windows(AsyncMock(return_value=[flow(2, 1)]), Window(0, 1))


async def test_failure_propagates_to_transaction_owner():
    with pytest.raises(TimeoutError):
        await read_windows(AsyncMock(side_effect=TimeoutError), Window(0, 1))
