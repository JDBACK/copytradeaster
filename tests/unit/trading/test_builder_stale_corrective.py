"""Stage 3 Part 2 corrective: stale Builder attribution must not block risk reduction."""

from __future__ import annotations

import json
from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest

from copytradeaster.aster.trading_api import AsterError
from copytradeaster.domain.models import ActionKind, ExecutionAction, ExecutionPlan
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    CommandRepo,
    OrderLinkRepo,
    SettingsRepo,
)
from copytradeaster.trading.builder_attribution import (
    BuilderAttributionProvider,
    is_builder_attribution_rejection,
)
from copytradeaster.trading.executor import Executor, command_key_of

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

    async def clear_builder_snapshot(self, account_id):
        row = self._rows.get(account_id)
        if row is None:
            return
        row["builder_address"] = None
        row["builder_name"] = None
        row["builder_max_fee_rate"] = None
        row["builder_agent_expires_at_ms"] = None
        row["builder_verified_at"] = None


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


def provider_for(rows, vals, creds):
    accounts = rows if isinstance(rows, FakeAccounts) else FakeAccounts(rows)
    return BuilderAttributionProvider(
        accounts=accounts,
        validations=FakeValidations(vals),
        credentials=FakeCreds(creds),
        builder_address=BUILDER_A,
        builder_name="copytradeaster",
        max_fee_rate=CONFIG_FEE,
        clock_ms=lambda: NOW_MS,
    )


def close_action(priority=1):
    return ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.05"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=priority,
    )


def open_action(priority=1):
    return ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.05"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=priority,
    )


def stop_action(priority=1):
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
        priority=priority,
    )


def tp_action(priority=1):
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
        priority=priority,
    )


class FakeClient:
    def __init__(self):
        self.placed = []
        self.place_calls = 0
        self.script = None
        self.query_found = False
        self.query_error = None
        self.history_error = None

    async def place_order(self, account_id, req):
        self.place_calls += 1
        self.placed.append((account_id, req))
        if self.script is not None:
            return await self.script(account_id, req, self.place_calls)

        class _O:
            status = "FILLED"

        return _O()

    async def cancel_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        class _O:
            status = "CANCELED"
            client_order_id = orig_client_order_id or ""

        return _O()

    async def set_leverage(self, account_id, symbol, leverage):
        return None

    async def open_orders(self, account_id, symbol=None):
        return []

    async def position_risk(self, account_id, symbol=None):
        return []

    async def query_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        if self.query_error is not None:
            raise self.query_error
        if self.query_found:

            class _O:
                status = "FILLED"

            return _O()
        for _, req in self.placed:
            if req.new_client_order_id == orig_client_order_id:

                class _O2:
                    status = "NEW"

                return _O2()
        return None

    async def query_order_history(self, account_id, symbol, *, orig_client_order_id):
        if self.history_error is not None:
            raise self.history_error
        return await self.query_order(account_id, symbol, orig_client_order_id=orig_client_order_id)


@pytest.fixture
async def exec_env(tmp_path: Path):
    db = Database(tmp_path / "c.sqlite3")
    await db.connect()
    await db.migrate()
    commands = CommandRepo(db)
    links = OrderLinkRepo(db)
    settings = SettingsRepo(db)
    yield db, commands, links, settings
    await db.close()


def make_executor(client, commands, links, settings, accounts_obj, vals, creds):
    prov = provider_for(accounts_obj, vals, creds)

    def clients(_aid):
        return client

    ex = Executor(
        clients,
        commands,
        links,
        {},
        settings=settings,
        attribution_provider=prov,
        builder_invalidator=accounts_obj.clear_builder_snapshot,
    )
    return ex, prov


class TestPredicate:
    def test_only_5026_is_builder_rejection(self):
        assert (
            is_builder_attribution_rejection(AsterError(-5026, "Fee rate exceeds maxFeeRate"))
            is True
        )
        assert is_builder_attribution_rejection(AsterError(-1000, "unknown")) is False
        assert is_builder_attribution_rejection(AsterError(-1006, "timeout")) is False
        assert is_builder_attribution_rejection(AsterError(-1111, "filter")) is False
        assert is_builder_attribution_rejection(AsterError(-2019, "margin")) is False
        assert is_builder_attribution_rejection(ValueError("x")) is False


