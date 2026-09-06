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


async def test_capture_job_runs_capture_callback_with_scope_and_symbol():
    scope = HistoryScope("a", "testnet")
    capture = AsyncMock(return_value=True)
    worker = HistoryWorker(
        AsyncMock(return_value=[HistoryJob(scope, "BTCUSDT", capture=True)]),
        Mock(return_value=AsyncMock()),
        capture,
        clock_ms=lambda: 1000,
    )
    await worker.run_once()
    capture.assert_awaited_once_with(scope, "BTCUSDT")


async def test_capture_deferral_backs_off_without_starving_fills():
    from copytradeaster.history.reconcile import HistoryReconciliationError

    scope = HistoryScope("a", "testnet")
    capture = AsyncMock(side_effect=HistoryReconciliationError("deferred"))
    sync = AsyncMock()
    worker = HistoryWorker(
        AsyncMock(
            return_value=[
                HistoryJob(scope, "BTCUSDT", capture=True),
                HistoryJob(scope, "BTCUSDT"),
            ]
        ),
        Mock(return_value=sync),
        capture,
        clock_ms=lambda: 1000,
    )
    await worker.run_once()
    assert capture.await_count == 1 and sync.sync_fills.await_count == 0
    await worker.run_once()
    assert capture.await_count == 1 and sync.sync_fills.await_count == 1


async def test_dirty_hint_prioritizes_symbol_and_clears_on_attempt():
    scope = HistoryScope("a", "testnet")
    sync = AsyncMock()
    worker = HistoryWorker(
        AsyncMock(return_value=[HistoryJob(scope, "AAA"), HistoryJob(scope, "BBB")]),
        Mock(return_value=sync),
        clock_ms=lambda: 1000,
    )
    worker.mark_dirty(scope, "BBB")
    worker.mark_dirty(scope, "")
    await worker.run_once()
    assert sync.sync_fills.await_args.args[1] == "BBB"
    assert worker._dirty == set()
    await worker.run_once()
    assert sync.sync_fills.await_args.args[1] == "AAA"


async def test_dirty_hint_for_gone_scope_is_pruned():
    scope = HistoryScope("a", "testnet")
    worker = HistoryWorker(
        AsyncMock(return_value=[HistoryJob(scope, "AAA")]),
        Mock(return_value=AsyncMock()),
        clock_ms=lambda: 1000,
    )
    worker.mark_dirty(HistoryScope("ghost", "testnet"), "BBB")
    await worker.run_once()
    assert worker._dirty == set()


async def test_capture_job_without_wiring_fails_closed():
    scope = HistoryScope("a", "testnet")
    worker = HistoryWorker(
        AsyncMock(return_value=[HistoryJob(scope, "BTCUSDT", capture=True)]),
        Mock(return_value=AsyncMock()),
        clock_ms=lambda: 1000,
    )
    await worker.run_once()
    assert worker._failures != {}
