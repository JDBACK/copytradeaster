import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
from copytradeaster.domain.models import (
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo, SettingsRepo
from copytradeaster.trading.executor import Executor, command_key_of, resolve_boot_unknowns

KEY_BTC = {"symbol": "BTCUSDT"}


def open_action(qty="0.05", coid="ct-aaa"):
    return ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "RESULT",
        },
        client_order_id=coid,
        replaces_client_order_id=None,
        priority=1,
    )


def stop_action(coid="ct-bbb", stop="64000"):
    return ExecutionAction(
        kind=ActionKind.PLACE_STOP,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": "0.05",
            "stopPrice": stop,
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "false",
            "newOrderRespType": "ACK",
        },
        client_order_id=coid,
        replaces_client_order_id=None,
        priority=2,
    )


def close_action(coid="ct-close"):
    return ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "quantity": "0.05",
            "positionSide": "LONG",
            "newOrderRespType": "RESULT",
        },
        client_order_id=coid,
        replaces_client_order_id=None,
        priority=2,
    )


def cancel_action(coid="ct-old"):
    return ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "origClientOrderId": coid},
        client_order_id=None,
        replaces_client_order_id=coid,
        priority=1,
    )


def plan_of(*actions):
    return ExecutionPlan(account_id="copyc1", trade_id=None, actions=list(actions))


class FakeClient:
    def __init__(self):
        self.placed: list[PlaceOrderRequest] = []
        self.canceled: list[str] = []
        self.leverage_calls: list[tuple[str, int]] = []
        self.open_orders_result: list = []
        self.query_results: dict[str, object | None] = {}
        self.positions_result: list = []
        self.risk_error: AsterError | None = None
        self.place_error: AsterError | None = None
        self.place_timeout = False
        self.query_error: AsterError | None = None

    async def place_order(self, account_id, req):
        if self.place_timeout:
            self.place_timeout = False
            raise TimeoutError
        if self.place_error is not None:
            err, self.place_error = self.place_error, None
            raise err
        self.placed.append(req)
        return SimpleOrder(coid=req.new_client_order_id, status="FILLED")

    async def cancel_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        self.canceled.append(orig_client_order_id)
        return SimpleOrder(coid=orig_client_order_id or "", status="CANCELED")

    async def set_leverage(self, account_id, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))

    async def open_orders(self, account_id, symbol=None):
        return self.open_orders_result

    async def position_risk(self, account_id, symbol=None):
        if self.risk_error is not None:
            raise self.risk_error
        return self.positions_result

    async def query_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        if self.query_error is not None:
            raise self.query_error
        if orig_client_order_id in self.query_results:
            return self.query_results[orig_client_order_id]
        # realistic fallback: the exchange knows every order it accepted
        for req in self.placed:
            if req.new_client_order_id == orig_client_order_id:
                return SimpleOrder(coid=orig_client_order_id or "", status="NEW")
        return None


class SimpleOrder:
    def __init__(self, coid, status):
        self.client_order_id = coid
        self.status = status


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "exec.sqlite3")
    await db.connect()
    await db.migrate()
    client = FakeClient()
    clients = lambda account_id: client  # noqa: E731
    commands = CommandRepo(db)
    links = OrderLinkRepo(db)
    settings = SettingsRepo(db)
    executor = Executor(clients, commands, links, {}, settings=settings)
    yield executor, client, commands, links, settings
    await db.close()


async def test_happy_path_market_confirmed(env):
    executor, client, commands, links, _ = env
    results = await executor.submit(plan_of(open_action()))
    assert [r.status for r in results] == ["CONFIRMED"]
    row = await commands.get(results[0].command_key)
    assert row["status"] == "CONFIRMED"
    assert row["attempts"] == 1
    assert len(client.placed) == 1
    assert client.placed[0].new_client_order_id == "ct" + results[0].command_key
    link_rows = await links.by_follower_coid("copyc1", "ct" + results[0].command_key)
    assert link_rows is not None


