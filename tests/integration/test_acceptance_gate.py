"""Adversarial acceptance-gate scenarios (review_stage1.md §acceptance gate).

Hermetic end-to-end runs over FakeAster with the REAL
projector -> coordinator -> planner -> executor stack and a real SQLite journal.
Each test defends an invariant the review required before Stage-1 signoff:
persisted-basis restart recovery, transition idempotency, crash-window recovery,
real pause, deviation/post-fill risk, protection convergence, fail-closed routing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.domain.models import (
    AccountState,
    PositionKey,
    PositionState,
    PosSide,
    ProtectiveOrder,
    RiskPolicy,
    Side,
    SkipReason,
)
from copytradeaster.master.projector import MasterDelta, MasterProjector, MasterState
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
from tests.integration.fake_aster import FakeAster, FakeOrder, btc_filters
from tests.integration.test_end_to_end_fake import order_trade, stop_new

FIDS = ["copyc1", "copyc2"]


def build_stack(db: Database, fake: FakeAster) -> tuple[MasterProjector, Coordinator]:
    """Fresh projector+coordinator+executor over the given DB (used both for the
    initial stack and to simulate a process restart over the same database)."""
    followers = [FollowerRuntime(account_id=fid, client=fake, identity=None) for fid in FIDS]
    executor = Executor(
        lambda aid: fake,
        CommandRepo(db),
        OrderLinkRepo(db),
        {},
        settings=SettingsRepo(db),
        on_confirmed=lambda account_id, action: coordinator.on_command_confirmed(
            account_id, action
        ),
    )
    coordinator = Coordinator(
        followers=followers,
        settings=SettingsRepo(db),
        executor=executor,
        trades=CopyTradeRepo(db),
        links=OrderLinkRepo(db),
        filters_provider=lambda: {"BTCUSDT": btc_filters()},
        app_flags=SettingsRepo(db),
        symbol_whitelist=["BTCUSDT"],
        fee_buffer_pct=D("0.001"),
        working_type="MARK_PRICE",
        basis=SyncBasisRepo(db),
    )
    projector = MasterProjector(
        MasterEventRepo(db),
        CopyTradeRepo(db),
        OrderLinkRepo(db),
        SettingsRepo(db),
        lambda delta: coordinator.on_master_delta(delta),
    )
    return projector, coordinator


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "gate.sqlite3")
    await db.connect()
    await db.migrate()
    fake = FakeAster(fill_price=D("65000"))
    fake.add_account("main", D("100000"))
    for fid in FIDS:
        fake.add_account(fid, D("1000"))
        await AccountRepo(db).upsert(fid, "follower", fid, f"0x{fid}")
        await SettingsRepo(db).upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
            fid,
        )
    projector, coordinator = build_stack(db, fake)
    yield projector, coordinator, fake, db
    await db.close()


def follower_qty(fake: FakeAster, account_id: str) -> D:
    return fake.positions.get((account_id, "BTCUSDT:BOTH"), D(0))


def resting_stops(fake: FakeAster, account_id: str) -> list[FakeOrder]:
    return [
        o
        for k, o in fake.orders.items()
        if k.startswith(f"{account_id}|") and o.status == "NEW" and o.type == "STOP_MARKET"
    ]


async def open_master_long(projector: MasterProjector, E=1000, qty="1"):
    await projector.apply_raw(
        order_trade(E=E, x="TRADE", X="FILLED", l=qty, z=qty, ap="65000", L="65000", q=qty)
    )
    await projector.apply_raw(stop_new(E=E + 10, c=f"m-stop-{E}", sp="64500"))


class TestPersistedBasis:
    async def test_restart_after_missed_partial_close_reduces(self, env):
        """Master halved while the bot was offline. A fresh stack (new RAM, same
        DB) must still reduce the follower - the pre-fix engine kept the stale
        position because prev_abs lived only in process memory."""
        from copytradeaster.aster.models import PositionDTO

        projector, _, fake, db, *_ = env
        await open_master_long(projector)
        assert follower_qty(fake, "copyc1") == D("0.019")

        # restart: fresh stack over the SAME database; the missed delta is
        # recovered the way a real boot does it - via REST snapshot
        projector2, coordinator2 = build_stack(db, fake)

        class MasterSnapshot:
            async def balance(self, account_id):
                return await fake.balance(account_id)

            async def position_risk(self, account_id, symbol=None):
                return [
                    PositionDTO(
                        symbol="BTCUSDT",
                        positionAmt=D("0.5"),  # halved while offline
                        entryPrice=D("65000"),
                        markPrice=D("65100"),
                        leverage=20,
                        liquidationPrice=D("100"),
                        marginType="cross",
                        positionSide=PosSide.BOTH,
                        unRealizedProfit=D(0),
                    )
                ]

            async def open_orders(self, account_id, symbol=None):
                return []

        await projector2.rebuild_full(MasterSnapshot(), "main")
        await coordinator2.on_master_delta(MasterDelta(frozenset({"*"}), projector2.state))
        await asyncio.sleep(0)
        # scaled_reduce(0.019, ref 1.0, 0.5) = floor(0.0095) = 0.009
        assert follower_qty(fake, "copyc1") == D("0.009")

    async def test_two_equal_reductions_both_execute(self, env):
        """Two consecutive reductions needing IDENTICAL MARKET_CLOSE payloads:
        payload-idempotency swallowed the second one; revision keys must not."""
        projector, _, fake, *_ = env
        await open_master_long(projector)
        await asyncio.sleep(0)
        closes_before = sum(1 for a, _ in fake.placed_log if a == "copyc1")
        for E in (1200, 1300):
            await projector.apply_raw(
                order_trade(
                    E=E,
                    S="SELL",
                    x="TRADE",
                    X="FILLED",
                    l="0.25",
                    z="0.25",
                    ap="65100",
                    L="65100",
                    q="0.25",
                )
            )
            await asyncio.sleep(0)
        # 0.019*0.75/1.0 -> floor(0.01425)=0.014; then *0.5/0.75 -> floor(0.00933)=0.009
        assert follower_qty(fake, "copyc1") == D("0.009")
        closes_after = sum(1 for a, _ in fake.placed_log if a == "copyc1")
        assert closes_after - closes_before >= 2  # BOTH closes actually sent

    async def test_stop_move_away_and_back_executes_twice(self, env):
        """SL A->B->A: re-placing level A is a NEW transition, not deduped
        against the historical CONFIRMED command."""
        projector, _, fake, *_ = env
        await open_master_long(projector)
        await projector.apply_raw(stop_new(E=1400, c="m-stop-b", sp="64000", i=202))
        await projector.apply_raw(
            order_trade(
                E=1401,
                c="m-stop-1000",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64500",
                i=101,
            )
        )
        await asyncio.sleep(0)
        assert {o.stop_price for o in resting_stops(fake, "copyc1")} == {D("64000")}
        # move back to 64500
        await projector.apply_raw(stop_new(E=1500, c="m-stop-a", sp="64500", i=203))
        await projector.apply_raw(
            order_trade(
                E=1501,
                c="m-stop-b",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64000",
                i=202,
            )
        )
        await asyncio.sleep(0)
        assert {o.stop_price for o in resting_stops(fake, "copyc1")} == {D("64500")}

    async def test_same_target_reconciles_after_confirmed_stop_drifts(self, env):
        """A confirmed stop disappearing from the exchange is a new
        reconciliation generation even though master target JSON is unchanged."""
        projector, coordinator, fake, *_ = env
        await open_master_long(projector)
        placed_before = len(fake.placed_log)
        for order in resting_stops(fake, "copyc1"):
            order.status = "CANCELED"  # external/manual cancellation on exchange

        await coordinator.on_master_delta(MasterDelta(frozenset({"BTCUSDT"}), projector.state))
        await asyncio.sleep(0)

        assert len(fake.placed_log) > placed_before
        assert {o.stop_price for o in resting_stops(fake, "copyc1")} == {D("64500")}


class TestCrashWindows:
    async def test_boot_resolves_sending_and_submitted_without_duplicate(self, tmp_path):
        db = Database(tmp_path / "crash.sqlite3")
        await db.connect()
        await db.migrate()
        fake = FakeAster(fill_price=D("65000"))
        commands = CommandRepo(db)
        links = OrderLinkRepo(db)
        # SENDING: request left, nothing on exchange -> FAILED
        await commands.insert(
            "k-send", "copyc1", None, "MARKET_OPEN", '{"symbol":"BTCUSDT"}', 1, "ct-send"
        )
        await commands.set_status("k-send", "SENDING")
        # SUBMITTED: exchange HAS the order -> CONFIRMED (+link), zero resend
        await commands.insert(
            "k-sub", "copyc1", None, "PLACE_STOP", '{"symbol":"BTCUSDT"}', 2, "ct-sub"
        )
        await commands.set_status("k-sub", "SUBMITTED")
        fake.orders["copyc1|ct-sub"] = FakeOrder(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            orig_qty=D("0.01"),
            client_order_id="ct-sub",
            status="NEW",
        )
        placed_before = list(fake.placed_log)
        report = await resolve_boot_unknowns(lambda aid: fake, commands, links)
        assert report.confirmed == 1 and report.unresolved == 0
        assert (await commands.get("k-sub"))["status"] == "CONFIRMED"
        assert (await commands.get("k-send"))["status"] == "FAILED"
        assert fake.placed_log == placed_before  # no duplicate exchange orders
        link = await links.by_follower_coid("copyc1", "ct-sub")
        assert link is not None  # recovered protection re-enters own-fill guard
        await db.close()


class TestPauseAndRisk:
    async def test_app_pause_flag_blocks_entries_but_allows_later_entry(self, env):
        projector, _, fake, db, *_ = env
        settings = SettingsRepo(db)
        await settings.set_flag("allow_new_entries", "false")
        await open_master_long(projector)
        await asyncio.sleep(0)
        for fid in FIDS:
            assert follower_qty(fake, fid) == 0, fid  # CLI pause really blocks
        await settings.set_flag("allow_new_entries", "true")
        await open_master_long(projector, E=3000, qty="0.5")
        await asyncio.sleep(0)
        assert follower_qty(fake, "copyc1") > 0

    async def test_deviation_gate_blocks_far_entry(self, env):
        _, coordinator, *_ = env
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        master_pos = PositionState(
            key=key,
            qty=D(1),
            entry_price=D("65000"),
            leverage=20,
            mark_price=D("65800"),  # 1.23% above historical entry (> 1% policy)
            liquidation_price=D(0),
            stop=ProtectiveOrder(
                client_order_id="m-stop",
                order_id=1,
                type="STOP_MARKET",
                side=Side.SELL,
                stop_price=D("64500"),
                reduce_only=True,
                close_position=False,
                working_type="MARK_PRICE",
            ),
        )
        master = MasterState(available_usdt=D(100000), positions={key: master_pos})
        actual = AccountState(
            account_id="copyc1", available_usdt=D("1000"), positions={}, open_orders=[]
        )
        policy = RiskPolicy(
            risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")
        )
        targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
        assert [s.reason for s in skips] == [SkipReason.ENTRY_DEVIATION]
        assert targets == []

    async def test_post_fill_worse_price_is_capped(self, env):
        projector, _, fake, *_ = env
        await open_master_long(projector)
        assert follower_qty(fake, "copyc1") == D("0.019")
        # worse actual fill: entry 65550 vs stop distance 1050 instead of 500
        for fid in FIDS:
            fake.entry_prices[(fid, "BTCUSDT:BOTH")] = D("65550")
        # any new master event triggers a sweep that clamps to the REAL cap:
        # cap = 10 / (1050 * 1.001) -> floor step 0.001 -> 0.009
        await projector.apply_raw(stop_new(E=5000, c="m-x", sp="64499", i=301))
        await asyncio.sleep(0)
        assert follower_qty(fake, "copyc1") <= D("0.009")


class TestProtectionConvergence:
    async def test_tp_removal_propagates_to_followers(self, env):
        projector, _, fake, *_ = env
        await open_master_long(projector)
        await projector.apply_raw(
            stop_new(
                E=6000,
                c="m-tp",
                sp="70000",
                S="SELL",
                o="TAKE_PROFIT_MARKET",
                ot="TAKE_PROFIT_MARKET",
                i=401,
            )
        )
        await asyncio.sleep(0)
        tps = [
            o
            for k, o in fake.orders.items()
            if k.startswith("copyc1|") and o.type == "TAKE_PROFIT_MARKET" and o.status == "NEW"
        ]
        assert tps, "follower TP expected"
        await projector.apply_raw(
            order_trade(
                E=6010,
                c="m-tp",
                x="CANCELED",
                X="CANCELED",
                o="TAKE_PROFIT_MARKET",
                ot="TAKE_PROFIT_MARKET",
                sp="70000",
                i=401,
            )
        )
        await asyncio.sleep(0)
        tps_after = [
            o
            for k, o in fake.orders.items()
            if k.startswith("copyc1|") and o.type == "TAKE_PROFIT_MARKET" and o.status == "NEW"
        ]
        assert tps_after == []  # master TP removed -> follower TP canceled

    async def test_orphan_protection_cleaned_on_flat(self, tmp_path):
        """Flat follower with a resting orphan stop is NOT converged: the sweep
        cancels the orphan and places nothing else."""
        db = Database(tmp_path / "orphan.sqlite3")
        await db.connect()
        await db.migrate()
        fake = FakeAster(fill_price=D("65000"))
        fake.add_account("copyc1", D("1000"))
        await AccountRepo(db).upsert("copyc1", "follower", "copyc1", "0xu")
        await SettingsRepo(db).upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
            "copyc1",
        )
        global_fids = FIDS[:]
        FIDS[:] = ["copyc1"]
        try:
            _, coordinator = build_stack(db, fake)
        finally:
            FIDS[:] = global_fids
        fake.orders["copyc1|ct-orphan"] = FakeOrder(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            orig_qty=D("0.01"),
            stop_price=D("64000"),
            reduce_only=True,
            client_order_id="ct-orphan",
            status="NEW",
        )
        placed_before = list(fake.placed_log)
        await coordinator.on_master_delta(
            MasterDelta(frozenset({"*"}), MasterState(available_usdt=D(100000), positions={}))
        )
        await asyncio.sleep(0)
        assert fake.orders["copyc1|ct-orphan"].status == "CANCELED"
        assert fake.placed_log == placed_before  # nothing else executed
        await db.close()

    async def test_stop_in_profit_transferred(self, env):
        """Long entry 65000, market 66500, master locks profit at 66200: a VALID
        protective stop that the pre-fix engine misclassified as NO_STOP
        (compared against the old entry) and refused to copy."""
        projector, _, fake, *_ = env
        await open_master_long(projector)
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        # Feed the authoritative symbol-scoped positionRisk path instead of
        # mutating the projector mark by hand.
        fake.positions[("main", "BTCUSDT:BOTH")] = D("1")
        fake.entry_prices[("main", "BTCUSDT:BOTH")] = D("65000")
        fake.set_price(D("66500"))
        await projector.refresh_mark_prices(fake, "main", {"BTCUSDT"})
        assert projector.state.positions[key].mark_price == D("66500")
        await projector.apply_raw(stop_new(E=7000, c="m-profit", sp="66200", i=501))
        await projector.apply_raw(
            order_trade(
                E=7001,
                c="m-stop-1000",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64500",
                i=101,
            )
        )
        await asyncio.sleep(0)
        stops = {o.stop_price for o in resting_stops(fake, "copyc1")}
        assert stops == {D("66200")}, stops


class TestMasterCriticalRecovery:
    async def test_margin_call_resnapshots_and_reconciles_immediately(self, env):
        projector, coordinator, fake, *_ = env
        await open_master_long(projector)
        assert follower_qty(fake, "copyc1") > 0
        reasons: list[str] = []

        async def on_critical(reason: str) -> None:
            reasons.append(reason)
            await projector.rebuild_full(fake, "main")
            await coordinator.on_master_delta(MasterDelta(frozenset({"*"}), projector.state))

        projector._on_critical = on_critical
        await projector.apply_raw({"e": "MARGIN_CALL", "E": 8000, "cw": "100", "p": []})
        await asyncio.sleep(0)

        assert reasons == ["margin_call"]
        assert projector.state.positions == {}
        assert follower_qty(fake, "copyc1") == 0


class TestFailClosed:
    async def test_unknown_account_never_routes_to_master(self):
        from copytradeaster.app import App

        app = App.__new__(App)
        app.followers = []
        app.master_client = object()
        with pytest.raises(KeyError):
            app._client_for("ghost-account")
