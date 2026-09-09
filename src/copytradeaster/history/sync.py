"""One bounded import step. Exchange I/O happens outside the SQLite write lock."""

from __future__ import annotations

import time
from collections.abc import Callable

from copytradeaster.history.models import ImportDelta
from copytradeaster.history.reader import HistoryReader, Span, Window
from copytradeaster.storage.history import HistoryRepository, HistoryScope

MAX_REQUEST_SPAN_MS = 7 * 86400000


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

    async def repair_order(self, scope: HistoryScope) -> None:
        now = self._clock_ms()
        candidate = await self._repository.repair_candidate(scope, now)
        if candidate is None:
            return
        order = await self._reader.order(
            scope.account_id, candidate["symbol"], candidate["client_order_id"]
        )
        if order is not None:
            if (
                order.symbol != candidate["symbol"]
                or order.client_order_id != candidate["client_order_id"]
            ):
                raise ValueError("history order identity mismatch")
            await self._repository.observe_order(
                scope,
                order,
                kind=candidate["kind"],
                command_key=candidate["command_key"],
                observed_at=order.update_time,
            )
        # Mark only after observation persisted. A crash between these writes
        # merely repeats an idempotent read, never loses a repair.
        await self._repository.record_order_check(
            scope, candidate["command_key"], now, found=order is not None
        )

    async def sync_fills(
        self,
        scope: HistoryScope,
        symbol: str,
        fallback: Window | Span,
        *,
        request_budget: int = 8,
    ) -> ImportDelta:
        """Import one bounded step and report what changed for replay planning."""
        window = await self._next_window(scope, "fills", symbol, fallback)
        result = await self._reader.fills(
            scope.account_id,
            symbol,
            window,
            request_budget=request_budget,
        )
        return await self._repository.commit_fills(
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
        fallback: Window | Span,
    ) -> Window:
        states = await self._repository.windows(scope, stream, symbol)
        for row in states:
            if row["state"] == "pending":
                return Window(row["start_ms"], row["end_ms"])
        # Saturated timestamps remain an explicit gap for separate reconciliation;
        # repeatedly selecting them would starve all subsequent history.
        verified_ends = [row["end_ms"] for row in states if row["state"] == "complete"]
        if verified_ends:
            # Bounded overlap recovers late exchange facts. Pending windows above
            # retain priority; no gap is silently promoted to complete coverage.
            start = min(fallback.end, max(fallback.start, max(verified_ends) - 60000))
            return Window(max(start, fallback.end - MAX_REQUEST_SPAN_MS), fallback.end)
        end = min(fallback.end, fallback.start + MAX_REQUEST_SPAN_MS)
        return Window(fallback.start, end)
