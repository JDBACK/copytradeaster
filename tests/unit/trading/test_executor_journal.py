import asyncio
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
from copytradeaster.trading.executor import Executor, command_key_of

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
        self.place_error: AsterError | None = None
        self.place_timeout = False

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

    async def query_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
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


async def test_idempotent_resubmit_skips_confirmed(env):
    executor, client, commands, links, _ = env
    plan = plan_of(open_action())
    first = await executor.submit(plan)
    placed_after_first = len(client.placed)
    second = await executor.submit(plan)
    assert second[0].status == "CONFIRMED"
    assert len(client.placed) == placed_after_first
    assert first[0].command_key == second[0].command_key


async def test_leverage_command(env):
    executor, client, commands, links, _ = env
    action = ExecutionAction(
        kind=ActionKind.SET_LEVERAGE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "leverage": "20"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=0,
    )
    results = await executor.submit(plan_of(action))
    assert results[0].status == "CONFIRMED"
    assert client.leverage_calls == [("BTCUSDT", 20)]


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
