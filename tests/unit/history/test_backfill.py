"""Income backfill: older windows are committed, the live region is never touched.

``GET /fapi/v3/income`` answers at most seven days per request, so commissions
and funding of an older trade are only recoverable by explicitly walking older
windows. The pass must chunk exactly, stop at the live boundary the running
worker owns, and keep an incomplete window as a gap instead of claiming it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from copytradeaster.aster.models import IncomeDTO
from copytradeaster.history.backfill import backfill_income
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.history import HistoryScope

DAY = 86_400_000
NOW = 1_700_000_000_000
LIVE_FLOOR = NOW - 7 * DAY


class FakeReader:
    def __init__(self, *, rows_per_window: int = 1, fail_at: int | None = None) -> None:
        self.windows: list[Window] = []
        self.rows_per_window = rows_per_window
        self.fail_at = fail_at

    async def income(
        self, account_id: str, window: Window, *, request_budget: int = 8
    ) -> WindowRead[IncomeDTO]:
        self.windows.append(window)
        if self.fail_at is not None and window.start == self.fail_at:
            raise RuntimeError("exchange refused")
        rows = tuple(
            IncomeDTO(
                symbol="BTCUSDT",
                incomeType="COMMISSION",
                income=Decimal("-0.01"),
                asset="USDT",
                time=window.start + index,
                tranId=str(window.start + index),
                tradeId=str(window.start),
            )
            for index in range(self.rows_per_window)
        )
        return WindowRead(rows, (window,), (), (), 1)


class FakeRepository:
    def __init__(self) -> None:
        self.commits: list[tuple[Window, int]] = []

    async def commit_income(self, scope, window, result, *, checked_at: int) -> None:
        self.commits.append((window, len(result.rows)))


def _scope() -> HistoryScope:
    return HistoryScope("f1", "testnet")


async def test_windows_chunk_backwards_and_stop_at_the_live_boundary() -> None:
    reader, repo = FakeReader(), FakeRepository()
    report = await backfill_income(
        reader, repo, _scope(), start_ms=NOW - 21 * DAY, end_ms=NOW, now_ms=NOW
    )
    assert [window.end for window, _ in repo.commits] == [NOW - 14 * DAY - 1, LIVE_FLOOR - 1]
    assert all(window.end <= LIVE_FLOOR - 1 for window in reader.windows)
    assert report.stopped_at_ms is None
    assert report.rows == 2 and report.requests == 2
    assert report.covered_from_ms == NOW - 21 * DAY
    assert report.covered_to_ms == LIVE_FLOOR - 1


async def test_a_range_inside_the_live_window_is_refused() -> None:
    with pytest.raises(ValueError):
        await backfill_income(
            FakeReader(),
            FakeRepository(),
            _scope(),
            start_ms=NOW - 3 * DAY,
            end_ms=NOW,
            now_ms=NOW,
        )


async def test_an_exchange_refusal_reports_where_it_stopped() -> None:
    reader = FakeReader(fail_at=NOW - 14 * DAY)
    repo = FakeRepository()
    report = await backfill_income(
        reader, repo, _scope(), start_ms=NOW - 21 * DAY, end_ms=NOW, now_ms=NOW
    )
    assert len(repo.commits) == 1
    assert report.stopped_at_ms == NOW - 14 * DAY
    assert report.reason == "RuntimeError"
    assert report.covered_to_ms == NOW - 14 * DAY - 1


async def test_an_incomplete_window_is_retried_instead_of_stranded() -> None:
    """A pending remainder is chased inside the same run.

    The live worker only walks the trailing seven days, so leaving an old
    pending window behind would strand it forever: the pass must keep asking for
    the unread remainder until it is read or the exchange refuses.
    """

    class PartialReader(FakeReader):
        async def income(self, account_id, window, *, request_budget=8):
            self.windows.append(window)
            half = Window(window.start, window.start + DAY)
            return WindowRead((), (half,), (Window(half.end + 1, window.end),), (), 2)

    reader = PartialReader()
    repo = FakeRepository()
    report = await backfill_income(
        reader, repo, _scope(), start_ms=NOW - 21 * DAY, end_ms=NOW, now_ms=NOW
    )
    assert len(repo.commits) > 1  # the remainder was requested again
    starts = [window.start for window, _ in repo.commits]
    assert starts == sorted(starts)  # oldest first, no backwards jump
    # The chain converges on a range the exchange request shape cannot accept:
    # the pass reports that concrete gap instead of calling it covered.
    assert report.reason == "ValueError"
    assert report.stopped_at_ms is not None
    assert report.steps[-1].complete is False


async def test_the_window_budget_bounds_one_pass() -> None:
    reader, repo = FakeReader(), FakeRepository()
    report = await backfill_income(
        reader,
        repo,
        _scope(),
        start_ms=NOW - 60 * DAY,
        end_ms=NOW,
        now_ms=NOW,
        max_windows=3,
    )
    assert len(repo.commits) == 3
    assert report.stopped_at_ms is None
