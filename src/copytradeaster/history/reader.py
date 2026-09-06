"""Bounded inclusive history windows with explicit incomplete coverage.

Splitting by time works for both streams, including income without a documented
ID/page cursor. A saturated single millisecond cannot prove completeness.
The caller persists returned facts and retries unresolved windows; it must not
advance a coverage checkpoint past a gap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from copytradeaster.aster.models import IncomeDTO, UserTradeDTO


class HistoryReadClient(Protocol):
    async def user_trades(
        self,
        account_id: str,
        symbol: str,
        *,
        start_time: int,
        end_time: int,
        limit: int,
    ) -> list[UserTradeDTO]: ...

    async def income_history(
        self,
        account_id: str,
        *,
        start_time: int,
        end_time: int,
        limit: int,
    ) -> list[IncomeDTO]: ...


@dataclass(frozen=True, slots=True)
class Window:
    start: int
    end: int

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int:
            raise ValueError("window timestamps must be integers")
        if self.start < 0 or not 0 <= self.end - self.start <= 7 * 86400000:
            raise ValueError("window must be ordered and at most seven days")


@dataclass(frozen=True, slots=True)
class WindowRead[T]:
    rows: tuple[T, ...]
    complete: tuple[Window, ...]
    pending: tuple[Window, ...]
    saturated: tuple[Window, ...]
    requests: int


async def read_windows[T: (UserTradeDTO, IncomeDTO)](
    fetch: Callable[[Window], Awaitable[list[T]]],
    window: Window,
    *,
    limit: int = 1000,
    request_budget: int = 8,
) -> WindowRead[T]:
    """At most request_budget calls; cancellation/errors propagate without checkpoint."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid page limit")
    if type(request_budget) is not int or not 1 <= request_budget <= 100:
        raise ValueError("invalid request budget")
    pending = [window]
    complete: list[Window] = []
    saturated: list[Window] = []
    rows: list[T] = []
    requests = 0
    while pending and requests < request_budget:
        current = pending.pop()
        page = await fetch(current)
        requests += 1
        if len(page) > limit or any(not current.start <= row.time <= current.end for row in page):
            raise ValueError("history page outside requested bounds")
        if len(page) < limit:
            rows.extend(page)
            complete.append(current)
        elif current.start == current.end:
            # Keep known facts, but never claim this millisecond is complete.
            rows.extend(page)
            saturated.append(current)
        else:
            mid = (current.start + current.end) // 2
            pending.extend((Window(mid + 1, current.end), Window(current.start, mid)))
    return WindowRead(
        tuple(rows),
        tuple(complete),
        tuple(reversed(pending)),
        tuple(saturated),
        requests,
    )


class HistoryReader:
    def __init__(self, client: HistoryReadClient) -> None:
        self._client = client

    async def fills(
        self,
        account_id: str,
        symbol: str,
        window: Window,
        *,
        request_budget: int = 8,
    ) -> WindowRead[UserTradeDTO]:
        async def fetch(part: Window) -> list[UserTradeDTO]:
            return await self._client.user_trades(
                account_id,
                symbol,
                start_time=part.start,
                end_time=part.end,
                limit=1000,
            )

        return await read_windows(fetch, window, request_budget=request_budget)

    async def income(
        self,
        account_id: str,
        window: Window,
        *,
        request_budget: int = 8,
    ) -> WindowRead[IncomeDTO]:
        async def fetch(part: Window) -> list[IncomeDTO]:
            return await self._client.income_history(
                account_id,
                start_time=part.start,
                end_time=part.end,
                limit=1000,
            )

        return await read_windows(fetch, window, request_budget=request_budget)