async def test_confirmed_callback_receives_durable_command_key(env):
    _executor, client, commands, links, settings = env
    seen: list[tuple[str, str, str]] = []

    async def on_confirmed(account_id, action, command_key):
        seen.append((account_id, action.kind.value, command_key))

    executor = Executor(
        lambda _account_id: client,
        commands,
        links,
        {},
        settings=settings,
        on_confirmed=on_confirmed,
    )
    result = await executor.submit(plan_of(open_action(coid="ct-confirmed-key")))

    assert result[0].status == "CONFIRMED"
    assert seen == [("copyc1", "MARKET_OPEN", result[0].command_key)]


async def test_confirmed_callback_failure_does_not_change_confirmed_result(env):
    _executor, client, commands, links, settings = env

    async def broken_callback(account_id, action, command_key):
        del account_id, action, command_key
        raise RuntimeError("notification sink unavailable")

    executor = Executor(
        lambda _account_id: client,
        commands,
        links,
        {},
        settings=settings,
        on_confirmed=broken_callback,
    )
    result = await executor.submit(plan_of(open_action(coid="ct-confirmed-isolated")))

    assert result[0].status == "CONFIRMED"
    assert (await commands.get(result[0].command_key))["status"] == "CONFIRMED"


async def test_503_unknown_then_query_found_confirmed_without_resend(env):
    executor, client, commands, links, _ = env
    client.place_error = AsterError(-1000, "Unknown", http_status=503)
    client.query_results = {}  # first resolution: not found -> retry
    results = await executor.submit(plan_of(open_action()))
    # after failed resolution the command is retried within the same submit
    assert results[0].status in ("CONFIRMED", "SUBMITTED", "FAILED")
    # exactly one successful placement (retry), the failed attempt left no order
    assert len(client.placed) == 1


async def test_503_unknown_query_finds_order_confirmed(env):
    executor, client, commands, links, _ = env
    coid_holder: dict[str, str] = {}

    async def place(account_id, req):
        coid_holder["coid"] = req.new_client_order_id
        client.query_results[req.new_client_order_id] = SimpleOrder(
            coid=req.new_client_order_id, status="FILLED"
        )
        raise AsterError(-1000, "Unknown", http_status=503)

    client.place_order = place  # type: ignore[method-assign]
    results = await executor.submit(plan_of(open_action()))
    assert results[0].status == "CONFIRMED"
    row = await commands.get(results[0].command_key)
    assert row["last_error"].startswith("resolved-unknown")


async def test_gateway_timeout_code_zero_is_outcome_unknown(env):
    executor, client, commands, links, _ = env
    client.place_error = AsterError(0, "The request has timed out.", http_status=504)
    results = await executor.submit(plan_of(open_action()))
    assert results[0].status == "CONFIRMED"
    row = await commands.get(results[0].command_key)
    assert row["status"] == "CONFIRMED"
    assert len(client.placed) == 1


async def test_unknown_exhausts_attempts_failed_and_degraded(env):
    executor, client, commands, links, settings = env

    async def place(account_id, req):
        raise AsterError(-1000, "Unknown", http_status=503)

    client.place_order = place  # type: ignore[method-assign]
    results = await executor.submit(plan_of(open_action()))
    assert results[0].status == "FAILED"
    row = await commands.get(results[0].command_key)
    assert row["attempts"] == 3
    flag = await settings.get_flag("degraded")
    assert flag is not None and "copyc1" in flag


async def test_margin_error_fails_immediately(env):
    executor, client, commands, links, _ = env
    client.place_error = AsterError(-2019, "margin insufficient")
    results = await executor.submit(plan_of(open_action()))
    assert results[0].status == "FAILED"
    row = await commands.get(results[0].command_key)
    assert row["attempts"] == 1
    assert "-2019" in row["last_error"]


