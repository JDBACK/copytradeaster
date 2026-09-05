"""End-to-end scenarios over FakeAster + real projector/coordinator/planner/executor."""

import asyncio
import json
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.app import App
from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PositionKey,
    PositionState,
    PosSide,
    RiskPolicy,
    Side,
    TargetPosition,
    TargetState,
)
from copytradeaster.master.projector import MasterProjector, MasterState
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    CopyTradeRepo,
    MasterEventRepo,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor, resolve_boot_unknowns
from copytradeaster.trading.planner import build_plan
from copytradeaster.trading.reconciler import ReconcileReport
from tests.integration.fake_aster import FakeAster, FakeOrder, btc_filters

D1 = D


def order_trade(**o):
    base = {
        "s": "BTCUSDT",
        "c": "coid-1",
        "S": "BUY",
        "o": "MARKET",
        "q": "1",
        "p": "0",
        "ap": "0",
        "sp": "0",
        "x": "NEW",
        "X": "NEW",
        "i": 100,
        "l": "0",
        "z": "0",
        "L": "0",
        "N": "USDT",
        "n": "0",
        "R": False,
        "wt": "MARK_PRICE",
        "ot": "MARKET",
        "ps": "BOTH",
        "rp": "0",
    }
    base.update(o)
    return {"e": "ORDER_TRADE_UPDATE", "E": base.get("E", 1000), "T": 999, "o": base}


def stop_new(**o):
    base = {
        "s": "BTCUSDT",
        "c": "m-stop",
        "S": "SELL",
        "o": "STOP_MARKET",
        "ot": "STOP_MARKET",
        "q": "1",
        "sp": "64500",
        "x": "NEW",
        "X": "NEW",
        "i": 101,
        "R": True,
        "wt": "MARK_PRICE",
        "ps": "BOTH",
        "ap": "0",
        "p": "0",
        "L": "0",
        "l": "0",
        "z": "0",
        "n": "0",
        "N": "USDT",
        "rp": "0",
    }
    base.update(o)
    return {"e": "ORDER_TRADE_UPDATE", "E": base.get("E", 1100), "T": 1099, "o": base}


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "e2e.sqlite3")
    await db.connect()
    await db.migrate()
    fake = FakeAster(fill_price=D("65000"))
    fake.add_account("main", D("100000"))
    follower_ids = ["copyc1", "copyc2", "copyc3", "copyc4"]
    for fid in follower_ids:
        fake.add_account(fid, D("1000"))
        await AccountRepo(db).upsert(fid, "follower", fid, f"0x{fid}")
        await SettingsRepo(db).upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
            fid,
        )
    followers = [
        FollowerRuntime(account_id=fid, client=fake, identity=None) for fid in follower_ids
    ]
    executor = Executor(
        lambda aid: fake, CommandRepo(db), OrderLinkRepo(db), {}, settings=SettingsRepo(db)
    )
    deltas: list = []

    async def on_delta(delta):
        deltas.append(delta)
        await coordinator.on_master_delta(delta)

    projector = MasterProjector(
        MasterEventRepo(db), CopyTradeRepo(db), OrderLinkRepo(db), SettingsRepo(db), on_delta
    )
    coordinator = Coordinator(
        followers=followers,
        settings=SettingsRepo(db),
        executor=executor,
        trades=CopyTradeRepo(db),
        links=OrderLinkRepo(db),
        filters_provider=lambda: {"BTCUSDT": btc_filters()},
        app_flags=SettingsRepo(db),
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        fee_buffer_pct=D("0.001"),
        working_type="MARK_PRICE",
        basis=SyncBasisRepo(db),
    )
    yield projector, coordinator, fake, db, deltas, follower_ids
    await db.close()


def follower_qty(fake: FakeAster, account_id: str) -> D:
    return fake.positions.get((account_id, "BTCUSDT:BOTH"), D(0))


def follower_stop(fake: FakeAster, account_id: str) -> FakeOrder | None:
    for key, order in fake.orders.items():
        if (
            key.startswith(f"{account_id}|")
            and order.type == "STOP_MARKET"
            and order.status == "NEW"
        ):
            return order
    return None


async def open_master_long(projector, fake, E=1000, qty="1"):
    await projector.apply_raw(
        order_trade(E=E, x="TRADE", X="FILLED", l=qty, z=qty, ap="65000", L="65000", q=qty)
    )
    await projector.apply_raw(stop_new(E=E + 10, c="m-stop", sp="64500"))


