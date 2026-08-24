"""Transport-neutral emergency controller tests."""

from contextlib import asynccontextmanager
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from copytradeaster.application.control import (
    EmergencyController,
    EmergencyFailure,
    build_close_plan,
)
from copytradeaster.domain.models import (
    AccountState,
    PositionKey,
    PositionState,
    PosSide,
    ProtectiveOrder,
    Side,
)


def flat(account_id: str) -> AccountState:
    return AccountState(account_id, D(100), {}, [])


def open_position(account_id: str) -> AccountState:
    key = PositionKey("BTCUSDT")
    position = PositionState(
        key=key,
        qty=D("0.05"),
        entry_price=D(65000),
        leverage=20,
        mark_price=D(65000),
        liquidation_price=D(30000),
    )
    return AccountState(account_id, D(100), {key: position}, [])


class FakeLifecycle:
    def __init__(self, account_id: str, snapshots: list):
        self.runtime = SimpleNamespace(account_id=account_id, client=object())
        self.snapshots = list(snapshots)
        self.active = False
        self.snapshot_calls = 0

    async def snapshot(self):
        assert self.active
        self.snapshot_calls += 1
        value = self.snapshots.pop(0) if self.snapshots else self.snapshots[-1]
        if isinstance(value, BaseException):
            raise value
        return value


class FakeExecutor:
    def __init__(self):
        self.calls: list[tuple[str, object, object]] = []

    async def submit_under_mutation_lock(self, plan, *, client):
        self.calls.append((plan.account_id, client, plan))
        return [SimpleNamespace(status="CONFIRMED") for _ in plan.actions]


class FakeState:
    def __init__(self):
        self.account_pauses: list[str] = []
        self.global_begins: list[str] = []
        self.results = []

    async def pause_account(self, account_id: str):
        self.account_pauses.append(account_id)

    async def begin_global(self, operation_id: str):
        self.global_begins.append(operation_id)

    async def record_result(self, report):
        self.results.append(report)


def controller_for(lifecycles, *, state=None, executor=None):
    @asynccontextmanager
    async def factory(account_id):
        lifecycle = lifecycles[account_id]
        lifecycle.active = True
        try:
            yield lifecycle
        finally:
            lifecycle.active = False

    return EmergencyController(
        lifecycle_factory=factory,
        executor=executor or FakeExecutor(),
        state=state or FakeState(),
        pass_delay_s=0,
    )


@pytest.mark.asyncio
async def test_global_emergency_isolates_failure_and_requires_two_proofs():
    lifecycles = {
        "copyc1": FakeLifecycle(
            "copyc1", [open_position("copyc1"), flat("copyc1"), flat("copyc1"), flat("copyc1")]
        ),
        "copyc2": FakeLifecycle("copyc2", [RuntimeError("secret transport detail")] * 5),
    }
    state = FakeState()
    executor = FakeExecutor()
    controller = controller_for(lifecycles, state=state, executor=executor)

    report = await controller.emergency_global(["copyc1", "copyc2"])

    assert report.success is False
    assert report.entries_paused is True
    assert report.emergency_active is True
    assert state.global_begins == [report.operation_id]
    assert state.account_pauses == []
    by_id = {item.account_id: item for item in report.accounts}
    assert by_id["copyc1"].success is True
    assert by_id["copyc1"].flat_proofs == 2
    assert by_id["copyc2"].failure == EmergencyFailure.SNAPSHOT_FAILED
    assert by_id["copyc2"].passes == 5
    assert len(executor.calls) == 1
    assert executor.calls[0][1] is lifecycles["copyc1"].runtime.client
    assert all("secret" not in repr(item) for item in report.accounts)


@pytest.mark.asyncio
async def test_per_account_emergency_uses_two_proofs_and_unique_operation_ids():
    state = FakeState()
    first_lifecycle = FakeLifecycle("copyc1", [flat("copyc1")] * 4)
    first = controller_for({"copyc1": first_lifecycle}, state=state)
    first_report = await first.emergency_account("copyc1")

    second_lifecycle = FakeLifecycle("copyc1", [flat("copyc1")] * 4)
    second = controller_for({"copyc1": second_lifecycle}, state=state)
    second_report = await second.emergency_account("copyc1")

    assert first_report.success and second_report.success
    assert first_report.operation_id != second_report.operation_id
    assert first_report.passes == second_report.passes == 2
    assert state.account_pauses == ["copyc1", "copyc1"]
    assert state.global_begins == []


def test_control_builder_preserves_cancel_then_reduce_only_shape():
    key = PositionKey("BTCUSDT", PosSide.BOTH)
    protection = ProtectiveOrder(
        client_order_id="ct-stop",
        order_id=1,
        type="STOP_MARKET",
        side=Side.SELL,
        stop_price=D(64000),
        reduce_only=True,
        close_position=False,
        working_type="MARK_PRICE",
    )
    position = PositionState(
        key=key,
        qty=D("0.05"),
        entry_price=D(65000),
        leverage=20,
        mark_price=D(65000),
        liquidation_price=D(30000),
        stop=protection,
    )
    actual = AccountState("copyc1", D(100), {key: position}, [])

    plan = build_close_plan("copyc1", actual, operation_id="op-1", revision=1)

    assert [action.kind for action in plan.actions] == [
        "CANCEL_ORDER",
        "MARKET_CLOSE",
    ]
    assert plan.actions[1].payload["reduceOnly"] == "true"
    assert plan.operation_id == "op-1"
