import asyncio

import pytest

from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor
from copytradeaster.trading.registry import FollowerRegistry


def runtime(account_id: str) -> FollowerRuntime:
    return FollowerRuntime(account_id=account_id, client=object(), identity=None)


def test_registry_rejects_duplicate_boot_ids():
    with pytest.raises(ValueError, match="duplicate"):
        FollowerRegistry([runtime("copyc1"), runtime("copyc1")])


async def test_registry_publish_and_remove_are_copy_on_write():
    first = runtime("copyc1")
    registry = FollowerRegistry([first])
    generation = registry.snapshot()

    second = runtime("copyc2")
    replacement = runtime("copyc1")
    await registry.publish_new(second)
    await registry.publish_replacement(replacement, expected=first)

    assert generation == (first,)
    assert registry.snapshot() == (replacement, second)
    assert registry.get("copyc1") is replacement
    with pytest.raises(KeyError):
        await registry.unpublish("missing", expected=replacement)
    assert await registry.unpublish("copyc1", expected=replacement) is replacement
    assert registry.snapshot() == (second,)


async def test_legacy_add_or_replace_name_is_new_only():
    registry = FollowerRegistry([runtime("copyc1")])

    with pytest.raises(ValueError, match="already registered"):
        await registry.add_or_replace(runtime("copyc1"))


async def test_registry_snapshot_never_changes_during_concurrent_publication():
    registry = FollowerRegistry([runtime("copyc1")])
    captured = registry.snapshot()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def reader() -> tuple[str, ...]:
        entered.set()
        await release.wait()
        return tuple(item.account_id for item in captured)

    task = asyncio.create_task(reader())
    await entered.wait()
    await registry.add_or_replace(runtime("copyc2"))
    release.set()

    assert await task == ("copyc1",)
    assert tuple(item.account_id for item in registry.snapshot()) == ("copyc1", "copyc2")


async def test_load_at_boot_replaces_previous_contents():
    registry = FollowerRegistry([runtime("old")])
    await registry.load_at_boot([runtime("copyc2"), runtime("copyc1")])
    assert tuple(item.account_id for item in registry.snapshot()) == ("copyc1", "copyc2")


async def test_executor_mutation_context_is_a_real_idle_barrier():
    executor = Executor(lambda _: object(), object(), object(), {})
    entered = asyncio.Event()
    release = asyncio.Event()
    idle = asyncio.Event()

    async def mutation() -> None:
        async with executor.mutation_context("copyc1"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        await executor.wait_until_idle("copyc1")
        idle.set()

    mutation_task = asyncio.create_task(mutation())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert not idle.is_set()
    release.set()
    await mutation_task
    await waiter_task
    assert idle.is_set()


async def test_coordinator_postfill_drain_cancels_after_bound():
    coordinator = Coordinator.__new__(Coordinator)
    started = asyncio.Event()

    async def stuck() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(stuck())
    coordinator._postfill_tasks = {task}
    task.add_done_callback(coordinator._postfill_tasks.discard)
    await started.wait()

    await coordinator.drain_postfill_tasks(timeout_s=0.01)

    assert task.cancelled()
    assert coordinator._postfill_tasks == set()