async def test_app_boundary_rejects_pre_snapshot_queued_trade(env):
    """Real App envelope + Projector + SQLite + FakeAster boundary.

    The REST state already contains the fill. A WS frame queued before the
    boundary must be audit-processed without adding the fill a second time.
    """
    projector, _, fake, db, _, _ = env
    fake.positions[("main", "BTCUSDT:BOTH")] = D("1")
    fake.entry_prices[("main", "BTCUSDT:BOTH")] = D("65000")
    phases: list[str] = []

    class Reconciler:
        async def reconcile(self, state, reason):
            phases.append(reason)
            return ReconcileReport(per_account={"copyc1": 0})

    class Commands:
        async def in_flight(self):
            return []

    app = App.__new__(App)
    app.master_client = fake
    app.projector = projector
    app.reconciler = Reconciler()
    app.commands = Commands()
    app.settings = SettingsRepo(db)
    app._ingest_queue = asyncio.Queue(maxsize=4)
    app._ingest_dropped = 0
    app._master_event_seq = 0
    app._master_boundary_active = False
    app._stream_connected = True
    app._stream_epoch = 1
    app._entry_admission_ready = False
    app._stream_ready = asyncio.Event()
    app._ready = False
    app._notification_token = "integration-boundary"
    app._emit_notification = lambda **kwargs: asyncio.sleep(0)

    app._enqueue(
        order_trade(
            E=1000,
            x="TRADE",
            X="FILLED",
            l="1",
            z="1",
            ap="65000",
            L="65000",
            q="1",
            t=900,
        )
    )
    assert await app._establish_master_boundary("integration") is True

    envelope = app._ingest_queue.get_nowait()
    await app._consume(envelope)
    app._ingest_queue.task_done()

    key = PositionKey("BTCUSDT", PosSide.BOTH)
    assert projector.state.positions[key].qty == D("1")
    assert phases == ["post-stream-close-only", "post-stream-entry"]
    row = await db.fetchone(
        "SELECT processed_at FROM master_events WHERE event_type='ORDER_TRADE_UPDATE'"
    )
    assert row is not None and row["processed_at"] is not None


class TestOpenAndClose:
    async def test_master_open_copied_with_qty_and_stop(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        for fid in fids:
            assert follower_qty(fake, fid) == D("0.019"), fid
            stop = follower_stop(fake, fid)
            assert stop is not None and stop.stop_price == D("64500")
            assert stop.close_position and stop.reduce_only and stop.side.value == "SELL"

    async def test_master_close_flattens_everyone(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="1",
                z="1",
                ap="65500",
                L="65500",
                q="1",
                rp="5",
            )
        )
        await asyncio.sleep(0)
        for fid in fids:
            assert follower_qty(fake, fid) == 0, fid
            assert follower_stop(fake, fid) is None, fid


