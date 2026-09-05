"""Bounded in-RAM load-gate metrics for the production engine.

Asyncio-loop-confined recorder: the engine mutates it from exactly one event
loop, so no locks are taken. Every series is a bounded deque — memory is flat
regardless of uptime.

The load gate reads :meth:`EngineMetrics.snapshot`, whose keys are stable:

- ``reconcile_duration_s`` — one sample per :meth:`observe_reconcile`
  (full sweep wall time, seconds).
- ``follower_duration_s`` — one sample per follower per sweep
  (snapshot + plan + execute wall time; snapshot-only latency is not
  separately observable without touching the coordinator hot path, so the
  per-follower sample is snapshot-inclusive by construction).
- ``limiter_request_wait_s`` / ``limiter_order_wait_s`` — seconds spent
  blocked in the sliding-window limiter before a request/order acquired.
- ``request_weight_per_min`` — summed request weight over the trailing 60 s.
- ``ingest_age_s`` — seconds each master envelope spent queued before consume.
- ``ingest_depth`` / ``ingest_pending_generations`` / ``ingest_dropped_total``
  — gauges sampled at enqueue time.
- ``postfill_pending`` — gauge for coordinator post-fill recomputes in flight.

Each duration/wait series snapshots as
``{"count": n, "p50": x, "p95": y, "p99": z, "max": m}``
(quantile fields are ``None`` while the series is empty); quantiles use the
nearest-rank method over the bounded window.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any


def _quantile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank quantile; ``None`` on an empty series."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = int(math.ceil(q * len(sorted_values))) - 1
    return sorted_values[min(max(rank, 0), len(sorted_values) - 1)]


class _Series:
    """Bounded sample window with p50/p95/p99 description."""

    __slots__ = ("samples",)

    def __init__(self, maxlen: int) -> None:
        self.samples: deque[float] = deque(maxlen=maxlen)

    def add(self, value: float) -> None:
        self.samples.append(float(value))

    def describe(self) -> dict[str, Any]:
        ordered = sorted(self.samples)
        return {
            "count": len(ordered),
            "p50": _quantile(ordered, 0.50),
            "p95": _quantile(ordered, 0.95),
            "p99": _quantile(ordered, 0.99),
            "max": ordered[-1] if ordered else None,
        }


class EngineMetrics:
    """Recall-free recorder; every ``observe_*`` call is O(1) amortized."""

    def __init__(self, *, max_samples: int = 1024) -> None:
        self._reconcile_s = _Series(max_samples)
        self._follower_s = _Series(max_samples)
        self._request_wait_s = _Series(max_samples)
        self._order_wait_s = _Series(max_samples)
        self._ingest_age_s = _Series(max_samples)
        # Weight events arrive per REST call (the hottest series), so they get
        # a deeper window; the per-minute rate still undercounts if the window
        # fills in under 60 s — sized for the follower-cap envelope, not abuse.
        self._weights: deque[tuple[float, int]] = deque(maxlen=8192)
        self._ingest_depth = 0
        self._ingest_pending_generations = 0
        self._ingest_dropped_total = 0
        self._postfill_pending = 0

    def observe_reconcile(self, duration_s: float) -> None:
        self._reconcile_s.add(max(duration_s, 0.0))

    def observe_follower(self, duration_s: float) -> None:
        self._follower_s.add(max(duration_s, 0.0))

    def observe_limiter_wait(self, kind: str, waited_s: float) -> None:
        waited = max(waited_s, 0.0)
        if kind == "order":
            self._order_wait_s.add(waited)
        else:
            self._request_wait_s.add(waited)

    def observe_request_weight(self, weight: int, *, now: float | None = None) -> None:
        self._weights.append((time.monotonic() if now is None else now, int(weight)))

    def observe_ingest_age(self, age_s: float) -> None:
        self._ingest_age_s.add(max(age_s, 0.0))

    def set_ingest(self, depth: int, pending_generations: int, dropped_total: int) -> None:
        self._ingest_depth = max(int(depth), 0)
        self._ingest_pending_generations = max(int(pending_generations), 0)
        self._ingest_dropped_total = max(int(dropped_total), 0)

    def set_postfill_pending(self, pending: int) -> None:
        self._postfill_pending = max(int(pending), 0)

    def request_weight_per_min(self, *, now: float | None = None) -> int:
        """Summed request weight over the trailing 60 s window."""
        current = time.monotonic() if now is None else now
        cutoff = current - 60.0
        return sum(weight for ts, weight in self._weights if ts >= cutoff)

    def snapshot(self) -> dict[str, Any]:
        return {
            "reconcile_duration_s": self._reconcile_s.describe(),
            "follower_duration_s": self._follower_s.describe(),
            "limiter_request_wait_s": self._request_wait_s.describe(),
            "limiter_order_wait_s": self._order_wait_s.describe(),
            "request_weight_per_min": self.request_weight_per_min(),
            "ingest_age_s": self._ingest_age_s.describe(),
            "ingest_depth": self._ingest_depth,
            "ingest_pending_generations": self._ingest_pending_generations,
            "ingest_dropped_total": self._ingest_dropped_total,
            "postfill_pending": self._postfill_pending,
        }
