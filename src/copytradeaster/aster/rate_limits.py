"""Sliding-window rate limiters matching Aster V3 limits:

- REQUEST_WEIGHT per IP per minute;
- ORDERS per account per minute and per 10 seconds.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class SlidingWindowLimiter:
    """Weighted sliding window over `window_s` seconds."""

    def __init__(self, limit: int, window_s: float) -> None:
        self._limit = limit
        self._window_s = window_s
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self._window_s:
            _, w = self._events.popleft()
            self._used -= w

    async def acquire(self, weight: int = 1) -> None:
        while True:
            now = time.monotonic()
            self._prune(now)
            if self._used + weight <= self._limit:
                self._events.append((now, weight))
                self._used += weight
                return
            wait = self._events[0][0] + self._window_s - now
            await asyncio.sleep(max(wait, 0.01))


class RateLimits:
    """Windows default to the exchange contract (60s / 60s / 10s); the keyword
    overrides exist for fast tests only.

    ``metrics`` is an optional :class:`EngineMetrics`-shaped recorder
    (duck-typed to avoid an import cycle). When present, every acquire records
    limiter wait seconds by kind plus the request weight, so the load gate can
    assert weight/min and limiter backpressure from live traffic.
    """

    def __init__(
        self,
        weight_per_min: int,
        orders_per_min: int,
        orders_per_10s: int,
        *,
        weight_window_s: float = 60.0,
        orders_min_window_s: float = 60.0,
        orders_10s_window_s: float = 10.0,
        metrics=None,
    ) -> None:
        self._request_window = SlidingWindowLimiter(weight_per_min, weight_window_s)
        self._orders_per_min = orders_per_min
        self._orders_per_10s = orders_per_10s
        self._orders_min_window_s = orders_min_window_s
        self._orders_10s_window_s = orders_10s_window_s
        self._order_windows: dict[str, tuple[SlidingWindowLimiter, SlidingWindowLimiter]] = {}
        self._metrics = metrics

    async def acquire_request(self, weight: int) -> None:
        start = time.monotonic()
        await self._request_window.acquire(weight)
        metrics = self._metrics
        if metrics is not None:
            metrics.observe_limiter_wait("request", time.monotonic() - start)
            metrics.observe_request_weight(weight)

    async def acquire_order(self, account_id: str) -> None:
        start = time.monotonic()
        windows = self._order_windows.get(account_id)
        if windows is None:
            windows = (
                SlidingWindowLimiter(self._orders_per_min, self._orders_min_window_s),
                SlidingWindowLimiter(self._orders_per_10s, self._orders_10s_window_s),
            )
            self._order_windows[account_id] = windows
        await windows[0].acquire(1)
        await windows[1].acquire(1)
        metrics = self._metrics
        if metrics is not None:
            metrics.observe_limiter_wait("order", time.monotonic() - start)
