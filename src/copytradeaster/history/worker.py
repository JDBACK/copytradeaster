"""Single serial history worker with bounded steps and per-stream failure isolation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from copytradeaster.history.reader import Window
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.logging_utils import get_logger
from copytradeaster.storage.history import HistoryScope

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HistoryJob:
    scope: HistoryScope
    symbol: str | None  # None is the account-wide income stream.
    repair: bool = False
    capture: bool = False


class HistoryWorker:
    def __init__(
        self,
        discover: Callable[[], Awaitable[list[HistoryJob]]],
        synchronizer: Callable[[HistoryScope], HistorySynchronizer],
        capture: Callable[[HistoryScope, str], Awaitable[bool]] | None = None,
        *,
        interval: float = 5.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("history interval must be positive")
        self._discover = discover
        self._synchronizer = synchronizer
        self._capture = capture
        self._interval = interval
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task | None = None
        self._last_job: HistoryJob | None = None
        self._failures: dict[HistoryJob, tuple[int, int]] = {}
        self._dirty: set[tuple[str, str, str]] = set()

    def mark_dirty(self, scope: HistoryScope, symbol: str) -> None:
        """Prioritize one account-symbol on the next rotations.

        A best-effort wake-up hint only: the periodic sweep covers a missed
        hint, so this state is intentionally not durable.
        """
        if symbol:
            self._dirty.add((scope.account_id, scope.environment, symbol))

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="follower-history")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run_once(self) -> None:
        jobs = list(dict.fromkeys(await self._discover()))
        current = set(jobs)
        known = {(job.scope.account_id, job.scope.environment) for job in jobs}
        self._failures = {job: value for job, value in self._failures.items() if job in current}
        self._dirty = {key for key in self._dirty if key[:2] in known}
        if self._last_job in jobs:
            index = jobs.index(self._last_job) + 1
            jobs = jobs[index:] + jobs[:index]
        jobs = sorted(
            jobs,
            key=lambda job: (
                0
                if (
                    job.symbol is not None
                    and (job.scope.account_id, job.scope.environment, job.symbol) in self._dirty
                )
                else 1,
            ),
        )
        now = self._clock()
        for job in jobs:
            failures, retry_at = self._failures.get(job, (0, 0))
            if now < retry_at:
                continue
            self._last_job = job
            self._dirty.discard((job.scope.account_id, job.scope.environment, job.symbol or ""))
            try:
                if job.capture:
                    if self._capture is None:
                        raise ValueError("capture job without capture wiring")
                    await self._capture(job.scope, job.symbol or "")
                else:
                    sync = self._synchronizer(job.scope)
                    window = Window(max(0, now - 7 * 86400000), now)
                    if job.repair:
                        await sync.repair_order(job.scope)
                    elif job.symbol is None:
                        await sync.sync_income(job.scope, window, request_budget=1)
                    else:
                        await sync.sync_fills(job.scope, job.symbol, window, request_budget=1)
            except Exception:
                count = min(failures + 1, 8)
                self._failures[job] = (count, now + min(300000, 5000 * 2**count))
                log.warning(
                    "history sync failed account_id=%s stream=%s",
                    job.scope.account_id,
                    "income"
                    if job.symbol is None
                    else f"capture:{job.symbol}"
                    if job.capture
                    else "fills",
                )
            else:
                self._failures.pop(job, None)
            return  # One job at most per tick, regardless of account count.

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.warning("history discovery unavailable")
            await asyncio.sleep(self._interval)
