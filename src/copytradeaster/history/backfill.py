"""One resumable income backfill pass over explicit 7-day windows.

``GET /fapi/v3/income`` returns at most seven days per request, so a trade whose
lifetime predates the live import window can never prove its commissions or
funding from the running worker alone. This pass walks older windows explicitly,
commits them through the same durable coverage table the worker uses, and stops
on the first window the exchange refuses — a gap stays a gap, never a silent
"complete".
"""

from __future__ import annotations

from dataclasses import dataclass

from copytradeaster.history.reader import HistoryReader, Window
from copytradeaster.storage.history import HistoryRepository, HistoryScope

MAX_STEP_MS = 7 * 86400000


@dataclass(frozen=True, slots=True)
class IncomeBackfillStep:
    """One committed window and what it carried."""

    start_ms: int
    end_ms: int
    rows: int
    requests: int
    complete: bool


@dataclass(frozen=True, slots=True)
class IncomeBackfillReport:
    """Ordered oldest-first steps; ``stopped_at_ms`` names an unfinished window."""

    steps: tuple[IncomeBackfillStep, ...] = ()
    stopped_at_ms: int | None = None
    reason: str | None = None

    @property
    def rows(self) -> int:
        return sum(step.rows for step in self.steps)

    @property
    def requests(self) -> int:
        return sum(step.requests for step in self.steps)

    @property
    def covered_from_ms(self) -> int | None:
        return self.steps[0].start_ms if self.steps else None

    @property
    def covered_to_ms(self) -> int | None:
        return self.steps[-1].end_ms if self.steps else None


async def backfill_income(
    reader: HistoryReader,
    repository: HistoryRepository,
    scope: HistoryScope,
    *,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    request_budget: int = 2,
    max_windows: int = 64,
) -> IncomeBackfillReport:
    """Commit ``[start_ms, end_ms]`` in exchange-sized windows, oldest first.

    ``now_ms`` is the anchor every window must be older than: the live worker
    owns the trailing seven days, and this pass must not race it. A window that
    ends inside the live region is clamped; a fully live request is refused
    before any exchange call.
    """
    if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms < start_ms:
        raise ValueError("income backfill range is invalid")
    if type(max_windows) is not int or max_windows < 1:
        raise ValueError("income backfill window budget must be positive")
    live_floor = now_ms - MAX_STEP_MS
    if start_ms >= live_floor:
        raise ValueError("income backfill range is already inside the live import window")
    end_ms = min(end_ms, live_floor - 1)

    steps: list[IncomeBackfillStep] = []
    cursor = start_ms
    pending: list[Window] = [Window(start_ms, min(start_ms + MAX_STEP_MS - 1, end_ms))]
    passes = 0
    while cursor <= end_ms and len(steps) < max_windows:
        window = pending.pop(0)
        passes += 1
        if passes > max_windows * 4:
            # A dense window may subdivide deeply; bound one run so the operator
            # gets output and can simply resume instead of spinning on a range.
            return IncomeBackfillReport(
                steps=tuple(steps),
                stopped_at_ms=window.start,
                reason="window_budget_exhausted",
            )
        try:
            result = await reader.income(scope.account_id, window, request_budget=request_budget)
        except Exception as exc:  # noqa: BLE001 - report the exchange refusal
            return IncomeBackfillReport(
                steps=tuple(steps),
                stopped_at_ms=window.start,
                reason=type(exc).__name__,
            )
        await repository.commit_income(scope, window, result, checked_at=now_ms)
        steps.append(
            IncomeBackfillStep(
                start_ms=window.start,
                end_ms=window.end,
                rows=len(result.rows),
                requests=result.requests,
                complete=not result.pending and not result.saturated,
            )
        )
        if result.saturated:
            # Known facts are committed, but this millisecond cannot be proven
            # complete: stop here and let the worker keep retrying the gap.
            return IncomeBackfillReport(
                steps=tuple(steps),
                stopped_at_ms=window.end + 1,
                reason="window_saturated",
            )
        if result.pending:
            # Subdivide inside this same run: the live worker never walks this
            # old range, so leaving the remainder pending would strand it.
            pending = list(result.pending)
            continue
        cursor = window.end + 1
        if not pending and cursor <= end_ms:
            pending = [Window(cursor, min(cursor + MAX_STEP_MS - 1, end_ms))]
    return IncomeBackfillReport(steps=tuple(steps))


__all__ = ["IncomeBackfillReport", "IncomeBackfillStep", "backfill_income"]