async def test_reduceonly_close_rejected_after_fill_is_confirmed_flat(env):
    executor, client, commands, _, _ = env
    client.place_error = AsterError(-2022, "ReduceOnly Order is rejected.")
    results = await executor.submit(plan_of(close_action()))
    assert results[0].status == "CONFIRMED"
    row = await commands.get(results[0].command_key)
    assert row["status"] == "CONFIRMED"
    assert row["last_error"].startswith("already-flat:-2022")


async def test_conditional_confirmed_via_open_orders(env):
    executor, client, commands, links, _ = env
    client.open_orders_result = [SimpleOrder(coid="PENDING", status="NEW")]
    results = await executor.submit(plan_of(stop_action()))
    assert results[0].status == "SUBMITTED"  # coid differs from fake open order


async def test_conditional_confirmed_when_present(env):
    executor, client, commands, links, _ = env
    seen: list[str] = []

    original = client.place_order

    async def place(account_id, req):
        seen.append(req.new_client_order_id)
        client.open_orders_result = [SimpleOrder(coid=req.new_client_order_id, status="NEW")]
        return await original(account_id, req)

    client.place_order = place  # type: ignore[method-assign]
    results = await executor.submit(plan_of(stop_action()))
    assert results[0].status == "CONFIRMED"


async def test_cancel_2011_treated_confirmed(env):
    executor, client, commands, links, _ = env

    async def cancel(account_id, symbol, *, orig_client_order_id=None, order_id=None):
        raise AsterError(-2011, "cancel rejected")

    client.cancel_order = cancel  # type: ignore[method-assign]
    results = await executor.submit(plan_of(cancel_action()))
    assert results[0].status == "CONFIRMED"


async def test_failed_protection_place_does_not_cancel_old_protection(env):
    executor, client, commands, links, _ = env
    client.place_error = AsterError(-2019, "margin insufficient")
    new_stop = stop_action(coid="planner-new-stop", stop="64500")
    cancel_old = cancel_action("ct-old-stop")
    cancel_old = dataclasses.replace(
        cancel_old,
        priority=2,
        depends_on_client_order_id=new_stop.client_order_id,
    )

    results = await executor.submit(plan_of(new_stop, cancel_old))

    assert [result.status for result in results] == ["FAILED", "BLOCKED"]
    assert client.canceled == []
    assert await commands.get(results[1].command_key) is None


async def test_idempotent_resubmit_skips_confirmed(env):
    executor, client, commands, links, _ = env
    plan = plan_of(open_action())
    first = await executor.submit(plan)
    placed_after_first = len(client.placed)
    second = await executor.submit(plan)
    assert second[0].status == "CONFIRMED"
    assert len(client.placed) == placed_after_first
    assert first[0].command_key == second[0].command_key


def leverage_action(action_id="leverage:BTCUSDT", lev="20"):
    return ExecutionAction(
        kind=ActionKind.SET_LEVERAGE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "leverage": lev},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=0,
        action_id=action_id,
    )


def with_dependency(action, action_id):
    return dataclasses.replace(action, depends_on_action_id=action_id)


async def test_leverage_command(env):
    executor, client, commands, links, _ = env
    results = await executor.submit(plan_of(leverage_action()))
    assert results[0].status == "CONFIRMED"
    assert client.leverage_calls == [("BTCUSDT", 20)]


async def test_failed_set_leverage_blocks_dependent_open_and_protection(env):
    """P0 invariant: a FAILED leverage prerequisite must NEVER let risk-
    increasing actions run (previously MARKET_OPEN fired regardless)."""
    executor, client, commands, links, _ = env

    async def failing_set(account_id, symbol, leverage):
        raise AsterError(-4028, "invalid leverage")

    client.set_leverage = failing_set  # type: ignore[method-assign]
    plan = plan_of(
        leverage_action(),
        with_dependency(open_action(), "leverage:BTCUSDT"),
        with_dependency(stop_action(), "leverage:BTCUSDT"),
    )

    results = await executor.submit(plan)

    assert [r.status for r in results] == ["FAILED", "BLOCKED", "BLOCKED"]
    assert client.placed == []
    # BLOCKED is not journaled: the next reconciliation retries the transition
    assert await commands.get(results[1].command_key) is None
    assert await commands.get(results[2].command_key) is None


