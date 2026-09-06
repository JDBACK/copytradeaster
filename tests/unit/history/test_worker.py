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
    # A finished fills job clears its own import hint but never the capture
    # hint (mock synchronizers do not chain); the import side is clean.
    assert worker._dirty_import == set()
    assert worker._dirty_capture == {(scope.account_id, scope.environment, "BBB")}
    await worker.run_once()
    assert sync.sync_fills.await_args.args[1] == "AAA"


async def test_fills_change_rearms_capture_in_fair_slot():
    from copytradeaster.history.models import ImportDelta

    scope = HistoryScope("a", "testnet")
    changed = ImportDelta(
        inserted_count=1,
        changed_from_ms=10,
        coverage_changed=True,
        coverage_changed_from_ms=10,
        verified_through_ms=100,
        facts_revision=2,
        requests=1,
    )
    sync = AsyncMock()
    sync.sync_fills.return_value = changed
    capture = AsyncMock(return_value=True)
    worker = HistoryWorker(
        AsyncMock(
            return_value=[
                HistoryJob(scope, "BTCUSDT"),
                HistoryJob(scope, "BTCUSDT", capture=True),
            ]
        ),
        Mock(return_value=sync),
        capture,
        clock_ms=lambda: 1000,
    )
    await worker.run_once()
    assert sync.sync_fills.await_count == 1 and capture.await_count == 0
    assert worker.stats()["sync_requests"] == 1
    await worker.run_once()
    assert capture.await_count == 1
    assert worker.stats()["capture_runs"] == 1
    assert worker._dirty_capture == set()


async def test_skipped_capture_falls_through_to_next_job_same_tick():
    scope = HistoryScope("a", "testnet")
    sync = AsyncMock()
    capture = AsyncMock(side_effect=[False, True])
    worker = HistoryWorker(
        AsyncMock(
            return_value=[
                HistoryJob(scope, "AAA", capture=True),
                HistoryJob(scope, "BBB", capture=True),
            ]
        ),
        Mock(return_value=sync),
        capture,
        clock_ms=lambda: 1000,
    )
    await worker.run_once()
    assert capture.await_count == 2
    assert worker.stats()["capture_runs"] == 2


async def test_capture_job_without_wiring_fails_closed():
    scope = HistoryScope("a", "testnet")
    worker = HistoryWorker(
        AsyncMock(return_value=[HistoryJob(scope, "BTCUSDT", capture=True)]),
        Mock(return_value=AsyncMock()),
        clock_ms=lambda: 1000,
    )
    await worker.run_once()
    assert worker._failures != {}
