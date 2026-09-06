"""Serial boundary capture: snapshot -> import -> recheck -> publish.

The pure seams already exist (``snapshot`` proves an unchanged position
boundary, ``projection`` publishes a quantity-reconciled window atomically).
This module is the runtime wiring between them:

* :func:`read_boundary` acquires one authoritative ``positionRisk`` snapshot
  with a local clock interval, so :meth:`PositionBoundary.prove_unchanged`
  can bracket the history cutoff with the 2000 ms clock margin.
* :class:`HistoryCapture` serializes publication per
  account/environment/symbol, infers starting seeds from the proven ending
  boundary, reserves stable placeholder refs for partial history, and
  publishes through :class:`HistoryProjection`. Concurrent imports of one
  key interleave outside the lock; the idempotent commit makes that safe,
  and only the prove/infer/publish section is serialized.

Fail-closed contract: any proof failure (changed position, unbracketed
cutoff, incomplete coverage, quantity mismatch) raises
:class:`HistoryReconciliationError` and publishes nothing. A deferred
publication is an expected state, not a fault: durable fills and coverage
already committed by the import stay intact, and the caller retries with a
fresh window. Only unexpected exchange/DB errors merit worker backoff.
Trading execution never depends on this path.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Protocol

from copytradeaster.aster.models import PositionDTO
from copytradeaster.history.models import TradeSource
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import Window
from copytradeaster.history.reconcile import HistoryReconciliationError
from copytradeaster.history.snapshot import PositionBoundary, infer_starting_seeds
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.logging_utils import get_logger
from copytradeaster.storage.episodes import EpisodeRepository, StoredEpisode
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import CommandRepo

log = get_logger(__name__)

MAX_CAPTURE_SPAN_MS = 7 * 86400000
LIVE_ELIGIBLE_MS = 300000


class PositionSnapshotClient(Protocol):
    """The narrow ``positionRisk`` surface needed for boundary evidence."""

    async def position_risk(self, account_id: str, symbol: str) -> list[PositionDTO]: ...


async def read_boundary(
    client: PositionSnapshotClient,
    scope: HistoryScope,
    symbol: str,
    *,
    clock_ms: Callable[[], int] | None = None,
) -> PositionBoundary:
    """Read one authoritative position boundary for ``symbol``.

    Exchange identity errors propagate; scope/time problems raise
    ``HistoryReconciliationError`` via :meth:`PositionBoundary.from_positions`.
    """
    if not symbol:
        raise ValueError("capture symbol required")
    clock = clock_ms or (lambda: int(time.time() * 1000))
    started = clock()
    rows = await client.position_risk(scope.account_id, symbol)
    received = clock()
    return PositionBoundary.from_positions(
        symbol, rows, request_started=started, response_received=received
    )


class HistoryCapture:
    """Serialize reconciled publication per account/environment/symbol.

    ``events`` enables atomic event emission on the default projection;
    an explicitly passed ``projection`` keeps its own wiring.
    """

    def __init__(
        self,
        history: HistoryRepository,
        episodes: EpisodeRepository,
        *,
        events: EventRepository | None = None,
        projection: HistoryProjection | None = None,
        commands: CommandRepo | None = None,
        clock_margin_ms: int = 2000,
    ) -> None:
        if clock_margin_ms < 0:
            raise ValueError("negative clock margin")
        self._history = history
        self._episodes = episodes
        self._projection = projection or HistoryProjection(
            history, episodes, events, commands=commands
        )
        self._margin = clock_margin_ms
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def _lock_for(self, scope: HistoryScope, symbol: str) -> asyncio.Lock:
        key = (scope.account_id, scope.environment, symbol)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def publish_reconciled(
        self,
        scope: HistoryScope,
        symbol: str,
        window: Window,
        *,
        before: PositionBoundary,
        after: PositionBoundary,
        cutoff: int,
        order_sources: Mapping[int, TradeSource] | None = None,
        recorded_at: int | None = None,
        notify_eligible: bool = False,
        require_settled: bool = False,
    ) -> tuple[StoredEpisode, ...]:
        """Prove, infer seeds, and atomically publish one complete window.

        ``before``/``after`` must bracket ``cutoff`` (normally
        ``window.end``); fills come from durable storage, never from the
        caller. Unknown order linkage stays ``unknown`` unless the caller
        passes proven ``order_sources``. Nonzero starting quantities keep a
        stable placeholder ref anchored at ``window.start`` so a later
        backfill supplements the same link instead of minting a new one.
        With ``require_settled``, windows spanning an open position (nonzero
        start and nonzero end) are refused: steady-state publication covers
        only settled ranges so one position never fragments into per-window
        placeholders.
        """
        if not symbol:
            raise ValueError("capture symbol required")
        if before.symbol != symbol or after.symbol != symbol:
            raise HistoryReconciliationError("boundary symbol mismatch")
        async with self._lock_for(scope, symbol):
            before.prove_unchanged(after, cutoff, clock_margin_ms=self._margin)
            fills = await self._history.fills(scope, symbol, window)
            seeds = infer_starting_seeds(
                after,
                window,
                fills,
                placeholder_ref=lambda leg: f"seed:{window.start}:{leg.value}",
            )
            resolved = {}
            for leg, seed in seeds.items():
                if seed.public_ref is not None:
                    ref = await self._episodes.reserve_ref(scope, symbol, leg, seed.public_ref)
                    resolved[leg] = replace(seed, public_ref=ref)
                else:
                    resolved[leg] = seed
            ending = after.quantities()
            if require_settled and (
                any(seed.quantity != 0 for seed in resolved.values())
                and any(qty != 0 for qty in ending.values())
            ):
                raise HistoryReconciliationError("open position across capture window")
            stored = await self._projection.publish_window(
                scope,
                symbol,
                window,
                seeds=resolved,
                ending=ending,
                order_sources=order_sources,
                recorded_at=recorded_at,
                notify_eligible=notify_eligible,
            )
            log.info(
                "history capture published account_id=%s symbol=%s episodes=%d",
                scope.account_id,
                symbol,
                len(stored),
            )
            return stored

    async def _complete_frontier(self, scope: HistoryScope, symbol: str) -> int | None:
        """Newest complete fills-coverage end, or None when nothing is due."""
        states = await self._history.windows(scope, "fills", symbol)
        ends = [row["end_ms"] for row in states if row["state"] == "complete"]
        return max(ends) if ends else None

    async def capture_and_publish(
        self,
        scope: HistoryScope,
        symbol: str,
        window: Window,
        *,
        snapshots: PositionSnapshotClient,
        synchronizer: HistorySynchronizer,
        order_sources: Mapping[int, TradeSource] | None = None,
        request_budget: int = 8,
        clock_ms: Callable[[], int] | None = None,
        notify_eligible: bool = False,
        require_settled: bool = False,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> tuple[tuple[StoredEpisode, ...], Window]:
        """Import to the live edge, then publish what the edge can prove.

        The requested ``window`` is a start hint with a possibly stale end:
        the import runs to ``max(window.end, before.response_received)`` so
        fresh snapshots can bracket the published cutoff. The effective
        cutoff is ``min(complete frontier, after.request_started - margin)``;
        a fast import that leaves no snapshot gap waits out the remainder
        once and re-reads ``after`` a single time. Anything else fail-closes
        with ``HistoryReconciliationError`` and the caller retries on a
        later sweep. ``notify_eligible`` is caller policy ANDed with
        freshness of the published end, so deferred backfill stays silent
        while a live edge notifies. Returns the stored episodes with the
        effectively published window (its end drives checkpoints).
        """
        if not symbol:
            raise ValueError("capture symbol required")
        before = await read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
        edge = max(window.end, before.response_received)
        await synchronizer.sync_fills(
            scope, symbol, Window(window.start, edge), request_budget=request_budget
        )
        after = await read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
        frontier = await self._complete_frontier(scope, symbol)
        if frontier is None or frontier < window.start:
            raise HistoryReconciliationError("capture window has no complete coverage")
        cutoff = min(frontier, after.request_started - self._margin)
        if cutoff < before.response_received and frontier >= before.response_received:
            # Coverage already reaches the proven edge; only the snapshot gap
            # is missing (a fast import). Wait it out once and re-read after.
            # When coverage itself falls short, waiting cannot help: defer.
            delay_s = (before.response_received + self._margin - after.request_started) / 1000.0
            if delay_s > 0:
                await (sleep or asyncio.sleep)(delay_s)
            after = await read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
            cutoff = min(frontier, after.request_started - self._margin)
        if cutoff < window.start:
            raise HistoryReconciliationError("capture window has no provable coverage")
        published = Window(window.start, cutoff)
        fresh = published.end >= after.response_received - LIVE_ELIGIBLE_MS
        stored = await self.publish_reconciled(
            scope,
            symbol,
            published,
            before=before,
            after=after,
            cutoff=cutoff,
            order_sources=order_sources,
            recorded_at=after.response_received,
            notify_eligible=notify_eligible and fresh,
            require_settled=require_settled,
        )
        return stored, published

    async def prepare_window(self, scope: HistoryScope, symbol: str, now_ms: int) -> Window | None:
        """Stable publication window from durable state; no exchange reads.

        Returns None when nothing is due: no complete fills coverage, the
        frontier is already published, or it is too fresh to bracket with the
        clock margin. The start pins to the stored checkpoint (or the first
        attempt) so placeholder anchors survive retries; spans over seven
        days clamp deterministically to the newest slice. The end is a hint:
        capture extends the import to the live edge and publishes whatever
        the fresh snapshots can bracket.
        """
        if not symbol:
            raise ValueError("capture symbol required")
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("capture clock must be a nonnegative integer")
        frontier = await self._complete_frontier(scope, symbol)
        if frontier is None:
            return None
        if frontier + self._margin > now_ms:
            return None
        checkpoint = await self._history.capture_state(scope, symbol)
        captured_end = checkpoint["cap_end_ms"] if checkpoint else None
        if captured_end is not None and frontier <= captured_end:
            return None
        stored_start = checkpoint["cap_start_ms"] if checkpoint else None
        start = stored_start if stored_start is not None else max(0, frontier - MAX_CAPTURE_SPAN_MS)
        if frontier - start > MAX_CAPTURE_SPAN_MS:
            start = frontier - MAX_CAPTURE_SPAN_MS
        return Window(start, frontier)

    async def run_scheduled(
        self,
        scope: HistoryScope,
        symbol: str,
        *,
        snapshots: PositionSnapshotClient,
        synchronizer: HistorySynchronizer,
        now_ms: int,
        request_budget: int = 1,
        order_sources: Mapping[int, TradeSource] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> bool:
        """Publish one due window; False means skipped without exchange reads.

        Raises on attempted-but-deferred publication (the worker backs off)
        and records a secret-safe checkpoint in both cases. Eligibility is
        decided on the effectively published end: fresh live edges notify,
        older backfill edges stay silent.
        """
        window = await self.prepare_window(scope, symbol, now_ms)
        if window is None:
            return False
        try:
            _, published = await self.capture_and_publish(
                scope,
                symbol,
                window,
                snapshots=snapshots,
                synchronizer=synchronizer,
                request_budget=request_budget,
                order_sources=order_sources,
                clock_ms=clock_ms,
                notify_eligible=True,
                require_settled=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._history.record_capture(
                scope,
                symbol,
                start_ms=window.start,
                end_ms=(await self._history.capture_state(scope, symbol) or {}).get("cap_end_ms"),
                now_ms=now_ms,
                error=_safe_category(exc),
            )
            raise
        await self._history.record_capture(
            scope, symbol, start_ms=published.start, end_ms=published.end, now_ms=now_ms
        )
        return True


def _safe_category(exc: BaseException) -> str:
    """Exception type name only; messages may carry secrets or URLs."""
    text = "".join(char if char.isalnum() else "_" for char in type(exc).__name__)
    return (text.strip("_") or "error")[:64]