class TestCloseFallback:
    async def test_close_5026_then_direct(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            if n == 1:
                assert req.builder == BUILDER_A
                assert req.fee_rate == Decimal(CONFIG_FEE)
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
            assert req.builder is None
            assert req.fee_rate is None

            class _O:
                status = "FILLED"

            return _O()

        client.script = script
        ex, prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        plan = ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()])
        key = command_key_of(plan, plan.actions[0])
        res = await ex.submit(plan)
        assert res[0].status == "CONFIRMED"
        assert res[0].command_key == key
        assert client.place_calls == 2
        first_req = client.placed[0][1]
        second_req = client.placed[1][1]
        assert first_req.builder == BUILDER_A
        assert second_req.builder is None
        assert first_req.new_client_order_id != second_req.new_client_order_id
        row = await commands.get(key)
        assert int(row["attempts"]) == 2
        payload = json.loads(row["payload"])
        assert "builder" not in payload
        assert "feeRate" not in payload
        assert "fee_rate" not in payload
        blob = json.dumps(dict(row))
        assert "signature" not in blob.lower()
        assert "private" not in blob.lower()
        degraded = await settings.get_flag("degraded")
        assert degraded is not None
        assert "builder-rejected" in degraded
        assert accounts._rows["a"]["identity_source"] == "builder"
        assert accounts._rows["a"]["builder_address"] is None
        nxt = await prov.resolve("a")
        assert nxt.kind.value == "BUILDER_UNAVAILABLE"

    async def test_non_builder_error_no_fallback_no_invalidation(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            assert req.builder == BUILDER_A
            raise AsterError(-1111, "filter reject")

        client.script = script
        ex, prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        res = await ex.submit(
            ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()])
        )
        assert res[0].status == "FAILED"
        assert client.place_calls == 1
        assert accounts._rows["a"]["builder_address"] == BUILDER_A
        nxt = await prov.resolve("a")
        assert nxt.kind.value == "BUILDER_VALID"


class TestEntryFailClosed:
    async def test_entry_5026_no_direct_and_next_blocked(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            assert req.builder == BUILDER_A
            raise AsterError(-5026, "Fee rate exceeds maxFeeRate")

        client.script = script
        ex, prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        plan = ExecutionPlan(account_id="a", trade_id=None, actions=[open_action()])
        res = await ex.submit(plan)
        assert res[0].status == "FAILED"
        assert client.place_calls == 1
        assert accounts._rows["a"]["identity_source"] == "builder"
        assert accounts._rows["a"]["builder_address"] is None
        nxt = await prov.resolve("a")
        assert nxt.kind.value == "BUILDER_UNAVAILABLE"
        plan2 = ExecutionPlan(
            account_id="a", trade_id=None, actions=[open_action()], operation_id="op2"
        )
        res2 = await ex.submit(plan2)
        assert res2[0].status == "FAILED"
        assert client.place_calls == 1


class TestStopTpFallback:
    @pytest.mark.parametrize("make", [stop_action, tp_action])
    async def test_stop_tp_5026_then_direct(self, exec_env, make):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            if n == 1:
                assert req.builder == BUILDER_A
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
            assert req.builder is None

            class _O:
                status = "FILLED"

            return _O()

        client.script = script
        ex, prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        res = await ex.submit(ExecutionPlan(account_id="a", trade_id=None, actions=[make()]))
        assert res[0].status in ("CONFIRMED", "SUBMITTED")
        assert client.place_calls == 2
        assert client.placed[0][1].builder == BUILDER_A
        assert client.placed[1][1].builder is None
        assert (await prov.resolve("a")).kind.value == "BUILDER_UNAVAILABLE"


class TestPlanSnapshot:
    async def test_remaining_actions_go_direct_after_first_rejection(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})
        builder_calls = []

        async def script(account_id, req, n):
            if req.builder is not None:
                builder_calls.append(n)
                if len(builder_calls) == 1:
                    raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
                raise AssertionError("stale Builder reused after invalidation")

            class _O:
                status = "FILLED"

            return _O()

        client.script = script
        ex, _prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        plan = ExecutionPlan(
            account_id="a",
            trade_id=None,
            actions=[close_action(), stop_action(priority=2), tp_action(priority=3)],
        )
        res = await ex.submit(plan)
        assert [r.status for r in res] == ["CONFIRMED", "SUBMITTED", "SUBMITTED"]
        assert len(builder_calls) == 1
        assert client.place_calls == 4
        for _, req in client.placed[1:]:
            assert req.builder is None
            assert req.fee_rate is None

    async def test_entry_late_in_same_plan_stays_blocked(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            if req.builder is not None:
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")

            class _O:
                status = "FILLED"

            return _O()

        client.script = script
        ex, _prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        plan = ExecutionPlan(
            account_id="a",
            trade_id=None,
            actions=[close_action(priority=1), open_action(priority=2)],
        )
        res = await ex.submit(plan)
        assert res[0].status == "CONFIRMED"
        assert res[1].status == "FAILED"
        assert client.place_calls == 2


class TestUnknownAfterDirect:
    async def test_direct_unknown_query_before_retry(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        client.query_found = True
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            if n == 1:
                assert req.builder == BUILDER_A
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
            assert req.builder is None
            raise AsterError(-1007, "transport timeout")

        client.script = script
        ex, _prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )
        res = await ex.submit(
            ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()])
        )
        assert res[0].status == "CONFIRMED"
        assert client.place_calls == 2
        row = await commands.get(res[0].command_key)
        assert "resolved-unknown" in (row["last_error"] or "")

    async def test_direct_unknown_unresolved_never_gets_second_direct_retry(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        client.query_error = AsterError(-1006, "current query unavailable")
        client.history_error = AsterError(-1006, "history unavailable")
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            del account_id
            if n == 1:
                assert req.builder == BUILDER_A
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
            assert req.builder is None
            raise AsterError(-1007, "transport timeout")

        client.script = script
        ex, _prov = make_executor(
            client, commands, links, settings, accounts, {"a": valid_validation()}, {"a": SIGNER_A}
        )

        result = await ex.submit(
            ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()])
        )

        assert result[0].status == "UNKNOWN"
        assert client.place_calls == 2
        assert [req.builder for _, req in client.placed] == [BUILDER_A, None]


