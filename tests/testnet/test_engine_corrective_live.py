"""Corrective qualification over signed Testnet state, never a mainnet run.

The production App boots normally. During deterministic fault windows only
its automatic follower generations and maintenance tasks are stopped; real
master WS ingestion, REST snapshots, sizing, planner, executor, journal and
signed orders remain in use. Injected failures are identified explicitly.
Every admitted campaign owns initially-flat accounts and audits cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.app import App
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.domain.models import (
    ActionKind,
    PosSide,
    RiskPolicy,
    Side,
    TargetState,
)
from copytradeaster.master.projector import MasterDelta
from copytradeaster.trading.planner import build_plan
from tests.testnet.test_pre_mainnet_hedge_live import _prepare_scratch

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.getenv("CT_TESTNET") != "1", reason="signed Testnet requires CT_TESTNET=1"
    ),
]
SYMBOLS = ("BTCUSDT", "ETHUSDT")


@dataclass
class Campaign:
    app: App
    http: httpx.AsyncClient
    followers: list[str]
    output: Path
    evidence: dict = field(default_factory=dict)

    @property
    def audit(self) -> AsterClient:
        return AsterClient(
            self.http,
            self.app.cfg.rest_base_url,
            self.app.limiter,
            self.app.credentials,
            self.app.credentials,
            self.app.nonces,
        )

    async def quiesce(self) -> None:
        await self.app.coordinator.pause_barrier()
        for name in ("_sweep_task", "_master_recovery_task", "_leverage_task"):
            task = getattr(self.app, name, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self.app, name, None)
        assert self.app._stream_connected

    async def refresh(self):
        await self.app.projector.rebuild_full(self.audit, "main")
        self.app._entry_admission_ready = True
        return self.app.projector.state

    async def recompute(self, runtime=None, *, state=None) -> int:
        state = state if state is not None else await self.refresh()
        runtime = runtime or self.app.follower_registry.get(self.followers[0])
        assert runtime is not None
        return await self.app.coordinator._recompute_follower(
            runtime, MasterDelta(frozenset({"*"}), state), master_fresh=True
        )

    async def snapshot(self):
        runtime = self.app.follower_registry.get(self.followers[0])
        assert runtime is not None
        return await self.app.coordinator.snapshot_follower(runtime)

    async def converge(self) -> None:
        zeros = 0
        for _ in range(20):
            count = await self.recompute()
            zeros = zeros + 1 if count == 0 else 0
            if zeros == 3:
                return
            await asyncio.sleep(1)
        raise AssertionError("three authoritative zero-action evaluations were not reached")

    async def open_master(self, symbol: str, leg: PosSide, distance_pct: D) -> None:
        filters = self.app._filters_cache[symbol]
        # Small real master notional; follower quantity is independently risk-sized.
        qty = D("0.002") if symbol == "BTCUSDT" else D("0.02")
        qty = max(filters.min_qty, qty)
        stamp = str(time.time_ns())
        await self.audit.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.BUY if leg == PosSide.LONG else Side.SELL,
                type="MARKET",
                quantity=qty,
                position_side=leg,
                new_client_order_id=f"corrective-open-{stamp}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        position = next(
            p
            for p in await self.audit.position_risk("main", symbol)
            if p.position_side == leg and p.position_amt != 0
        )
        level = position.mark_price * (
            D(1) - distance_pct if leg == PosSide.LONG else D(1) + distance_pct
        )
        rounding = ROUND_FLOOR if leg == PosSide.LONG else ROUND_CEILING
        level = (level / filters.tick_size).to_integral_value(rounding=rounding) * filters.tick_size
        await self.audit.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.SELL if leg == PosSide.LONG else Side.BUY,
                type="STOP_MARKET",
                stop_price=level,
                close_position=True,
                position_side=leg,
                working_type="MARK_PRICE",
                new_client_order_id=f"corrective-stop-{stamp}"[:36],
            ),
        )

    def save(self) -> None:
        self.output.write_text(json.dumps(self.evidence, indent=2, default=str) + "\n")


async def _flat_proof(client, accounts: list[str]) -> dict:
    proof = {}
    for aid in accounts:
        positions = [p for p in await client.position_risk(aid) if p.position_amt != 0]
        orders = await client.open_orders(aid)
        proof[aid] = {"positions": len(positions), "orders": len(orders)}
        assert not positions and not orders, (
            f"{aid}: expected initially/finally flat, got {proof[aid]}"
        )
    return proof


async def _cleanup(case: Campaign, accounts: list[str]) -> None:
    await case.app.stop()
    await case.app.db.connect()
    # Fresh reads before each attempt; never blindly resend an old close quantity.
    for attempt in range(3):
        for aid in accounts:
            for order in await case.audit.open_orders(aid):
                with contextlib.suppress(AsterError):
                    await case.audit.cancel_order(
                        aid, order.symbol, orig_client_order_id=order.client_order_id
                    )
            for pos in await case.audit.position_risk(aid):
                if pos.position_amt == 0:
                    continue
                with contextlib.suppress(AsterError):
                    await case.audit.place_order(
                        aid,
                        PlaceOrderRequest(
                            symbol=pos.symbol,
                            side=Side.SELL if pos.position_amt > 0 else Side.BUY,
                            type="MARKET",
                            quantity=abs(pos.position_amt),
                            position_side=pos.position_side,
                            reduce_only=pos.position_side == PosSide.BOTH,
                            new_client_order_id=f"corrective-flat-{time.time_ns()}"[:36],
                            new_order_resp_type="RESULT",
                        ),
                    )
        try:
            case.evidence["final_flat"] = await _flat_proof(case.audit, accounts)
            await asyncio.sleep(1)
            case.evidence["final_flat_second"] = await _flat_proof(case.audit, accounts)
            return
        except AssertionError:
            if attempt == 2:
                raise
            await asyncio.sleep(1)


@pytest.fixture
async def campaign(tmp_path):
    settings, followers = await _prepare_scratch(tmp_path, "engine-corrective", max_followers=2)
    settings = settings.model_copy(
        update={
            "symbol_whitelist": list(SYMBOLS),
            "follower_snapshot_symbols": [],
            "operator_max_leverage": 20,
            "max_total_risk_pct": D("0.04"),
        }
    )
    settings.network_profile.require_testnet()
    http = httpx.AsyncClient(timeout=15)
    case = Campaign(App(settings), http, followers, tmp_path / "evidence.json")
    accounts = ["main", *followers]
    owned = False
    original_leverage = {}
    try:
        await case.app.db.connect()
        await case.app.credentials.load()
        case.evidence["initial_flat"] = await _flat_proof(case.audit, accounts)
        owned = True
        for aid in followers:
            for symbol in SYMBOLS:
                rows = await case.audit.position_risk(aid, symbol)
                values = {int(p.leverage) for p in rows if int(p.leverage) > 0}
                if len(values) == 1:
                    original_leverage[(aid, symbol)] = values.pop()
            await case.app.settings.upsert_policy(RiskPolicy(D("0.02"), 20, D("0.05")), aid)
        await case.app.start()
        await case.quiesce()
        for aid in followers:
            prepared = await case.app.leverage.prepare_account(aid, SYMBOLS)
            assert not prepared.failed, prepared.failed
        yield case
    finally:
        try:
            if owned:
                await _cleanup(case, accounts)
                for (aid, symbol), leverage in original_leverage.items():
                    await case.audit.set_leverage(aid, symbol, leverage)
            else:
                await case.app.stop()
        finally:
            case.save()
            await http.aclose()
            await case.app.db.close()


async def test_shared_hedge_leverage_rejection_retry_and_worker_restart(campaign):
    """LC1: real asymmetric legs, one failed prerequisite, retry, no worker upshift."""
    c = campaign
    await c.open_master("BTCUSDT", PosSide.LONG, D("0.06"))
    await c.open_master("BTCUSDT", PosSide.SHORT, D("0.01"))
    state = await c.refresh()
    runtime = c.app.follower_registry.get(c.followers[0])
    actual = await c.snapshot()
    policy = await c.app.settings.get_policy(runtime.account_id)
    targets, _ = await c.app.coordinator.targets_for(
        runtime.account_id, state, actual, policy, client=runtime.client
    )
    live_targets = [t for t in targets if t.qty != 0]
    c.evidence["asymmetric_targets"] = [(t.key.pos_side, t.qty, t.leverage) for t in targets]
    independent_ceilings = [
        min(
            20,
            int(
                p.mark_price // (abs(p.mark_price - p.stop.stop_price) + p.mark_price * D("0.015"))
            ),
        )
        for p in state.positions.values()
        if p.qty and p.stop is not None
    ]
    c.evidence["individual_stop_ceilings"] = independent_ceilings
    if len(live_targets) != 2 or len(set(independent_ceilings)) != 2:
        pytest.skip("balance/configuration did not exercise asymmetric ceilings; see evidence")
    plan = build_plan(
        actual,
        TargetState(runtime.account_id, targets),
        c.app._filters_cache,
        lambda kind, symbol, n: f"ct-corrective-{n}",
    )
    leverages = [a for a in plan.actions if a.kind == ActionKind.SET_LEVERAGE]
    assert len(leverages) == 1
    wanted = min(independent_ceilings)
    assert leverages[0].payload["leverage"] == str(wanted)

    class RejectedLeverage:
        def __init__(self, inner):
            self.inner, self.attempts = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def set_leverage(self, *args):
            self.attempts += 1
            raise AsterError(-4028, "injected authoritative leverage rejection")

    fault = RejectedLeverage(runtime.client)
    rejected_runtime = replace(runtime, client=fault)
    # A retry of one FAILED journal row must remain nonzero in evaluation reports.
    assert await c.recompute(rejected_runtime) > 0
    assert await c.recompute(rejected_runtime) > 0
    assert fault.attempts == 2
    assert not [p for p in await c.audit.position_risk(runtime.account_id) if p.position_amt]
    await c.converge()
    before = await c.snapshot()
    assert len([p for p in before.positions.values() if p.qty]) == 2
    assert before.symbol_leverages["BTCUSDT"] == wanted
    for _ in range(2):
        await c.app.leverage._maintain_once()
    assert (await c.snapshot()).symbol_leverages["BTCUSDT"] == wanted
    settings = c.app.cfg
    await c.app.stop()
    c.app = App(settings)
    await c.app.start()
    await c.quiesce()
    await c.converge()
    after = await c.snapshot()
    assert after.symbol_leverages["BTCUSDT"] == wanted
    assert {k: p.qty for k, p in after.positions.items() if p.qty} == {
        k: p.qty for k, p in before.positions.items() if p.qty
    }
    c.evidence["leverage_before_after_restart"] = [wanted, after.symbol_leverages["BTCUSDT"]]


@pytest.mark.parametrize("constraint", ["risk", "margin"])
async def test_account_budget_on_two_real_symbols(campaign, constraint):
    """LC2/LC3: aggregate reservations verified before send and against signed fills."""
    c = campaign
    aid = c.followers[0]
    if constraint == "margin":
        # 1% risk / 1% stop / 2x = about 50% margin per independent symbol.
        c.app.coordinator._operator_max_leverage = 2
        c.app.leverage._operator_max_leverage = 2
        risk, distance = D("0.01"), D("0.01")
    else:
        risk, distance = D("0.03"), D("0.03")
    await c.app.settings.upsert_policy(RiskPolicy(risk, 20, D("0.05")), aid)
    await c.app.leverage.prepare_account(aid, SYMBOLS)
    for symbol in SYMBOLS:
        await c.open_master(symbol, PosSide.LONG, distance)
    master = await c.refresh()
    before = await c.snapshot()
    runtime = c.app.follower_registry.get(aid)
    policy = await c.app.settings.get_policy(aid)
    targets, skips = await c.app.coordinator.targets_for(
        aid, master, before, policy, client=runtime.client
    )
    c.evidence["budget_targets"] = [(t.key.symbol, t.qty, t.leverage) for t in targets]
    assert not skips, skips
    assert len([t for t in targets if t.qty]) == 2, "both real symbols are required for this gate"
    risk_sum = sum(
        (
            abs(t.qty)
            * (
                abs(master.positions[t.key].mark_price - t.stop_price)
                + master.positions[t.key].mark_price * c.app.cfg.fee_buffer_pct
            )
            for t in targets
        ),
        D(0),
    )
    margin_sum = sum(
        (abs(t.qty) * master.positions[t.key].mark_price / t.leverage for t in targets), D(0)
    )
    assert risk_sum <= before.equity_usdt * c.app.cfg.max_total_risk_pct
    assert margin_sum <= before.available_usdt * D("0.8")
    if constraint == "risk":
        assert risk * 2 > c.app.cfg.max_total_risk_pct
    else:
        assert margin_sum > before.available_usdt * D("0.6"), "margin clamp was not exercised"
    planned = {t.key: abs(t.qty) for t in targets}
    await c.recompute(state=master)
    after = await c.snapshot()
    for key, quantity in planned.items():
        leg = after.positions[key]
        assert D(0) < abs(leg.qty) <= quantity
        assert leg.stop is not None and leg.stop.close_position
    c.evidence["budget"] = {
        "planned_stop_risk": risk_sum,
        "planned_margin": margin_sum,
        "equity": before.equity_usdt,
        "available": before.available_usdt,
        "signed_quantities": {str(k): p.qty for k, p in after.positions.items() if p.qty},
    }


async def test_shared_liquidation_quote_uses_mark_geometry(campaign):
    """LC4: compare independent signed HEDGE geometry with the production predicate."""
    c = campaign
    await c.open_master("BTCUSDT", PosSide.LONG, D("0.03"))
    await c.open_master("BTCUSDT", PosSide.SHORT, D("0.02"))
    await c.converge()
    actual = await c.snapshot()
    legs = [p for p in actual.positions.values() if p.qty]
    assert len(legs) == 2 and all(p.stop is not None for p in legs)
    observed = []
    for leg in legs:
        liq, mark, stop = leg.liquidation_price, leg.mark_price, leg.stop.stop_price
        short = leg.key.pos_side == PosSide.SHORT
        adverse = liq > 0 and (liq >= mark if short else liq <= mark)
        expected = adverse and (liq <= stop * D("1.005") if short else liq >= stop * D("0.995"))
        assert c.app.coordinator._liquidation_violated(leg, stop) == expected
        observed.append(
            {
                "leg": leg.key.pos_side,
                "entry": leg.entry_price,
                "mark": mark,
                "liquidation": liq,
                "stop": stop,
                "violation": expected,
            }
        )
    c.evidence["liquidation_geometry"] = observed
    if not any(row["liquidation"] > 0 for row in observed):
        pytest.skip("exchange exposed no nonzero liquidation geometry; see signed evidence")


async def test_real_registration_cap_is_atomic_and_boot_keeps_close_only_accounts(campaign):
    """LC5: concurrent last-slot registration, then boot above cap without losing runtimes."""
    c = campaign
    if len(c.followers) < 2:
        pytest.skip("two provisioned REAL followers required for the registration race")
    registry = c.app.follower_registry
    for aid in c.followers:
        runtime = registry.get(aid)
        await registry.unpublish(aid, expected=runtime)
    registry._max_real_followers = 1
    results = await asyncio.gather(
        *(c.app.register_follower(aid) for aid in c.followers), return_exceptions=True
    )
    assert sum(isinstance(result, ValueError) for result in results) == len(c.followers) - 1
    assert len(registry.snapshot()) == 1
    winner = registry.snapshot()[0]
    await c.open_master("BTCUSDT", PosSide.LONG, D("0.03"))
    await c.recompute(winner)
    before = [p for p in await c.audit.position_risk(winner.account_id) if p.position_amt]
    assert before, "a real protected position is required to test close-only boot"
    original_qty = sum((abs(p.position_amt) for p in before), D(0))
    cfg = c.app.cfg.model_copy(update={"max_followers": 1})
    await c.app.stop()
    c.app = App(cfg)
    await c.app.start()
    await c.quiesce()
    assert len(c.app.followers) == len(c.followers)
    assert not c.app._master_entry_admitted()
    for runtime in c.app.followers:
        await c.recompute(runtime)
        live = [p for p in await c.audit.position_risk(runtime.account_id) if p.position_amt]
        if runtime.account_id == winner.account_id:
            assert D(0) < sum((abs(p.position_amt) for p in live), D(0)) <= original_qty
        else:
            assert not live
    from tests.testnet.test_pre_mainnet_hedge_live import _flatten_account

    await _flatten_account(c.audit, "main")
    for runtime in c.app.followers:
        await c.recompute(runtime)
        assert not [p for p in await c.audit.position_risk(runtime.account_id) if p.position_amt]
    c.evidence["cap"] = {
        "configured": 1,
        "retained_at_boot": len(c.app.followers),
        "new_entries": 0,
    }


async def test_full_reconcile_health_after_saturated_journal(campaign):
    """LC6: bounded history cannot masquerade as convergence; master READY cannot erase health."""
    from copytradeaster.trading.reconciler import ReconcileReport

    c = campaign
    aid = c.followers[0]
    # Synthetic HISTORICAL journal only; every position/order below remains signed REAL state.
    async with c.app.db.transaction() as connection:
        await connection.executemany(
            "INSERT INTO execution_commands("
            "command_key,account_id,kind,payload,status,created_at,updated_at) "
            "VALUES(?,?,'PLACE_STOP','{}','CONFIRMED','2000-01-01','2000-01-01')",
            [(f"corrective-history-{i}", aid) for i in range(10001)],
        )
    await c.open_master("BTCUSDT", PosSide.LONG, D("0.03"))
    before = len(await c.app.commands.recent(10000))
    count = await c.recompute()
    after = len(await c.app.commands.recent(10000))
    assert before == after == 10000 and count > 0
    c.evidence["saturation"] = {
        "bounded_before": before,
        "bounded_after": after,
        "evaluated_actions": count,
    }
    for _ in range(20):
        counts = {runtime.account_id: await c.recompute(runtime) for runtime in c.app.followers}
        if all(value == 0 for value in counts.values()):
            break
        await asyncio.sleep(1)
    else:
        raise AssertionError("followers did not converge to an empty evaluated plan")
    await c.app.leverage._maintain_once()
    await c.app.settings.set_flag("degraded", "corrective-health-proof")
    await c.app._publish_master_ready(
        was_ready=True, stream_epoch=c.app._stream_epoch, event_seq=c.app._master_event_seq
    )
    assert await c.app.settings.get_flag("degraded_reason") == "corrective-health-proof"
    assert await c.app._sweep_hold_reason(ReconcileReport(per_account=counts)) is None
    assert (
        await c.app._sweep_hold_reason(ReconcileReport(per_account=counts, authoritative=False))
        == "scoped-snapshot"
    )
    await c.app.coordinator.resume_after_pause()
    try:
        for _ in range(5):
            await c.app._establish_master_boundary("corrective-authoritative-proof")
            if await c.app.settings.get_flag("degraded_reason") == "":
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("authoritative zero-action boundary did not clear degraded")
    finally:
        await c.app.coordinator.pause_barrier()
