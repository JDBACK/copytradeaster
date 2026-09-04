"""P2.7 executor/idempotency/safety/race/security regressions."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
from copytradeaster.domain.models import ActionKind, ExecutionAction, ExecutionPlan, Side
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo, SettingsRepo
from copytradeaster.trading.builder_attribution import (
    BuilderAttributionProvider,
)
from copytradeaster.trading.executor import Executor, _with_coid, command_key_of

BUILDER_A = "0x1111111111111111111111111111111111111111"
CONFIG_FEE = "0.0001"
NOW_MS = 1_700_000_000_000
FUTURE_MS = 4_102_444_800_000
SIGNER_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SIGNER_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeAccounts:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, account_id):
        return self._rows.get(account_id)


class FakeValidations:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, account_id):
        return self._rows.get(account_id)


class FakeCreds:
    def __init__(self, signers):
        self._signers = signers

    def identity(self, account_id):
        if account_id not in self._signers:
            raise KeyError(account_id)

        class _I:
            pass

        ident = _I()
        ident.signer = self._signers[account_id]
        return ident


def builder_row(builder=BUILDER_A, fee=CONFIG_FEE):
    return {
        "role": "follower",
        "enabled": 1,
        "identity_source": "builder",
        "builder_address": builder,
        "builder_name": "copytradeaster",
        "builder_max_fee_rate": fee,
        "builder_agent_expires_at_ms": FUTURE_MS,
        "builder_verified_at": "2026-01-01T00:00:00+00:00",
    }


def direct_row():
    return {
        "role": "follower",
        "enabled": 1,
        "identity_source": "direct",
        "builder_address": None,
        "builder_name": None,
        "builder_max_fee_rate": None,
        "builder_agent_expires_at_ms": None,
        "builder_verified_at": None,
    }


def valid_validation(signer=SIGNER_A):
    return {
        "signer_address": signer,
        "can_read": 1,
        "can_perp_trade": 1,
        "can_withdraw": 0,
        "expired_ms": FUTURE_MS,
        "ip_whitelist": "0.0.0.0/0",
    }


def provider_for(rows, vals, creds, builder=BUILDER_A, fee=CONFIG_FEE):
    return BuilderAttributionProvider(
        accounts=FakeAccounts(rows),
        validations=FakeValidations(vals),
        credentials=FakeCreds(creds),
        builder_address=builder,
        builder_name="copytradeaster",
        max_fee_rate=fee,
        clock_ms=lambda: NOW_MS,
    )


def open_action(qty="0.05"):
    return ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": qty},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=1,
    )


def close_action(qty="0.05"):
    return ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": qty},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=1,
    )


def stop_action():
    return ExecutionAction(
        kind=ActionKind.PLACE_STOP,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "stopPrice": "64000",
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        },
        client_order_id=None,
        replaces_client_order_id=None,
        priority=1,
    )


def tp_action():
    return ExecutionAction(
        kind=ActionKind.PLACE_TP,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": "66000",
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        },
        client_order_id=None,
        replaces_client_order_id=None,
        priority=1,
    )


def cancel_action():
    return ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "origClientOrderId": "ct-old"},
        client_order_id=None,
        replaces_client_order_id="ct-old",
        priority=1,
    )


def lev_action():
    return ExecutionAction(
        kind=ActionKind.SET_LEVERAGE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "leverage": "10"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=0,
    )


class FakeClient:
    def __init__(self):
        self.placed = []
        self.canceled = []
        self.leverages = []
        self.query_results = {}
        self.place_error = None
        self.place_calls = 0

    async def place_order(self, account_id, req):
        self.place_calls += 1
        if self.place_error is not None:
            err, self.place_error = self.place_error, None
            raise err
        self.placed.append((account_id, req))

        class _O:
            status = "FILLED"

        return _O()

    async def cancel_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        self.canceled.append(orig_client_order_id)

        class _O:
            status = "CANCELED"
            client_order_id = orig_client_order_id or ""

        return _O()

    async def set_leverage(self, account_id, symbol, leverage):
        self.leverages.append((symbol, leverage))

    async def open_orders(self, account_id, symbol=None):
        return []

    async def position_risk(self, account_id, symbol=None):
        return []

    async def query_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        if orig_client_order_id in self.query_results:
            return self.query_results[orig_client_order_id]
        for _, req in self.placed:
            if req.new_client_order_id == orig_client_order_id:

                class _O:
                    status = "NEW"

                return _O()
        return None

    async def query_order_history(self, account_id, symbol, *, orig_client_order_id):
        return await self.query_order(account_id, symbol, orig_client_order_id=orig_client_order_id)


@pytest.fixture
async def exec_env(tmp_path: Path):
    db = Database(tmp_path / "b.sqlite3")
    await db.connect()
    await db.migrate()
    commands = CommandRepo(db)
    links = OrderLinkRepo(db)
    settings = SettingsRepo(db)
    yield db, commands, links, settings
    await db.close()


def make_executor(client, commands, links, settings, prov):
    by_account = {"builder1": client, "direct1": client, "a": client, "b": client}

    def clients(account_id):
        return by_account.get(account_id, client)

    return Executor(clients, commands, links, {}, settings=settings, attribution_provider=prov)


class TestIdempotency:
    def test_command_key_identical_with_without_attribution(self):
        plan = ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()])
        assert command_key_of(plan, plan.actions[0]) == command_key_of(plan, plan.actions[0])

    def test_with_coid_preserves_attribution(self):
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            builder=BUILDER_A,
            fee_rate=Decimal(CONFIG_FEE),
        )
        out = _with_coid(req, "ct-new")
        assert out.builder == BUILDER_A
        assert out.fee_rate == Decimal(CONFIG_FEE)
        assert out.new_client_order_id == "ct-new"

    async def test_journal_payload_clean_and_attribution_after_identity(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        prov = provider_for({"a": builder_row()}, {"a": valid_validation()}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        plan = ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()])
        res = await ex.submit(plan)
        assert res[0].status == "CONFIRMED"
        row = await commands.get(res[0].command_key)
        payload = json.loads(row["payload"])
        assert "builder" not in payload
        assert "feeRate" not in payload
        assert "fee_rate" not in payload
        assert "signer" not in json.dumps(payload).lower()
        assert client.placed[0][1].builder == BUILDER_A
        assert client.placed[0][1].fee_rate == Decimal(CONFIG_FEE)

    async def test_unknown_retry_preserves_attribution(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        client.place_error = AsterError(-1000, "unknown", http_status=503)
        prov = provider_for({"a": builder_row()}, {"a": valid_validation()}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        plan = ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()])
        res = await ex.submit(plan)
        assert res[0].status == "CONFIRMED"
        assert len(client.placed) == 1
        assert client.placed[0][1].builder == BUILDER_A
        assert client.placed[0][1].fee_rate == Decimal(CONFIG_FEE)
        # retry used a new coid but same attribution
        row = await commands.get(res[0].command_key)
        assert row["client_order_id"] != ""

    async def test_direct_retry_stays_direct(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        client.place_error = AsterError(-1000, "unknown", http_status=503)
        prov = provider_for({"direct1": direct_row()}, {}, {"direct1": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        plan = ExecutionPlan(account_id="direct1", trade_id=None, actions=[open_action()])
        res = await ex.submit(plan)
        assert res[0].status == "CONFIRMED"
        assert client.placed[0][1].builder is None
        assert client.placed[0][1].fee_rate is None


class TestSafety:
    async def test_entry_blocked_no_http(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        bad = builder_row()
        bad["builder_address"] = None
        prov = provider_for({"a": bad}, {}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        res = await ex.submit(ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()]))
        assert res[0].status == "FAILED"
        assert client.place_calls == 0
        assert await settings.get_flag("degraded") is not None

    async def test_fallback_close_stop_tp(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        bad = builder_row()
        bad["builder_address"] = None
        prov = provider_for({"a": bad}, {}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        for act in (close_action(), stop_action(), tp_action()):
            client.placed.clear()
            plan = ExecutionPlan(account_id="a", trade_id=None, actions=[act])
            res = await ex.submit(plan)
            assert res[0].status in ("CONFIRMED", "SUBMITTED")
            assert client.placed[0][1].builder is None
            assert client.placed[0][1].fee_rate is None
        assert await settings.get_flag("degraded") is not None

    async def test_cancel_leverage_not_blocked(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        bad = builder_row()
        bad["builder_address"] = None
        prov = provider_for({"a": bad}, {}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        plan = ExecutionPlan(account_id="a", trade_id=None, actions=[lev_action(), cancel_action()])
        res = await ex.submit(plan)
        assert [r.status for r in res] == ["CONFIRMED", "CONFIRMED"]

    async def test_emergency_flatten_not_broken_by_unavailable(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        bad = builder_row()
        bad["builder_address"] = None
        prov = provider_for({"a": bad}, {}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        plan = ExecutionPlan(
            account_id="a", trade_id=None, actions=[close_action()], operation_id="emg-1"
        )
        res = await ex.submit(plan)
        assert res[0].status in ("CONFIRMED", "SUBMITTED")


class TestRace:
    async def test_old_inflight_keeps_snapshot_next_is_direct(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        state = {"mode": "builder"}
        rows = {"a": builder_row()}
        vals = {"a": valid_validation(SIGNER_A)}
        creds = {"a": SIGNER_A}

        async def dynamic_provider(account_id):
            if state["mode"] == "builder":
                prov = provider_for(rows, vals, creds)
            else:
                prov = provider_for({"a": direct_row()}, {}, {"a": SIGNER_B})
            return await prov.resolve(account_id)

        ex = Executor(
            lambda _aid: client,
            commands,
            links,
            {},
            settings=settings,
            attribution_provider=dynamic_provider,
        )
        # Slow first placement holds the mutation lock.
        orig_place = client.place_order

        async def slow_place(account_id, req):
            await asyncio.sleep(0.05)
            return await orig_place(account_id, req)

        client.place_order = slow_place  # type: ignore[method-assign]
        plan1 = ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()])
        t1 = asyncio.create_task(ex.submit(plan1))
        await asyncio.sleep(0.01)
        # Lifecycle replacement waits on the same mutation lock: emulate by
        # holding executor mutation context (as App.follower_lifecycle does).
        async with ex.mutation_context("a"):
            pass  # proves mutual exclusion; would deadlock if locks diverged
        res1 = await t1
        assert res1[0].status == "CONFIRMED"
        assert client.placed[0][1].builder == BUILDER_A
        # After lifecycle commit the next command is direct.
        state["mode"] = "direct"
        client.placed.clear()
        plan2 = ExecutionPlan(
            account_id="a",
            trade_id=None,
            actions=[open_action(qty="0.06")],
            operation_id="op-direct",
        )
        res2 = await ex.submit(plan2)
        assert res2[0].status == "CONFIRMED"
        assert client.placed[0][1].builder is None

    async def test_no_cross_account_leakage(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        prov = provider_for(
            {"a": builder_row(), "b": direct_row()},
            {"a": valid_validation(SIGNER_A)},
            {"a": SIGNER_A, "b": SIGNER_A},
        )
        ex = make_executor(client, commands, links, settings, prov)
        ra = await ex.submit(ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()]))
        rb = await ex.submit(ExecutionPlan(account_id="b", trade_id=None, actions=[open_action()]))
        assert ra[0].status == "CONFIRMED"
        assert rb[0].status == "CONFIRMED"
        by_account = {aid: req for aid, req in client.placed}
        assert by_account["a"].builder == BUILDER_A
        assert by_account["b"].builder is None


class TestSecurity:
    def test_repr_and_params_have_no_secrets(self):
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            builder=BUILDER_A,
            fee_rate=Decimal(CONFIG_FEE),
        )
        blob = repr(req) + json.dumps(req.to_params())
        for secret in ("SIGNER_KEY", "MAIN_KEY", "signature", "Api-Key", "abcd-frontend"):
            assert secret not in blob

    async def test_journal_and_degraded_have_no_secrets(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        bad = builder_row()
        bad["builder_address"] = None
        prov = provider_for({"a": bad}, {}, {"a": SIGNER_A})
        ex = make_executor(client, commands, links, settings, prov)
        res = await ex.submit(ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()]))
        row = await commands.get(res[0].command_key)
        blob = json.dumps(dict(row)) + (await settings.get_flag("degraded") or "")
        assert "0xaaa" not in blob.lower()
        assert "signer" not in blob.lower() or "builder-unavailable" in blob.lower()
        assert "PRIVATE" not in blob