class TestIsolation:
    async def test_other_accounts_untouched(self, exec_env):
        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row(), "b": builder_row(), "c": direct_row()})
        vals = {"a": valid_validation(SIGNER_A), "b": valid_validation(SIGNER_B)}
        creds = {"a": SIGNER_A, "b": SIGNER_B, "c": SIGNER_A}

        async def script(account_id, req, n):
            if account_id == "a" and req.builder is not None:
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")

            class _O:
                status = "FILLED"

            return _O()

        client.script = script
        prov = provider_for(accounts, vals, creds)

        def clients(_aid):
            return client

        ex = Executor(
            clients,
            commands,
            links,
            {},
            settings=settings,
            attribution_provider=prov,
            builder_invalidator=accounts.clear_builder_snapshot,
        )
        ra = await ex.submit(ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()]))
        assert ra[0].status == "CONFIRMED"
        assert accounts._rows["a"]["builder_address"] is None
        assert accounts._rows["b"]["builder_address"] == BUILDER_A
        rb = await ex.submit(
            ExecutionPlan(
                account_id="b", trade_id=None, actions=[close_action()], operation_id="op-b"
            )
        )
        assert rb[0].status == "CONFIRMED"
        assert client.placed[-1][1].builder == BUILDER_A
        rc = await ex.submit(
            ExecutionPlan(
                account_id="c", trade_id=None, actions=[close_action()], operation_id="op-c"
            )
        )
        assert rc[0].status == "CONFIRMED"
        assert client.placed[-1][1].builder is None


class TestRestartDurable:
    async def test_new_provider_after_invalidation_is_unavailable(self, tmp_path: Path):
        db = Database(tmp_path / "r.sqlite3")
        await db.connect()
        await db.migrate()
        try:
            accounts = AccountRepo(db)
            validations = AgentValidationRepo(db)
            await accounts.upsert(
                "a", "follower", "lbl", "0xuser000000000000000000000000000000000001"
            )
            await accounts.set_builder_snapshot(
                "a",
                builder_address=BUILDER_A,
                builder_name="copytradeaster",
                builder_max_fee_rate=CONFIG_FEE,
                builder_agent_expires_at_ms=FUTURE_MS,
                builder_verified_at="2026-01-01T00:00:00+00:00",
            )
            await validations.upsert(
                "a",
                validation=AgentValidation(
                    signer_address=SIGNER_A,
                    can_read=True,
                    can_perp_trade=True,
                    can_withdraw=False,
                    expired_ms=FUTURE_MS,
                    ip_whitelist="0.0.0.0/0",
                    status="VALID",
                    checked_at="2026-01-01T00:00:00+00:00",
                ),
            )

            class _Creds:
                def identity(self, _aid):
                    class _I:
                        signer = SIGNER_A

                    return _I()

            def make_provider():
                return BuilderAttributionProvider(
                    accounts=accounts,
                    validations=validations,
                    credentials=_Creds(),
                    builder_address=BUILDER_A,
                    builder_name="copytradeaster",
                    max_fee_rate=CONFIG_FEE,
                    clock_ms=lambda: NOW_MS,
                )

            assert (await make_provider().resolve("a")).kind.value == "BUILDER_VALID"
            commands = CommandRepo(db)
            links = OrderLinkRepo(db)
            settings = SettingsRepo(db)
            client = FakeClient()

            async def script(account_id, req, n):
                if n == 1:
                    raise AsterError(-5026, "Fee rate exceeds maxFeeRate")

                class _O:
                    status = "FILLED"

                return _O()

            client.script = script

            def clients(_aid):
                return client

            ex = Executor(
                clients,
                commands,
                links,
                {},
                settings=settings,
                attribution_provider=make_provider(),
                builder_invalidator=accounts.clear_builder_snapshot,
            )
            res = await ex.submit(
                ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()])
            )
            assert res[0].status == "CONFIRMED"
            row = await accounts.get("a")
            assert str(row["identity_source"]) == "builder"
            assert row["builder_address"] is None
            fresh = make_provider()
            assert (await fresh.resolve("a")).kind.value == "BUILDER_UNAVAILABLE"
        finally:
            await db.close()


