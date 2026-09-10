"""Single serial history worker with bounded steps and per-stream failure isolation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from copytradeaster.history.models import ImportDelta
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
        on_change: Callable[[HistoryScope], None] | None = None,
        *,
        interval: float = 5.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("history interval must be positive")
        self._discover = discover
        self._synchronizer = synchronizer
        self._capture = capture
        self._on_change = on_change
        self._interval = interval
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task | None = None
        self._last_job: HistoryJob | None = None
        self._failures: dict[HistoryJob, tuple[int, int]] = {}
        # Import dirt (new fills/income/repair) and projection dirt (a
        # capture is due) are tracked separately: a finished fills job
        # never clears the capture hint, and fills that actually changed
        # re-arm the capture of the same scope in the next fair slot.
        self._dirty_import: set[tuple[str, str, str]] = set()
        self._dirty_capture: set[tuple[str, str, str]] = set()
        self._sync_requests = 0
        self._snapshot_requests = 0
        self._capture_runs = 0

    @property
    def _dirty(self) -> set[tuple[str, str, str]]:
        return self._dirty_import | self._dirty_capture

    def stats(self) -> dict[str, int]:
        """Observed request/run counters for the capacity budget."""
        return {
            "sync_requests": self._sync_requests,
            "snapshot_requests": self._snapshot_requests,
            "capture_runs": self._capture_runs,
            "requests_total": self._sync_requests + self._snapshot_requests,
        }

    def note_snapshot_request(self) -> None:
        """Count one boundary snapshot read toward the request budget."""
        self._snapshot_requests += 1

    def mark_dirty(self, scope: HistoryScope, symbol: str) -> None:
        """Prioritize one account-symbol on the next rotations.

        A best-effort wake-up hint only: the periodic sweep covers a missed
        hint, so this state is intentionally not durable. An external hint
        arms both the import and the capture so a stale hint still gets one
        prioritized capture attempt.
        """
        if symbol:
            key = (scope.account_id, scope.environment, symbol)
            self._dirty_import.add(key)
            self._dirty_capture.add(key)

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
        self._dirty_import = {key for key in self._dirty_import if key[:2] in known}
        self._dirty_capture = {key for key in self._dirty_capture if key[:2] in known}
        if self._last_job in jobs:
            index = jobs.index(self._last_job) + 1
            jobs = jobs[index:] + jobs[:index]
        jobs = sorted(
            jobs,
            key=lambda job: (
                0
                if (
                    job.symbol is not None
                    and (job.scope.account_id, job.scope.environment, job.symbol)
                    in (self._dirty_capture if job.capture else self._dirty_import)
                )
                else 1,
            ),
        )
        now = self._clock()
        attempts = 0
        for job in jobs:
            failures, retry_at = self._failures.get(job, (0, 0))
            if now < retry_at:
                continue
            attempts += 1
            if attempts > len(jobs):
                return
            self._last_job = job
            key = (job.scope.account_id, job.scope.environment, job.symbol or "")
            try:
                if job.capture:
                    if self._capture is None:
                        raise ValueError("capture job without capture wiring")
                    published = await self._capture(job.scope, job.symbol or "")
                    self._capture_runs += 1
                    if not published:
                        # Nothing due and no exchange reads happened: try the
                        # next job in the same tick instead of idling a full
                        # interval. The rotation bound above keeps this fair.
                        continue
                    self._dirty_capture.discard(key)
                    self._publish_change(job.scope)
                else:
                    sync = self._synchronizer(job.scope)
                    window = Window(max(0, now - 7 * 86400000), now)
                    if job.repair:
                        await sync.repair_order(job.scope)
                    elif job.symbol is None:
                        await sync.sync_income(job.scope, window, request_budget=1)
                    else:
                        delta = await sync.sync_fills(
                            job.scope, job.symbol, window, request_budget=1
                        )
                        if isinstance(delta, ImportDelta):
                            self._sync_requests += delta.requests
                            if delta.inserted_count > 0 or delta.coverage_changed:
                                # Fresh facts re-arm the capture of the same
                                # scope in the next fair slot; other accounts
                                # keep theirs. Mock synchronizers in tests do
                                # not chain.
                                self._dirty_capture.add(key)
                                self._publish_change(job.scope)
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
                if not job.capture:
                    self._dirty_import.discard(key)
            return  # One real job per tick; skips and backoffs fall through.

    def _publish_change(self, scope: HistoryScope) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(scope)
        except Exception:  # noqa: BLE001 - presentation hint cannot break history
            log.warning("history live hint failed account_id=%s", scope.account_id)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.warning("history discovery unavailable")
            await asyncio.sleep(self._interval)