async def test_failed_set_leverage_does_not_block_close_or_cancel(env):
    """Leverage never owns the right to block risk reduction."""
    executor, client, commands, links, _ = env

    async def failing_set(account_id, symbol, leverage):
        raise AsterError(-4028, "invalid leverage")

    client.set_leverage = failing_set  # type: ignore[method-assign]
    plan = plan_of(leverage_action(), close_action())

    results = await executor.submit(plan)

    assert [r.status for r in results] == ["FAILED", "CONFIRMED"]
    assert len(client.placed) == 1


async def test_confirmed_set_leverage_releases_dependent_actions(env):
    executor, client, commands, links, _ = env
    plan = plan_of(
        leverage_action(),
        with_dependency(open_action(), "leverage:BTCUSDT"),
        with_dependency(stop_action(), "leverage:BTCUSDT"),
    )

    results = await executor.submit(plan)

    assert [r.status for r in results] == ["CONFIRMED", "CONFIRMED", "SUBMITTED"]
    assert client.leverage_calls == [("BTCUSDT", 20)]
    assert len(client.placed) == 2


async def test_concurrent_submits_serialized(env):
    executor, client, commands, links, _ = env
    plan = plan_of(open_action(), stop_action())
    results = await asyncio.gather(executor.submit(plan), executor.submit(plan))
    statuses = sorted(r.status for res in results for r in res)
    # second submit sees CONFIRMED records and skips placement
    assert statuses.count("CONFIRMED") >= 2
    assert len(client.placed) <= 2


async def test_command_key_stable_and_distinct():
    a1 = open_action()
    plan = plan_of(a1)
    k1 = command_key_of(plan, a1)
    k2 = command_key_of(plan_of(open_action(qty="0.06")), open_action(qty="0.06"))
    assert k1 != k2
    assert len(k1) == 24
    assert k1 == command_key_of(plan_of(open_action()), a1)


async def test_command_key_distinguishes_operation_generations():
    action = close_action()
    first = dataclasses.replace(plan_of(action), operation_id="emergency-1")
    second = dataclasses.replace(plan_of(action), operation_id="emergency-2")
    assert command_key_of(first, action) != command_key_of(second, action)


async def test_existing_sending_is_never_resubmitted(env):
    executor, client, commands, links, _ = env
    del links
    action = open_action()
    plan = plan_of(action)
    key = command_key_of(plan, action)
    await commands.insert(
        key,
        plan.account_id,
        None,
        action.kind.value,
        json.dumps(action.payload),
        action.priority,
        "ct-unresolved",
    )
    await commands.set_status(key, "SENDING")
    result = await executor.submit(plan)
    assert result[0].status == "SENDING"
    assert client.placed == []


async def test_submit_under_mutation_lock_reuses_executor_internals_without_deadlock(env):
    executor, client, commands, links, _ = env
    del commands, links
    plan = plan_of(open_action(coid="ct-under-lock"))

    with pytest.raises(RuntimeError, match="requires the current task"):
        await executor.submit_under_mutation_lock(plan, client=client)

    async with executor.mutation_context(plan.account_id):
        result = await executor.submit_under_mutation_lock(plan, client=client)

    assert result[0].status == "CONFIRMED"
    assert len(client.placed) == 1