class TestIntegrationProductionComposition:
    async def test_desired_reduction_converges_via_direct(self, tmp_path: Path):
        db = Database(tmp_path / "i.sqlite3")
        await db.connect()
        await db.migrate()
        try:
            accounts = AccountRepo(db)
            validations = AgentValidationRepo(db)
            await accounts.upsert(
                "a", "follower", "lbl", "0xuser000000000000000000000000000000000001"
            )
            await accounts.set_builder_snapshot(
                "a",
                builder_address=BUILDER_A,
                builder_name="copytradeaster",
                builder_max_fee_rate=CONFIG_FEE,
                builder_agent_expires_at_ms=FUTURE_MS,
                builder_verified_at="2026-01-01T00:00:00+00:00",
            )
            await validations.upsert(
                "a",
                validation=AgentValidation(
                    signer_address=SIGNER_A,
                    can_read=True,
                    can_perp_trade=True,
                    can_withdraw=False,
                    expired_ms=FUTURE_MS,
                    ip_whitelist="0.0.0.0/0",
                    status="VALID",
                    checked_at="2026-01-01T00:00:00+00:00",
                ),
            )

            class _Creds:
                def identity(self, _aid):
                    class _I:
                        signer = SIGNER_A

                    return _I()

            def make_provider():
                return BuilderAttributionProvider(
                    accounts=accounts,
                    validations=validations,
                    credentials=_Creds(),
                    builder_address=BUILDER_A,
                    builder_name="copytradeaster",
                    max_fee_rate=CONFIG_FEE,
                    clock_ms=lambda: NOW_MS,
                )

            commands = CommandRepo(db)
            links = OrderLinkRepo(db)
            settings = SettingsRepo(db)
            client = FakeClient()
            seen_builders = []

            async def script(account_id, req, n):
                seen_builders.append(req.builder)
                if n == 1:
                    assert req.builder == BUILDER_A
                    raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
                assert req.builder is None

                class _O:
                    status = "FILLED"

                return _O()

            client.script = script

            def clients(_aid):
                return client

            ex = Executor(
                clients,
                commands,
                links,
                {},
                settings=settings,
                attribution_provider=make_provider(),
                builder_invalidator=accounts.clear_builder_snapshot,
            )
            plan = ExecutionPlan(account_id="a", trade_id=None, actions=[close_action()])
            key = command_key_of(plan, plan.actions[0])
            res = await ex.submit(plan)
            assert res[0].status == "CONFIRMED"
            assert res[0].command_key == key
            assert seen_builders[0] == BUILDER_A
            assert seen_builders[1] is None
            row = await commands.get(key)
            payload = json.loads(row["payload"])
            assert "builder" not in payload and "feeRate" not in payload
            snap = await accounts.get_builder_snapshot("a")
            assert snap["builder_address"] is None
            res2 = await ex.submit(plan)
            assert res2[0].status == "CONFIRMED"
            assert res2[0].command_key == key
        finally:
            await db.close()


