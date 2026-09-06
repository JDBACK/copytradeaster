from unittest.mock import AsyncMock, Mock

from copytradeaster.history.worker import HistoryJob, HistoryWorker
from copytradeaster.storage.history import HistoryScope


async def test_round_robin_one_request_per_tick_and_removed_scope_pruned():
    a, b = HistoryScope("a", "testnet"), HistoryScope("b", "testnet")
    discover = AsyncMock(return_value=[HistoryJob(a, "BTCUSDT"), HistoryJob(b, None)])
    sync = AsyncMock()
    worker = HistoryWorker(discover, Mock(return_value=sync), clock_ms=lambda: 1000)
    await worker.run_once()
    sync.sync_fills.assert_awaited_once()
    sync.sync_income.assert_not_awaited()
    await worker.run_once()
    sync.sync_income.assert_awaited_once()
    assert sync.sync_income.await_args.kwargs["request_budget"] == 1


async def test_failed_account_backoff_does_not_starve_other_account():
    a, b = HistoryScope("a", "testnet"), HistoryScope("b", "testnet")
    sync = AsyncMock()
    sync.sync_fills.side_effect = RuntimeError("unavailable")
    worker = HistoryWorker(
        AsyncMock(return_value=[HistoryJob(a, "BTCUSDT"), HistoryJob(b, None)]),
        Mock(return_value=sync),
        clock_ms=lambda: 1000,
    )
    for _ in range(3):
        await worker.run_once()
    assert sync.sync_fills.await_count == 1
    assert sync.sync_income.await_count == 2


async def test_stop_cancels_inflight_read_and_start_is_idempotent():
    import asyncio

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def discover():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = HistoryWorker(discover, Mock())
    await worker.start()
    task = worker._task
    await worker.start()
    assert worker._task is task
    await entered.wait()
    await worker.stop()
    assert cancelled.is_set()
    assert task.done()
    await worker.stop()
