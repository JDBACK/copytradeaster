"""One bounded import step. Exchange I/O happens outside the SQLite write lock."""

from __future__ import annotations

import time
from collections.abc import Callable

from copytradeaster.history.reader import HistoryReader, Window
from copytradeaster.storage.history import HistoryRepository, HistoryScope


class HistorySynchronizer:
    """Called serially by the history worker; durable pending windows survive restart."""

    def __init__(
        self,
        reader: HistoryReader,
        repository: HistoryRepository,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._reader = reader
        self._repository = repository
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    async def sync_fills(
        self,
        scope: HistoryScope,
        symbol: str,
        fallback: Window,
        *,
        request_budget: int = 8,
    ) -> None:
        window = await self._next_window(scope, "fills", symbol, fallback)
        result = await self._reader.fills(
            scope.account_id,
            symbol,
            window,
            request_budget=request_budget,
        )
        await self._repository.commit_fills(
            scope,
            symbol,
            window,
            result,
            checked_at=self._clock_ms(),
        )

    async def sync_income(
        self,
        scope: HistoryScope,
        fallback: Window,
        *,
        request_budget: int = 2,
    ) -> None:
        window = await self._next_window(scope, "income", "", fallback)
        result = await self._reader.income(scope.account_id, window, request_budget=request_budget)
        await self._repository.commit_income(scope, window, result, checked_at=self._clock_ms())

    async def _next_window(
        self,
        scope: HistoryScope,
        stream: str,
        symbol: str,
        fallback: Window,
    ) -> Window:
        states = await self._repository.windows(scope, stream, symbol)
        for row in states:
            if row["state"] == "pending":
                return Window(row["start_ms"], row["end_ms"])
        # Saturated timestamps remain an explicit gap for separate reconciliation;
        # repeatedly selecting them would starve all subsequent history.
        return fallback