class TestEmergencyFallback:
    async def test_emergency_close_uses_same_safety_retry(self, exec_env):
        from contextlib import asynccontextmanager
        from decimal import Decimal as D
        from types import SimpleNamespace

        from copytradeaster.application.control import EmergencyController
        from copytradeaster.domain.models import AccountState, PositionKey, PositionState

        _, commands, links, settings = exec_env
        client = FakeClient()
        accounts = FakeAccounts({"a": builder_row()})

        async def script(account_id, req, n):
            if n == 1:
                assert req.builder == BUILDER_A
                raise AsterError(-5026, "Fee rate exceeds maxFeeRate")
            assert req.builder is None

            class _O:
                status = "FILLED"

            return _O()

        client.script = script
        prov = provider_for(accounts, {"a": valid_validation()}, {"a": SIGNER_A})

        def clients(_aid):
            return client

        ex = Executor(
            clients,
            commands,
            links,
            {},
            settings=settings,
            attribution_provider=prov,
            builder_invalidator=accounts.clear_builder_snapshot,
        )

        class Adapter:
            async def submit_under_mutation_lock(self, plan, *, client):
                return await ex.submit(plan, client=client)

        key = PositionKey("BTCUSDT")
        open_state = AccountState(
            "a",
            D(100),
            {
                key: PositionState(
                    key=key,
                    qty=D("0.05"),
                    entry_price=D(65000),
                    leverage=20,
                    mark_price=D(65000),
                    liquidation_price=D(30000),
                )
            },
            [],
        )
        flat_state = AccountState("a", D(100), {}, [])

        class Lifecycle:
            def __init__(self):
                self.runtime = SimpleNamespace(account_id="a", client=client)
                self.calls = 0

            async def snapshot(self):
                self.calls += 1
                if self.calls <= 2:
                    return open_state if self.calls == 1 else flat_state
                return flat_state

        holder = {}

        @asynccontextmanager
        async def factory(account_id):
            lc = Lifecycle()
            holder["lc"] = lc
            yield lc

        class State:
            async def pause_account(self, account_id):
                return None

            async def begin_global(self, operation_id):
                return None

            async def record_result(self, report):
                return None

        ctl = EmergencyController(
            lifecycle_factory=factory,
            executor=Adapter(),
            state=State(),
            max_passes=3,
            required_flat_proofs=1,
            pass_delay_s=0,
        )
        report = await ctl.emergency_account("a")
        assert report.success is True
        assert accounts._rows["a"]["builder_address"] is None
        assert client.place_calls >= 2
        assert client.placed[0][1].builder == BUILDER_A
        assert client.placed[1][1].builder is None


class TestTrustAfterInvalidation:
    async def test_service_trust_not_verified_after_clear(self, tmp_path: Path):
        from datetime import datetime
        from types import SimpleNamespace

        from copytradeaster.application.service import UserApplicationService
        from copytradeaster.storage.repositories import TelegramUserRepo

        db = Database(tmp_path / "t.sqlite3")
        await db.connect()
        await db.migrate()
        try:
            accounts = AccountRepo(db)
            await accounts.upsert(
                "a", "follower", "lbl", "0xuser000000000000000000000000000000000001"
            )
            await TelegramUserRepo(db).upsert(7, 700, account_id="a")
            await accounts.set_builder_snapshot(
                "a",
                builder_address=BUILDER_A,
                builder_name="copytradeaster",
                builder_max_fee_rate=CONFIG_FEE,
                builder_agent_expires_at_ms=FUTURE_MS,
                builder_verified_at=datetime.now(UTC).isoformat(),
            )
            await AgentValidationRepo(db).upsert(
                "a",
                validation=AgentValidation(
                    signer_address=SIGNER_A,
                    can_read=True,
                    can_perp_trade=True,
                    can_withdraw=False,
                    expired_ms=FUTURE_MS,
                    ip_whitelist="0.0.0.0/0",
                    status="VALID",
                    checked_at=datetime.now(UTC).isoformat(),
                ),
            )
            cfg = SimpleNamespace(
                builder_address=BUILDER_A, builder_name="copytradeaster", max_fee_rate=CONFIG_FEE
            )

            class Creds:
                def identity(self, _aid):
                    return SimpleNamespace(signer=SIGNER_A)

            runtime = SimpleNamespace(db=db, cfg=cfg, credentials=Creds())
            svc = UserApplicationService(runtime, admin_ids=[1])
            assert await svc._builder_verified("a") is True
            await accounts.clear_builder_snapshot("a")
            row = await accounts.get("a")
            assert str(row["identity_source"]) == "builder"
            assert await svc._builder_verified("a") is False
            dto = await svc.account_builder(7, refresh=False)
            assert dto.identity_source == "builder"
            assert dto.builder_address_masked is None
        finally:
            await db.close()
