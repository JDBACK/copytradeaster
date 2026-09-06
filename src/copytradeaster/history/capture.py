"""Serial boundary capture: snapshot -> import -> recheck -> publish.

One asyncio lock per account/environment/symbol serializes the whole
cycle; the lock is released while waiting out a snapshot gap so other
work can proceed. Seven days bound the exchange request size, never the
replay: publication resumes from the durable projection boundary (or the
accepted origin after a late change), so a rolling import window never
mints a second identity for a known episode. Partial (open-start)
projections publish with ``partial_history`` quality instead of being
refused.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Protocol

from copytradeaster.aster.models import PositionDTO
from copytradeaster.history.models import LIVE_ELIGIBLE_MS, ImportDelta, TradeSource
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import Span, Window
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


class PositionSnapshotClient(Protocol):
    """The narrow ``positionRisk`` surface needed for boundary evidence."""

    async def position_risk(self, account_id: str, symbol: str) -> list[PositionDTO]: ...


async def read_boundary(
    snapshots: PositionSnapshotClient,
    scope: HistoryScope,
    symbol: str,
    *,
    clock_ms: Callable[[], int] | None = None,
) -> PositionBoundary:
    """Read one authoritative position boundary for ``symbol``.

    The clock is read before and after the fetch, so the boundary carries
    the real round-trip interval the bracket proof reasons about.
    """
    clock = clock_ms or (lambda: int(time.time() * 1000))
    started = clock()
    positions = await snapshots.position_risk(scope.account_id, symbol)
    received = clock()
    return PositionBoundary.from_positions(
        symbol, positions, request_started=started, response_received=received
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
        on_request: Callable[[], None] | None = None,
    ) -> None:
        if clock_margin_ms < 0:
            raise ValueError("negative clock margin")
        self._on_request = on_request
        self._history = history
        self._episodes = episodes
        self._projection = projection or HistoryProjection(
            history, episodes, events, commands=commands
        )
        self._margin = clock_margin_ms
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def set_on_request(self, on_request: Callable[[], None] | None) -> None:
        """Count boundary snapshot reads toward the request budget."""
        self._on_request = on_request

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
        window: Window | Span,
        *,
        before: PositionBoundary,
        after: PositionBoundary,
        cutoff: int,
        order_sources: Mapping[int, TradeSource] | None = None,
        recorded_at: int | None = None,
        notify_eligible: bool = False,
        delta: ImportDelta | None = None,
    ) -> tuple[StoredEpisode, ...]:
        """Prove, seed once, and atomically publish the replayed projection.

        ``before``/``after`` must bracket ``cutoff``; fills come from durable
        storage, never from the caller. Unknown order linkage stays
        ``unknown`` unless the caller passes proven ``order_sources``. The
        first acceptance infers starting seeds and anchors them as the
        origin; every later call resumes from the stored boundary seeds, so
        the published window start never renames a known episode. Nonzero
        starting quantities publish a partial projection, they no longer
        refuse it. The import ``delta`` enables the late-change rollback to
        the origin inside the projection.
        """
        if not symbol:
            raise ValueError("capture symbol required")
        if before.symbol != symbol or after.symbol != symbol:
            raise HistoryReconciliationError("boundary symbol mismatch")
        async with self._lock_for(scope, symbol):
            before.prove_unchanged(after, cutoff, clock_margin_ms=self._margin)
            latest = await self._episodes.load_boundary(scope, symbol)
            if latest is None:
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
                stored = await self._projection.publish_window(
                    scope,
                    symbol,
                    window,
                    seeds=resolved,
                    ending=after.quantities(),
                    order_sources=order_sources,
                    recorded_at=recorded_at,
                    notify_eligible=notify_eligible,
                    delta=delta,
                )
            else:
                stored = await self._projection.publish_window(
                    scope,
                    symbol,
                    window,
                    seeds=latest.seed_map(),
                    ending=after.quantities(),
                    order_sources=order_sources,
                    recorded_at=recorded_at,
                    notify_eligible=notify_eligible,
                    delta=delta,
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
        window: Window | Span,
        *,
        snapshots: PositionSnapshotClient,
        synchronizer: HistorySynchronizer,
        order_sources: Mapping[int, TradeSource] | None = None,
        request_budget: int = 8,
        clock_ms: Callable[[], int] | None = None,
        notify_eligible: bool = False,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> tuple[tuple[StoredEpisode, ...], Span]:
        """Import to the live edge, then publish what the edge can prove.

        The requested ``window`` is an import start hint with a possibly
        stale end: the import starts at the accepted origin (so late facts
        anywhere after it are picked up) and runs to
        ``max(window.end, before.response_received)`` so fresh snapshots can
        bracket the published cutoff. The effective cutoff is
        ``min(complete frontier, after.request_started - margin)``; a fast
        import that leaves no snapshot gap waits out the remainder once and
        re-reads ``after`` a single time. Anything else fail-closes with
        ``HistoryReconciliationError`` and the caller retries on a later
        sweep. Replay restarts at the durable boundary (origin after a late
        change); its start — not the import hint — anchors identity.
        ``notify_eligible`` is caller policy ANDed with freshness of the
        published end, so deferred backfill stays silent while a live edge
        notifies. Returns the stored episodes with the effectively
        published window (its end drives checkpoints).
        """
        if not symbol:
            raise ValueError("capture symbol required")
        before = await self._read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
        origin = await self._episodes.load_boundary(scope, symbol, kind="origin")
        import_start = origin.cutoff_ms if origin is not None else window.start
        edge = max(window.end, before.response_received)
        delta = await synchronizer.sync_fills(
            scope, symbol, Span(import_start, edge), request_budget=request_budget
        )
        after = await self._read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
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
            after = await self._read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
            cutoff = min(frontier, after.request_started - self._margin)
        if cutoff < window.start:
            raise HistoryReconciliationError("capture window has no provable coverage")
        replay = await self._projection.plan_replay(scope, symbol, delta, window.start)
        if replay.start_ms > cutoff:
            # The proven frontier already covers the requested window and did
            # not grow: nothing to publish, report the window overlap.
            stored = tuple(await self._episodes.list_range(scope, symbol, window.start, cutoff))
            return stored, Span(cutoff, cutoff)
        published = Span(replay.start_ms, cutoff)
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
            delta=delta,
        )
        if not stored:
            # The replay slice grew the proven frontier but projected nothing
            # new. Report what the requested window already shows: the slice
            # alone cannot overlap episodes that closed before it.
            stored = tuple(
                await self._episodes.list_range(scope, symbol, window.start, published.end)
            )
        return stored, published

    async def _read_boundary(
        self,
        snapshots: PositionSnapshotClient,
        scope: HistoryScope,
        symbol: str,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> PositionBoundary:
        """One snapshot read that counts toward the request budget."""
        boundary = await read_boundary(snapshots, scope, symbol, clock_ms=clock_ms)
        if self._on_request is not None:
            self._on_request()
        return boundary

    async def prepare_window(self, scope: HistoryScope, symbol: str, now_ms: int) -> Span | None:
        """Stable import hint from durable state; no exchange reads.

        Returns None when nothing is due: no complete fills coverage, the
        frontier is already published, or it is too fresh to bracket with the
        clock margin. The start pins to the accepted origin (or the legacy
        checkpoint, or zero) so late facts anywhere after it are picked up;
        there is no span clamp — seven days bound the exchange request, not
        the replay. The end is a hint: capture extends the import to the
        live edge and publishes whatever the fresh snapshots can bracket.
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
        origin = await self._episodes.load_boundary(scope, symbol, kind="origin")
        if origin is not None:
            start = origin.cutoff_ms
        elif checkpoint is not None and checkpoint["cap_start_ms"] is not None:
            start = checkpoint["cap_start_ms"]
        else:
            start = 0
        return Span(start, frontier)

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