class TestReduceAndStops:
    async def test_master_partial_reduce_scales_followers(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.3",
                z="0.3",
                ap="65200",
                L="65200",
                q="0.3",
            )
        )
        for fid in fids:
            # scaled_reduce(0.019, 1.0, 0.7) = floor(0.0133, 0.001) = 0.013
            assert follower_qty(fake, fid) == D("0.013"), fid

    async def test_stop_farther_reduces_qty(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        qty_before = follower_qty(fake, "copyc1")
        # master moves stop farther: 64500 -> 64000
        await projector.apply_raw(stop_new(E=1300, c="m-stop-2", sp="64000", i=102))
        await projector.apply_raw(
            order_trade(
                E=1301,
                c="m-stop",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64500",
                i=101,
            )
        )
        qty_after = follower_qty(fake, "copyc1")
        assert qty_after < qty_before
        # new cap = budget 10 / (1000 * 1.001) = 0.00999 -> 0.009
        assert qty_after == D("0.009")
        stop = follower_stop(fake, "copyc1")
        assert stop is not None and stop.stop_price == D("64000")

    async def test_stop_closer_keeps_qty(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        qty_before = follower_qty(fake, "copyc1")
        await projector.apply_raw(stop_new(E=1400, c="m-stop-3", sp="64900", i=103))
        await projector.apply_raw(
            order_trade(
                E=1401,
                c="m-stop",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64500",
                i=101,
            )
        )
        assert follower_qty(fake, "copyc1") == qty_before  # no auto-increase


class TestFailureIsolation:
    async def test_margin_error_isolates_follower_and_degrades(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        fake.fail_next["copyc3"] = "-2019"
        await open_master_long(projector, fake)
        for fid in fids:
            if fid == "copyc3":
                assert follower_qty(fake, fid) == 0
            else:
                assert follower_qty(fake, fid) == D("0.019"), fid
        flag = await SettingsRepo(db).get_flag("degraded")
        assert flag is not None and "copyc3" in flag
        row = await db.fetchone(
            "SELECT status, last_error FROM execution_commands WHERE account_id='copyc3'"
            " AND status='FAILED'"
        )
        assert row is not None and "-2019" in row["last_error"]

    async def test_503_retries_and_succeeds(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        fake.fail_next["copyc2"] = "503"
        await open_master_long(projector, fake)
        assert follower_qty(fake, "copyc2") == D("0.019")
        # retry used a suffixed coid
        coids = [c for a, c in fake.placed_log if a == "copyc2"]
        assert any(c.endswith("r2") for c in coids)

    async def test_accepted_market_open_response_lost_has_one_economic_fill(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        del coordinator, db, deltas, fids
        fake.fail_next["copyc2"] = "503-after-fill"

        await open_master_long(projector, fake)

        fills = [
            order
            for key, order in fake.orders.items()
            if key.startswith("copyc2|") and order.type == "MARKET"
        ]
        assert len(fills) == 1
        assert fills[0].executed_qty == D("0.019")
        assert follower_qty(fake, "copyc2") == D("0.019")

    async def test_accepted_market_close_response_lost_has_one_economic_fill(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        del coordinator, db, deltas, fids
        await open_master_long(projector, fake)
        before = len(
            [
                order
                for key, order in fake.orders.items()
                if key.startswith("copyc2|") and order.type == "MARKET"
            ]
        )
        fake.fail_next["copyc2"] = "503-after-fill"

        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="1",
                z="1",
                ap="65500",
                L="65500",
                q="1",
                rp="5",
            )
        )

        after = [
            order
            for key, order in fake.orders.items()
            if key.startswith("copyc2|") and order.type == "MARKET"
        ]
        assert len(after) == before + 1
        assert follower_qty(fake, "copyc2") == 0


class TestEconomicOutcomeRecovery:
    async def test_partial_response_lost_next_plan_contains_only_residual(self, tmp_path):
        db = Database(tmp_path / "partial-unknown.sqlite3")
        await db.connect()
        await db.migrate()
        fake = FakeAster(fill_price=D("65000"))
        fake.add_account("copyc1", D("1000"))
        executor = Executor(
            lambda _aid: fake,
            CommandRepo(db),
            OrderLinkRepo(db),
            {},
            settings=SettingsRepo(db),
        )
        first_action = ExecutionAction(
            kind=ActionKind.MARKET_OPEN,
            symbol="BTCUSDT",
            payload={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "0.05",
                "newOrderRespType": "RESULT",
            },
            client_order_id="planner-open",
            replaces_client_order_id=None,
            priority=1,
        )
        fake.partial_market_fill_ratio = D("0.4")
        fake.fail_next["copyc1"] = "503-after-fill"

        first = await executor.submit(
            ExecutionPlan(account_id="copyc1", trade_id=None, actions=[first_action])
        )

        assert first[0].status == "CONFIRMED"
        assert follower_qty(fake, "copyc1") == D("0.02")
        actual = AccountState(
            account_id="copyc1",
            available_usdt=D("1000"),
            positions={
                PositionKey("BTCUSDT"): PositionState(
                    key=PositionKey("BTCUSDT"),
                    qty=D("0.02"),
                    entry_price=D("65000"),
                    leverage=20,
                    mark_price=D("65000"),
                    liquidation_price=D("100"),
                )
            },
        )
        residual_plan = build_plan(
            actual,
            TargetState(
                "copyc1",
                [
                    TargetPosition(
                        key=PositionKey("BTCUSDT"),
                        qty=D("0.05"),
                        stop_price=None,
                        tp_price=None,
                        leverage=20,
                    )
                ],
            ),
            {"BTCUSDT": btc_filters()},
            lambda kind, symbol, seq: f"residual-{kind.value}-{symbol}-{seq}",
        )
        market_actions = [
            action for action in residual_plan.actions if action.kind == ActionKind.MARKET_OPEN
        ]
        assert len(market_actions) == 1
        assert market_actions[0].payload["quantity"] == "0.03"

        fake.partial_market_fill_ratio = None
        second = await executor.submit(residual_plan)
        assert second[0].status == "CONFIRMED"
        assert follower_qty(fake, "copyc1") == D("0.05")
        market_orders = [order for order in fake.orders.values() if order.type == "MARKET"]
        assert [order.executed_qty for order in market_orders] == [D("0.02"), D("0.03")]
        await db.close()


class TestProtectionQuantitySafety:
    async def test_fixed_stop_grow_same_level_then_trigger_flattens(self, env):
        """PROT-I01: the exact P0 baseline reproduction through snapshot,
        planner, journal, executor and exchange trigger semantics."""
        projector, coordinator, fake, db, deltas, fids = env
        del projector, db, deltas, fids
        fid = "copyc1"
        key = PositionKey("BTCUSDT")
        fake.positions[(fid, "BTCUSDT:BOTH")] = D("0.02")
        fake.entry_prices[(fid, "BTCUSDT:BOTH")] = D("65000")
        fake.orders[f"{fid}|ct-old-fixed"] = FakeOrder(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP",
            orig_qty=D("0.02"),
            stop_price=D("64500"),
            reduce_only=True,
            close_position=False,
            position_side=PosSide.BOTH,
            client_order_id="ct-old-fixed",
            order_id=901,
            update_seq=1,
        )
        fr = next(f for f in coordinator.followers if f.account_id == fid)
        actual = await coordinator._snapshot(fr)
        target = TargetPosition(
            key=key,
            qty=D("0.05"),
            stop_price=D("64500"),
            tp_price=None,
            leverage=20,
            stop_type="STOP",
        )
        plan = build_plan(
            actual,
            TargetState(fid, [target]),
            {"BTCUSDT": btc_filters()},
            coordinator._coid_factory(),
        )
        assert [action.kind.value for action in plan.actions] == [
            "MARKET_OPEN",
            "PLACE_STOP",
            "CANCEL_ORDER",
        ]
        await coordinator._executor.submit(plan)
        new_stop = next(
            order
            for order in fake.orders.values()
            if order.client_order_id != "ct-old-fixed" and order.status == "NEW"
        )
        assert new_stop.orig_qty == D("0.05")
        fake.set_price(D("64400"))
        assert follower_qty(fake, fid) == 0

    async def test_partial_market_fill_is_protected_at_live_remainder(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        del coordinator, db, deltas
        fake.partial_market_fill_ratio = D("0.5")
        await open_master_long(projector, fake)
        for fid in fids:
            qty = follower_qty(fake, fid)
            assert D(0) < qty < D("0.019")
            stop = follower_stop(fake, fid)
            assert stop is not None and stop.close_position
        fake.partial_market_fill_ratio = None

    async def test_grow_is_flattened_when_replacement_stop_fails_twice(self, env):
        """PROT-I05/INV-06: a confirmed increase may not remain partly
        protected by the old fixed-quantity Stop."""
        projector, coordinator, fake, db, deltas, fids = env
        del projector, deltas, fids
        fid = "copyc1"
        key = PositionKey("BTCUSDT")
        fake.positions[(fid, "BTCUSDT:BOTH")] = D("0.02")
        fake.entry_prices[(fid, "BTCUSDT:BOTH")] = D("65000")
        fake.orders[f"{fid}|ct-old-grow-fixed"] = FakeOrder(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP",
            orig_qty=D("0.02"),
            price=D("64450"),
            stop_price=D("64500"),
            reduce_only=True,
            close_position=False,
            position_side=PosSide.BOTH,
            client_order_id="ct-old-grow-fixed",
            order_id=903,
            update_seq=1,
        )
        target = TargetPosition(
            key=key,
            qty=D("0.05"),
            stop_price=D("64500"),
            tp_price=None,
            leverage=20,
            stop_type="STOP",
        )
        fr = next(f for f in coordinator.followers if f.account_id == fid)
        actual = await coordinator._snapshot(fr)
        plan = build_plan(
            actual,
            TargetState(fid, [target]),
            {"BTCUSDT": btc_filters()},
            coordinator._coid_factory(),
        )
        fake.fail_order_types[(fid, "STOP")] = 2
        results = await coordinator._executor.submit(plan)
        assert results[0].status == "CONFIRMED" and follower_qty(fake, fid) == D("0.05")

        safety_results = await coordinator._post_execution_converge(
            fr, MasterState(), [target], plan, results
        )

        assert any(result.status == "FAILED" for result in [*results, *safety_results])
        assert follower_qty(fake, fid) == 0
        assert not any(
            order.status == "NEW"
            and order.type in {"STOP_MARKET", "STOP"}
            and coid.startswith(f"{fid}|")
            for coid, order in fake.orders.items()
        )
        degraded = await SettingsRepo(db).get_flag("degraded")
        assert degraded is not None and "unprotected-entry-flattened" in degraded

    async def test_entry_is_flattened_when_stop_fails_twice(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        del coordinator, deltas, fids
        fake.fail_order_types[("copyc1", "STOP_MARKET")] = 2
        await open_master_long(projector, fake)
        assert follower_qty(fake, "copyc1") == 0
        assert follower_stop(fake, "copyc1") is None
        degraded = await SettingsRepo(db).get_flag("degraded")
        assert degraded is not None and "unprotected-entry-flattened" in degraded

    async def test_manual_conditional_is_neither_selected_nor_canceled(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        del projector, db, deltas, fids
        fid = "copyc1"
        fake.positions[(fid, "BTCUSDT:BOTH")] = D("0.02")
        fake.orders[f"{fid}|manual-stop"] = FakeOrder(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            orig_qty=D(0),
            stop_price=D("64500"),
            close_position=True,
            client_order_id="manual-stop",
            order_id=902,
            update_seq=1,
        )
        fr = next(f for f in coordinator.followers if f.account_id == fid)
        actual = await coordinator._snapshot(fr)
        assert actual.positions[PositionKey("BTCUSDT")].stop is None
        target = TargetPosition(PositionKey("BTCUSDT"), D("0.02"), D("64500"), None, 20)
        plan = build_plan(
            actual,
            TargetState(fid, [target]),
            {"BTCUSDT": btc_filters()},
            coordinator._coid_factory(),
        )
        assert all(
            action.payload.get("origClientOrderId") != "manual-stop" for action in plan.actions
        )


class TestRestartRecovery:
    async def test_boot_resolve_unknown_no_duplicates(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        # simulate a crash after the exchange accepted an order but before
        # the journal recorded CONFIRMED: UNKNOWN row + live order on exchange
        coid = "ct-crash0000000000000001"
        fake.orders[f"copyc1|{coid}"] = FakeOrder(
            symbol="BTCUSDT",
            side="SELL",
            type="STOP_MARKET",
            orig_qty=D("0.019"),
            stop_price=D(64500),
            reduce_only=True,
            client_order_id=coid,
            order_id=999,
            update_seq=1,
        )
        await db.execute(
            "INSERT INTO execution_commands(command_key, account_id, trade_id, kind,"
            " payload, priority, status, attempts, client_order_id, created_at,"
            " updated_at) VALUES ('k-crash', 'copyc1', NULL, 'PLACE_STOP', ?, 2,"
            " 'UNKNOWN', 1, ?, datetime('now'), datetime('now'))",
            ('{"symbol": "BTCUSDT"}', coid),
        )
        await db.conn.commit()
        report = await resolve_boot_unknowns(lambda aid: fake, CommandRepo(db))
        assert report.confirmed == 1 and report.unresolved == 0
        # no duplicate order was sent
        assert ("copyc1", coid) not in [(a, c) for a, c in fake.placed_log]
        row = await db.fetchone("SELECT status FROM execution_commands WHERE command_key='k-crash'")
        assert row["status"] == "CONFIRMED"


class TestPauseAndWhitelist:
    async def test_pause_blocks_entries_close_passes(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        for fid in fids:
            await SettingsRepo(db).upsert_policy(
                RiskPolicy(
                    risk_percent=D("0.01"),
                    max_leverage=20,
                    max_entry_deviation_pct=D("0.01"),
                    allow_new_entries=False,
                ),
                fid,
            )
        await open_master_long(projector, fake)
        assert all(follower_qty(fake, fid) == 0 for fid in fids)
        # now give copyc1 a position and close the master: close must pass
        fake.positions[("copyc1", "BTCUSDT:BOTH")] = D("0.019")
        await projector.apply_raw(
            order_trade(E=1500, S="SELL", x="TRADE", X="FILLED", l="1", z="1", ap="65000", q="1")
        )
        assert follower_qty(fake, "copyc1") == 0

    async def test_non_whitelisted_symbol_ignored(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        raw = order_trade(
            E=1600, s="SOLUSDT", x="TRADE", X="FILLED", l="10", z="10", ap="150", L="150", q="10"
        )
        raw["o"]["s"] = "SOLUSDT"
        await projector.apply_raw(raw)
        assert all(len(fake.placed_log) == 0 for _ in [0])
        assert all(follower_qty(fake, fid) == 0 for fid in fids)


class TestSelfStopGuard:
    async def test_follower_stop_fill_blocks_reentry_same_episode(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        # copyc2's stop is touched manually (price reached its stop)
        fake.trigger_stop_fill("copyc2")
        assert follower_qty(fake, "copyc2") == 0
        # master still long -> next recompute must NOT re-enter copyc2
        await projector.apply_raw(stop_new(E=1700, c="m-stop-4", sp="64400", i=104))
        assert follower_qty(fake, "copyc2") == 0
        assert len(follower_qty_all(fake, "copyc2")) == 0

        # master closes and opens a NEW episode -> copyc2 may enter again
        await projector.apply_raw(
            order_trade(E=1800, S="SELL", x="TRADE", X="FILLED", l="1", z="1", ap="65000", q="1")
        )
        await asyncio.sleep(0)
        trade = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert trade is None  # episode closed
        await projector.apply_raw(
            order_trade(E=1900, x="TRADE", X="FILLED", l="1", z="1", ap="65000", q="1", i=200)
        )
        # new episode must carry its OWN stop: the dead level from the previous
        # episode is cleared on close and naked entries are refused
        await projector.apply_raw(stop_new(E=1905, c="m-stop-5", sp="64400", i=205))
        await asyncio.sleep(0)
        # new episode entry is sized from the CURRENT master stop (64400):
        # budget 10 / (600 * 1.001) -> 0.016
        assert follower_qty(fake, "copyc2") == D("0.016")


def follower_qty_all(fake, account_id):
    return [k for k in fake.positions if k[0] == account_id]


def resting_conditionals(fake: FakeAster, account_id: str) -> list[FakeOrder]:
    return [
        o
        for key, o in fake.orders.items()
        if key.startswith(f"{account_id}|")
        and o.status == "NEW"
        and o.type in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    ]


def tp_new(**o):
    base = {
        "s": "BTCUSDT",
        "c": "m-tp",
        "S": "SELL",
        "o": "TAKE_PROFIT_MARKET",
        "ot": "TAKE_PROFIT_MARKET",
        "q": "1",
        "sp": "70000",
        "x": "NEW",
        "X": "NEW",
        "i": 300,
        "R": True,
        "wt": "MARK_PRICE",
        "ps": "BOTH",
        "ap": "0",
        "p": "0",
        "L": "0",
        "l": "0",
        "z": "0",
        "n": "0",
        "N": "USDT",
        "rp": "0",
    }
    base.update(o)
    return {"e": "ORDER_TRADE_UPDATE", "E": base.get("E", 1100), "T": 1099, "o": base}


class TestDuplicateProtectionConvergence:
    """Reviewer blocker, full engine path (projector -> coordinator -> planner ->
    executor -> journal): a protection replacement whose dependent CANCEL failed
    leaves the stale bot-owned level resting next to the new one. Before the fix
    the next reconcile saw current==target and issued NOTHING; now it must
    converge the leg to exactly ONE stop and ONE TP."""

    @staticmethod
    def _fail_next_cancel(fake: FakeAster, coid: str) -> None:
        """Make the NEXT engine attempt to cancel `coid` die as outcome-known
        failure (the exchange never saw it), like a real gateway error."""
        original = fake.cancel_order

        async def flaky(account_id, symbol, *, orig_client_order_id=None, order_id=None):
            if orig_client_order_id == coid:
                from copytradeaster.aster.trading_api import AsterError

                raise AsterError(-1000, "simulated gateway loss", http_status=503)
            return await original(
                account_id, symbol, orig_client_order_id=orig_client_order_id, order_id=order_id
            )

        fake.cancel_order = flaky  # type: ignore[method-assign]

    async def test_stale_stop_removed_after_failed_dependent_cancel(self, env):
        from copytradeaster.master.projector import MasterDelta

        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        await open_master_long(projector, fake)
        old_stop = follower_stop(fake, fid)
        assert old_stop is not None and old_stop.stop_price == D("64500")

        self._fail_next_cancel(fake, old_stop.client_order_id)
        try:
            # master moves the stop 64500 -> 64000 through the real pipeline;
            # the engine places the new stop CONFIRMED but its dependent cancel dies
            await projector.apply_raw(stop_new(E=2000, c="m-stop-b", sp="64000", i=202))
        finally:
            del fake.cancel_order  # un-shadow the fake exchange method

        # failure shape on the exchange: BOTH levels rest simultaneously
        stops = [o for o in resting_conditionals(fake, fid) if o.type == "STOP_MARKET"]
        assert {o.stop_price for o in stops} == {D("64000"), D("64500")}

        # the NEXT reconciliation must remove the stale level and keep the new one
        fr = next(f for f in coordinator.followers if f.account_id == fid)
        await coordinator.recompute_follower(fr, MasterDelta(frozenset({"*"}), projector.state))

        remaining = [o for o in resting_conditionals(fake, fid) if o.type == "STOP_MARKET"]
        assert [o.stop_price for o in remaining] == [D("64000")]
        assert remaining[0].client_order_id != old_stop.client_order_id

        # journal proof: a CONFIRMED CANCEL targeted the stale order
        rows = await CommandRepo(db).recent(10_000)
        assert any(
            r["account_id"] == fid
            and r["kind"] == "CANCEL_ORDER"
            and r["status"] == "CONFIRMED"
            and json.loads(str(r["payload"]))["origClientOrderId"] == old_stop.client_order_id
            for r in rows
        ), "reconcile must have canceled the stale stop"

        # converged: an identical follow-up pass issues zero commands
        before = len(await CommandRepo(db).recent(10_000))
        await coordinator.recompute_follower(fr, MasterDelta(frozenset({"*"}), projector.state))
        assert len(await CommandRepo(db).recent(10_000)) == before

    async def test_stale_tp_removed_after_failed_dependent_cancel(self, env):
        from copytradeaster.master.projector import MasterDelta

        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        await open_master_long(projector, fake)
        await projector.apply_raw(tp_new(E=2000, c="m-tp-a", sp="70000", i=203))
        old_tp = next(o for o in resting_conditionals(fake, fid) if o.type == "TAKE_PROFIT_MARKET")

        self._fail_next_cancel(fake, old_tp.client_order_id)
        try:
            await projector.apply_raw(tp_new(E=2100, c="m-tp-b", sp="70500", i=204))
        finally:
            del fake.cancel_order

        tps = [o for o in resting_conditionals(fake, fid) if o.type == "TAKE_PROFIT_MARKET"]
        assert {o.stop_price for o in tps} == {D("70000"), D("70500")}

        fr = next(f for f in coordinator.followers if f.account_id == fid)
        await coordinator.recompute_follower(fr, MasterDelta(frozenset({"*"}), projector.state))

        tps = [o for o in resting_conditionals(fake, fid) if o.type == "TAKE_PROFIT_MARKET"]
        assert [o.stop_price for o in tps] == [D("70500")]

    async def test_flat_close_removes_picked_and_extra_protection_in_one_pass(self, env):

        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        await open_master_long(projector, fake)
        old_stop = follower_stop(fake, fid)
        assert old_stop is not None
        self._fail_next_cancel(fake, old_stop.client_order_id)
        try:
            await projector.apply_raw(stop_new(E=2000, c="m-stop-b", sp="64000", i=202))
        finally:
            del fake.cancel_order
        assert {o.stop_price for o in resting_conditionals(fake, fid)} == {
            D("64000"),
            D("64500"),
        }

        # master flattens while TWO follower stops still rest
        await projector.apply_raw(
            order_trade(
                E=2200, S="SELL", x="TRADE", X="FILLED", l="1", z="1", ap="65000", L="65000", q="1"
            )
        )
        assert follower_qty(fake, fid) == 0
        assert resting_conditionals(fake, fid) == []


class TestMasterFlipEpisodeSemantics:
    """Reviewer regression (pass 3): a master sign flip is a TRANSITIONAL zero
    for the follower - close the old side, then open the reversed side on the
    next pass. The projector must close the old episode and open a new one for
    the new direction (which also clears sticky zero_target markers); the flip
    zero itself must NEVER latch INTENTIONAL_FLAT."""

    async def test_flip_closes_long_opens_short_with_new_episode(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        await open_master_long(projector, fake)
        assert follower_qty(fake, fid) == D("0.019")
        first = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert first is not None and int(first["direction"]) == 1

        # one fill flips the master LONG +1 -> SHORT -0.5
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="1.5",
                z="1.5",
                ap="65000",
                L="65000",
                q="1.5",
            )
        )
        await asyncio.sleep(0)
        # transitional zero: the LONG side is closed, NOTHING latches
        assert follower_qty(fake, fid) == 0, "flip must close the old side"
        row = await SyncBasisRepo(db).get(fid, "BTCUSDT", "BOTH")
        assert row is None or int(row["zero_target"]) == 0, "flip zero must not latch"

        flipped = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert flipped is not None, "flip must leave an active episode"
        assert int(flipped["trade_id"]) != int(first["trade_id"]), "flip must open a NEW episode"
        assert int(flipped["direction"]) == -1, "new episode must carry the SHORT direction"

        # master protects the SHORT leg (BUY stop above the market) ...
        await projector.apply_raw(stop_new(E=1210, c="m-stop-s", sp="65500", S="BUY"))
        # The flip close + SHORT open + stop placement settle over several
        # executor generations (SET_LEVERAGE -> MARKET_OPEN -> PLACE_STOP DAG);
        # drain for convergence instead of assuming a single tick.
        _deadline = time.monotonic() + 10
        while follower_qty(fake, fid) >= 0 and time.monotonic() < _deadline:
            await asyncio.sleep(0.05)
        # ... and the follower opens the SHORT on the next pass
        qty = follower_qty(fake, fid)
        assert qty < 0, "follower must open the reversed side after a flip"
        stop = follower_stop(fake, fid)
        assert stop is not None and stop.stop_price == D("65500")

        # repeated reconciles KEEP the SHORT: no marker blocks the position
        from copytradeaster.master.projector import MasterDelta

        fr = next(f for f in coordinator.followers if f.account_id == fid)
        for _ in range(2):
            await coordinator.recompute_follower(fr, MasterDelta(frozenset({"*"}), projector.state))
        assert follower_qty(fake, fid) == qty

    async def test_flip_from_dust_flat_still_opens_short(self, env):
        """The exact reviewer sequence: dust-flat marker set INSIDE an episode,
        then the master flips. The new-direction entry must NOT be blocked."""
        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        await open_master_long(projector, fake)
        assert follower_qty(fake, fid) == D("0.019")
        first = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")

        # dust: master 1.0 -> 0.03 floors the follower target to zero;
        # the intentional flat LATCHES (sticky marker)
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.97",
                z="0.97",
                ap="65000",
                L="65000",
                q="1",
            )
        )
        await asyncio.sleep(0)
        assert follower_qty(fake, fid) == 0
        row = await SyncBasisRepo(db).get(fid, "BTCUSDT", "BOTH")
        assert row is not None and int(row["zero_target"]) == 1, "dust flat must latch"

        # master flips +0.03 -> -0.02 in one fill
        await projector.apply_raw(
            order_trade(
                E=1250,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.05",
                z="0.05",
                ap="65000",
                L="65000",
                q="1",
            )
        )
        await asyncio.sleep(0)
        flipped = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert flipped is not None
        assert int(flipped["trade_id"]) != int(first["trade_id"])
        short_dir = int(flipped["direction"])
        assert short_dir == -1
        row = await SyncBasisRepo(db).get(fid, "BTCUSDT", "BOTH")
        assert row is None or int(row["zero_target"]) == 0, (
            "the flip's new episode must clear the sticky marker"
        )

        # short protection -> follower enters the reversed side (drain the DAG, see above)
        await projector.apply_raw(stop_new(E=1260, c="m-stop-s", sp="65500", S="BUY"))
        _deadline = time.monotonic() + 10
        while follower_qty(fake, fid) >= 0 and time.monotonic() < _deadline:
            await asyncio.sleep(0.05)
        assert follower_qty(fake, fid) < 0, "SHORT entry must not be blocked by the old marker"


class TestDustReductionNoReentry:
    """Reviewer edge case: a proportional reduction that rounds down to zero
    (master 1.0 -> 0.5 vs a minQty follower) is an INTENTIONAL flat inside the
    still-open master episode. The engine must not re-open the follower on the
    next post-fill/periodic reconcile - and must NOT lose the repaired
    drift-recovery behavior when the target was genuinely non-zero."""

    async def test_zero_rounded_target_stays_flat_until_new_episode(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        # normal entry: followers size 0.019 from their own risk budget
        await open_master_long(projector, fake)
        assert follower_qty(fake, fid) == D("0.019")

        # master cuts 1.0 -> 0.03: scaled target = floor(0.019 * 0.03 / 1.0)
        # = floor(0.00057) = 0 -> the whole follower leg must close ON PURPOSE
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.97",
                z="0.97",
                ap="65000",
                L="65000",
                q="1",
            )
        )
        await asyncio.sleep(0)
        assert follower_qty(fake, fid) == 0, "dust target must flatten the leg"

        # post-fill recompute + repeated reconciles must NOT re-open:
        from copytradeaster.master.projector import MasterDelta

        fr = next(f for f in coordinator.followers if f.account_id == fid)
        for _ in range(3):
            await coordinator.recompute_follower(fr, MasterDelta(frozenset({"*"}), projector.state))
        assert follower_qty(fake, fid) == 0, "intentional flat must persist"

        # master stop move inside the SAME episode - still no re-entry
        await projector.apply_raw(stop_new(E=1300, c="m-stop-b", sp="64000", i=202))
        assert follower_qty(fake, fid) == 0

        # only a NEW master episode re-enables entry (same price: this bare
        # harness has no mark-price refresher, so +1.5% would trip the
        # legitimate ENTRY_DEVIATION gate)
        await projector.apply_raw(
            order_trade(
                E=1400,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.03",
                z="0.03",
                ap="65000",
                L="65000",
                q="1",
            )
        )  # closes the episode (master flat)
        assert follower_qty(fake, fid) == 0
        await projector.apply_raw(
            order_trade(
                E=1500, x="TRADE", X="FILLED", l="1", z="1", ap="65000", L="65000", q="1", i=300
            )
        )  # new episode
        await projector.apply_raw(stop_new(E=1510, c="m-stop-c", sp="64500", i=302))
        qty = follower_qty(fake, fid)
        assert qty > D("0"), "new episode must allow normal entry again"

    async def test_manual_close_with_nonzero_target_still_recovers(self, env):
        """Control: when the issued target was NON-zero, an actual-vs-target
        drift (manual close) must still be repaired by re-entry."""
        projector, coordinator, fake, db, deltas, fids = env
        fid = "copyc1"
        # master long 1.0 with stop; followers enter 0.019 as usual
        await open_master_long(projector, fake)
        assert follower_qty(fake, fid) > 0
        entered = follower_qty(fake, fid)

        # simulate a MANUAL external close of the follower position
        key = ("copyc1", "BTCUSDT:BOTH")
        fake.positions.pop(key, None)
        fake.entry_prices.pop(key, None)

        from copytradeaster.master.projector import MasterDelta

        fr = next(f for f in coordinator.followers if f.account_id == fid)
        await coordinator.recompute_follower(fr, MasterDelta(frozenset({"*"}), projector.state))
        recovered = follower_qty(fake, fid)
        assert recovered > 0, "genuine drift must be repaired"
        # same magnitude: the persisted basis is unchanged, this is recovery
        assert recovered == entered
