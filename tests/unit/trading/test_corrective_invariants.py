"""Cross-component regressions from the independent pre-mainnet review."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    PositionKey,
    PositionState,
    PosSide,
    TargetPosition,
    TargetState,
)
from copytradeaster.master.projector import MasterState
from copytradeaster.risk.account_budget import reserve_account_budget
from copytradeaster.trading.coordinator import FollowerRuntime
from copytradeaster.trading.executor import command_key_of
from copytradeaster.trading.planner import build_plan
from copytradeaster.trading.registry import FollowerRegistry
from tests.unit.trading import test_executor_journal as journal_tests
from tests.unit.trading import test_reconciler_idempotent as recon_tests
from tests.unit.trading.test_coordinator_routing import filters_provider
from tests.unit.trading.test_executor_journal import (
    SimplePosition,
    leverage_action,
    open_action,
    plan_of,
    stop_action,
)

journal_env = journal_tests.env
recon_env = recon_tests.env


def position(key, qty="1", *, entry="100", mark="100", liq="0"):
    return PositionState(
        key=key,
        qty=D(qty),
        entry_price=D(entry),
        mark_price=D(mark),
        leverage=20,
        liquidation_price=D(liq),
    )


def target(key, qty="1", *, stop="90", lev=20):
    return TargetPosition(key, D(qty), D(stop), None, lev)


async def test_changed_target_cannot_bypass_unknown_and_other_account_can_trade(journal_env):
    executor, client, commands, _, _ = journal_env
    old = plan_of(open_action())
    key = command_key_of(old, old.actions[0])
    await commands.insert(
        key, old.account_id, None, "MARKET_OPEN", json.dumps(old.actions[0].payload), 1, "ct-old"
    )
    await commands.set_status(key, "UNKNOWN")
    blocked = await executor.submit(plan_of(open_action(qty="0.06")))
    assert blocked[0].status == "BLOCKED" and client.place_calls == 0
    # Both another symbol and another revision must retain account-wide risk reservations.
    eth = replace(
        open_action(), symbol="ETHUSDT", payload={**open_action().payload, "symbol": "ETHUSDT"}
    )
    assert (await executor.submit(plan_of(eth)))[0].status == "BLOCKED"
    other = replace(plan_of(open_action()), account_id="copyc2")
    assert (await executor.submit(other))[0].status == "CONFIRMED"
    assert client.place_calls == 1
    # Protective work remains admitted.
    await executor.submit(plan_of(stop_action()))
    assert client.place_calls == 2


async def test_confirmed_leverage_requires_current_exchange_proof(journal_env):
    executor, client, _, _, _ = journal_env
    plan = plan_of(leverage_action())
    assert (await executor.submit(plan))[0].status == "CONFIRMED"
    client.positions_result = [SimplePosition(200)]
    assert (await executor.submit(plan))[0].status == "CONFIRMED"
    assert client.leverage_calls == [("BTCUSDT", 20), ("BTCUSDT", 20)]
    client.positions_result = [SimplePosition(20)]
    await executor.submit(plan)
    assert len(client.leverage_calls) == 2


async def test_hedge_has_one_shared_leverage_dependency(journal_env):
    executor, client, _, _, _ = journal_env
    keys = [PositionKey("BTCUSDT", side) for side in (PosSide.LONG, PosSide.SHORT)]
    actual = AccountState("copyc1", D(1000), {}, symbol_leverages={"BTCUSDT": 200})
    targets = [
        target(keys[0], "0.01", stop="64000", lev=28),
        target(keys[1], "-0.01", stop="66000", lev=50),
    ]
    plan = build_plan(
        actual, TargetState("copyc1", targets), filters_provider(), lambda k, s, n: f"ct-{n}"
    )
    settings = [a for a in plan.actions if a.kind == ActionKind.SET_LEVERAGE]
    assert len(settings) == 1 and settings[0].payload["leverage"] == "28"
    from copytradeaster.aster.trading_api import AsterError

    async def rejected(*args):
        raise AsterError(-4028, "rejected")

    client.set_leverage = rejected
    results = await executor.submit(plan)
    assert results[0].status == "FAILED"
    assert all(r.status == "BLOCKED" for r in results[1:])
    assert client.place_calls == 0


async def test_registry_concurrent_last_slot_and_shadow_exclusion():
    registry = FollowerRegistry(max_real_followers=1)
    outcomes = await asyncio.gather(
        *(registry.publish_new(FollowerRuntime(aid, object(), None)) for aid in ("a", "b")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in outcomes) == 1
    assert len(registry) == 1
    await registry.publish_new(FollowerRuntime("shadow", object(), None, execution_mode="SHADOW"))
    assert len(registry) == 2


async def test_liquidation_between_entry_and_current_mark_is_not_discarded(recon_env):
    reconciler, _, _ = recon_env
    coordinator = reconciler._coordinator
    long = position(PositionKey("BTCUSDT", PosSide.LONG), entry="80000", mark="82000", liq="81700")
    assert coordinator._liquidation_violated(long, D(81500))
    short = position(
        PositionKey("BTCUSDT", PosSide.SHORT), "-1", entry="82000", mark="80000", liq="80300"
    )
    assert coordinator._liquidation_violated(short, D(80500))
    # Common cross-HEDGE quote above the mark belongs to the adverse SHORT direction.
    long.liquidation_price = D("25500000")
    assert not coordinator._liquidation_violated(long, D(81500))


async def test_reconcile_uses_call_result_without_bounded_journal_scan(recon_env):
    reconciler, fr, _ = recon_env

    async def forbidden(*args):
        raise AssertionError("journal counts cannot prove convergence")

    reconciler._coordinator.commands.recent = forbidden
    report = await reconciler.reconcile(MasterState(D(1000), {}))
    assert report.per_account == {fr.account_id: 0}


async def test_parallel_reconcile_scopes_are_isolated(recon_env):
    reconciler, fr, _ = recon_env
    coordinator = reconciler._coordinator
    seen = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def recompute(*args, **kwargs):
        scope = coordinator._snapshot_scope.get()
        seen.append(scope)
        if scope == {"BTCUSDT"}:
            entered.set()
            await release.wait()
            assert coordinator._snapshot_scope.get() == {"BTCUSDT"}
        return 0

    coordinator._recompute_follower = recompute
    first = asyncio.create_task(
        reconciler.reconcile(MasterState(D(0), {}), snapshot_scope={"BTCUSDT"})
    )
    await asyncio.wait_for(entered.wait(), 1)
    await reconciler.reconcile(MasterState(D(0), {}), snapshot_scope={"ETHUSDT"})
    release.set()
    await asyncio.wait_for(first, 1)
    assert seen == [{"BTCUSDT"}, {"ETHUSDT"}]
    assert coordinator._snapshot_scope.get() is None


@pytest.mark.parametrize("sides", [(PosSide.BOTH, PosSide.BOTH), (PosSide.LONG, PosSide.SHORT)])
def test_account_budget_sums_symbols_and_hedge_legs(sides):
    keys = [
        PositionKey("BTCUSDT", sides[0]),
        PositionKey("ETHUSDT" if sides[0] == PosSide.BOTH else "BTCUSDT", sides[1]),
    ]
    filters = {
        symbol: replace(f, min_notional=D(0), min_qty=D("0.001"))
        for symbol, f in filters_provider().items()
    }
    actual = AccountState("a", D(1000), {}, equity_usdt=D(1000))
    targets = [target(keys[0], "10"), target(keys[1], "-10", stop="110")]
    master = {key: position(key) for key in keys}
    bounded = reserve_account_budget(
        actual, targets, master, filters, max_total_risk_pct=D("0.1"), fee_buffer_pct=D("0.001")
    )
    assert sum(abs(t.qty) * D("10.1") for t in bounded) <= D(100)
    assert sum(abs(t.qty) for t in bounded) > 0


def test_pending_close_and_missing_stop_do_not_release_budget():
    btc, eth = PositionKey("BTCUSDT"), PositionKey("ETHUSDT")
    actual = AccountState("a", D(1000), {btc: position(btc)}, equity_usdt=D(1000))
    wanted = [target(btc, "0"), target(eth)]
    bounded = reserve_account_budget(
        actual,
        wanted,
        {eth: position(eth)},
        filters_provider(),
        max_total_risk_pct=D("0.1"),
        fee_buffer_pct=D("0.001"),
    )
    assert [t.qty for t in bounded] == [D(0), D(0)]


async def test_worker_observes_lower_leverage_after_shared_mutation_barrier(journal_env):
    from tests.unit.trading.test_leverage_manager import FakeLevClient, make_manager

    executor, _, _, _, _ = journal_env
    client = FakeLevClient(risk_leverages=[200])
    clients = {"main": FakeLevClient(), "copyc1": client}
    manager = make_manager(clients, symbols=["BTCUSDT"], mutation_context=executor.mutation_context)
    await manager.refresh_catalog()
    async with executor.mutation_context("copyc1"):
        task = asyncio.create_task(manager.prepare_symbol("copyc1", "BTCUSDT"))
        await asyncio.sleep(0)
        assert not task.done()
        # An entry's conservative stop ceiling was installed before worker admission.
        client.risk_leverages = [44]
    assert await asyncio.wait_for(task, 1) == 44
    assert manager.is_ready("copyc1", "BTCUSDT")
    assert client.set_calls == []
    restarted = make_manager(clients, symbols=["BTCUSDT"])
    await restarted.refresh_catalog()
    assert await restarted.prepare_symbol("copyc1", "BTCUSDT") == 44
    assert client.set_calls == []


async def test_stale_leverage_confirmation_with_empty_reads_reprovisions(journal_env):
    """Row-omitting reads (shadow doubles skip flat legs) leave nothing to
    verify a historical confirmation against. The safe answer is a fresh
    idempotent re-provision — never blind reuse, never a durable UNKNOWN
    that bricks the account behind the in-flight gate (load incident).
    Genuinely unreadable reads (transport errors) stay fail-closed; see
    test_boot_unreadable_leverage_stays_fail_closed."""
    executor, client, commands, _, _ = journal_env
    plan = plan_of(leverage_action())
    first = (await executor.submit(plan))[0]
    assert first.status == "CONFIRMED"
    client.positions_result = []  # no authoritative configured value
    assert (await executor.submit(plan))[0].status == "CONFIRMED"
    assert len(client.leverage_calls) == 2
    assert (await commands.get(first.command_key))["status"] == "CONFIRMED"
    assert (await executor.submit(plan_of(open_action())))[0].status == "CONFIRMED"
    assert client.place_calls == 1


def test_account_margin_is_reserved_once_for_all_increments():
    keys = [PositionKey("BTCUSDT"), PositionKey("ETHUSDT")]
    actual = AccountState("a", D(100), {}, equity_usdt=D(10000))
    targets = [target(key, "10", lev=2) for key in keys]
    master = {key: position(key) for key in keys}
    bounded = reserve_account_budget(
        actual,
        targets,
        master,
        filters_provider(),
        max_total_risk_pct=D("0.1"),
        fee_buffer_pct=D("0.001"),
    )
    assert sum(abs(t.qty) * D(100) / 2 for t in bounded) <= D(80)
    assert sum(abs(t.qty) for t in bounded) > 0


async def test_health_requires_authoritative_current_proof_not_historical_failures(journal_env):
    from types import SimpleNamespace

    from copytradeaster.app import App
    from copytradeaster.trading.reconciler import ReconcileReport

    _, _, commands, _, _ = journal_env
    app = App.__new__(App)
    app.commands = commands
    app._ready = True
    app.projector = SimpleNamespace(dirty_symbols=set())
    app.follower_registry = FollowerRegistry()
    app.cfg = SimpleNamespace(max_followers=10, symbol_whitelist=[])
    app.leverage = SimpleNamespace(pending_count=lambda: 0)
    action = open_action()
    await commands.insert(
        "historical", "gone", None, action.kind.value, json.dumps(action.payload), 1, "ct-past"
    )
    await commands.set_status("historical", "FAILED")
    assert await app._sweep_hold_reason(ReconcileReport()) is None
    assert await app._sweep_hold_reason(ReconcileReport(authoritative=False)) == "scoped-snapshot"
    assert (await app._sweep_hold_reason(ReconcileReport(per_account={"a": 1}))).startswith(
        "unconverged:"
    )
    await commands.set_status("historical", "UNKNOWN")
    assert (await app._sweep_hold_reason(ReconcileReport())).startswith("in-flight:")


async def test_load_harness_does_not_retry_from_partial_position_progress(journal_env, monkeypatch):
    from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
    from copytradeaster.domain.models import Side
    from tests.testnet.test_pre_mainnet_load_live import _submit_master_order
    from tests.unit.trading.test_executor_journal import SimpleMarketPosition

    _, client, _, _, _ = journal_env
    client.history_error = AsterError(-1006, "history unavailable")

    async def partial(*args):
        client.place_calls += 1
        client.positions_result = [SimpleMarketPosition("0.001")]
        raise AsterError(-1000, "lost response", http_status=503)

    client.place_order = partial
    monkeypatch.setattr("copytradeaster.trading.executor.RECOVERY_QUERY_BACKOFF_S", 0)
    stats = {
        key: 0
        for key in ("unknown_outcomes", "retries", "resolved_by_position", "resolved_by_query")
    }
    request = PlaceOrderRequest(
        "BTCUSDT", Side.BUY, "MARKET", quantity=D("0.002"), new_client_order_id="ct-unit"
    )
    with pytest.raises(RuntimeError, match="master outcome unresolved"):
        await _submit_master_order(client, request, stats)
    assert client.place_calls == 1 and stats["retries"] == 0