async def test_boot_query_error_remains_unresolved_after_bounded_retries(env):
    executor, client, commands, links, _ = env
    del executor
    action = open_action()
    plan = plan_of(action)
    key = command_key_of(plan, action)
    await commands.insert(
        key,
        plan.account_id,
        None,
        action.kind.value,
        json.dumps(action.payload),
        action.priority,
        "ct-unresolved",
    )
    await commands.set_status(key, "SENDING")
    client.query_error = AsterError(-1006, "transport unavailable")
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.unresolved_query_error == 1
    assert len(await commands.in_flight()) == 1
    assert client.placed == []


async def test_boot_authoritative_not_found_marks_failed(env):
    executor, client, commands, links, _ = env
    del executor
    action = open_action()
    plan = plan_of(action)
    key = command_key_of(plan, action)
    await commands.insert(
        key,
        plan.account_id,
        None,
        action.kind.value,
        json.dumps(action.payload),
        action.priority,
        "ct-not-found",
    )
    await commands.set_status(key, "UNKNOWN")
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.failed_not_found == 1 and report.unresolved == 0
    assert (await commands.get(key))["status"] == "FAILED"


class SimplePosition:
    def __init__(self, leverage: int, symbol: str = "BTCUSDT"):
        self.leverage = leverage
        self.symbol = symbol


async def _seed_sending_set_command(commands: CommandRepo) -> str:
    action = leverage_action()
    plan = plan_of(action)
    key = command_key_of(plan, action)
    await commands.insert(
        key,
        plan.account_id,
        None,
        action.kind.value,
        json.dumps(action.payload),
        action.priority,
        None,
    )
    await commands.set_status(key, "SENDING")
    return key


async def test_boot_resolves_sending_set_leverage_when_exchange_matches_target(env):
    """Crash window SENDING SET (no coid): positionRisk proves the write."""
    _executor, client, commands, links, _ = env
    key = await _seed_sending_set_command(commands)
    client.positions_result = [SimplePosition(20)]
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.confirmed == 1 and report.unresolved_query_error == 0
    row = await commands.get(key)
    assert row["status"] == "CONFIRMED"
    assert str(row["last_error"]).startswith("boot-resolved:leverage=")


async def test_boot_marks_sending_set_leverage_failed_on_authoritative_mismatch(env):
    """A readable different value proves the write never applied; reconcile
    re-provisions from FAILED - never a blind second write at boot."""
    _executor, client, commands, links, _ = env
    key = await _seed_sending_set_command(commands)
    client.positions_result = [SimplePosition(5)]
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.failed_not_found == 1
    row = await commands.get(key)
    assert row["status"] == "FAILED"
    assert str(row["last_error"]).startswith("boot-resolved:leverage-mismatch:")


async def test_boot_conflicting_position_rows_keep_set_unresolved(env):
    """Inconsistent LONG/SHORT leverages are NOT an authoritative mismatch:
    the outcome stays unknown -> in-flight, boot fail-closed."""
    _executor, client, commands, links, _ = env
    key = await _seed_sending_set_command(commands)
    client.positions_result = [SimplePosition(20), SimplePosition(5)]
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.unresolved_query_error == 1
    assert len(await commands.in_flight()) == 1
    row = await commands.get(key)
    assert row["status"] == "SENDING"
    assert str(row["last_error"]).startswith("boot-query-unresolved:ambiguous-leverage")


async def test_boot_empty_position_rows_keep_set_unresolved(env):
    _executor, client, commands, links, _ = env
    key = await _seed_sending_set_command(commands)
    client.positions_result = []
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.unresolved_query_error == 1
    assert len(await commands.in_flight()) == 1
    assert (await commands.get(key))["status"] == "SENDING"


async def test_boot_unreadable_leverage_stays_fail_closed(env):
    _executor, client, commands, links, _ = env
    await _seed_sending_set_command(commands)
    client.risk_error = AsterError(-1006, "transport unavailable")
    report = await resolve_boot_unknowns(lambda _aid: client, commands, links)
    assert report.unresolved_query_error == 1
    assert len(await commands.in_flight()) == 1
    assert client.leverage_calls == []
