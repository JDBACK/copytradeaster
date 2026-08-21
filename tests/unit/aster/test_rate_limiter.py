import time

from copytradeaster.aster.rate_limits import RateLimits, SlidingWindowLimiter


async def test_limiter_allows_under_limit():
    limiter = SlidingWindowLimiter(limit=10, window_s=1.0)
    start = time.monotonic()
    for _ in range(10):
        await limiter.acquire(1)
    assert time.monotonic() - start < 0.5


async def test_limiter_blocks_over_limit_and_releases():
    limiter = SlidingWindowLimiter(limit=2, window_s=0.2)
    await limiter.acquire(1)
    await limiter.acquire(1)
    start = time.monotonic()
    await limiter.acquire(1)  # must sleep ~window
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


async def test_limiter_weights_accumulate():
    limiter = SlidingWindowLimiter(limit=10, window_s=0.2)
    await limiter.acquire(6)
    start = time.monotonic()
    await limiter.acquire(5)  # exceeds remaining; waits for window slide
    assert time.monotonic() - start >= 0.15


async def test_rate_limits_per_account_independence():
    limits = RateLimits(
        weight_per_min=2,
        orders_per_min=2,
        orders_per_10s=2,
        orders_min_window_s=0.2,
        orders_10s_window_s=0.2,
    )
    for _ in range(2):
        await limits.acquire_order("a1")
    start = time.monotonic()
    for _ in range(2):
        await limits.acquire_order("a2")  # independent window: no wait expected
    assert time.monotonic() - start < 0.05


async def test_rate_limits_orders_block_within_account():
    limits = RateLimits(
        weight_per_min=100,
        orders_per_min=2,
        orders_per_10s=100,
        orders_min_window_s=0.2,
        orders_10s_window_s=0.2,
    )
    for _ in range(2):
        await limits.acquire_order("a1")
    start = time.monotonic()
    await limits.acquire_order("a1")  # third order within window -> blocks
    assert time.monotonic() - start >= 0.15


async def test_rate_limits_weight_budget_shared():
    limits = RateLimits(
        weight_per_min=5, orders_per_min=1000, orders_per_10s=1000, weight_window_s=0.2
    )
    await limits.acquire_request(4)
    start = time.monotonic()
    await limits.acquire_request(2)  # blocks until window slides
    assert time.monotonic() - start >= 0.15
