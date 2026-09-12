import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.domain.models import PositionKey, PositionState, PosSide
from copytradeaster.master.projector import MasterDelta, MasterProjector
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CopyTradeRepo,
    MasterEventRepo,
    OrderLinkRepo,
    SettingsRepo,
)


def order_trade(**o):
    base = {
        "s": "BTCUSDT",
        "c": "coid-1",
        "S": "BUY",
        "o": "MARKET",
        "q": "0.002",
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


def account_update(P=None, B=None, E=2000):
    return {
        "e": "ACCOUNT_UPDATE",
        "E": E,
        "T": 1999,
        "a": {"m": "ORDER", "B": B or [], "P": P or []},
    }


def position_payload(symbol="BTCUSDT", pa="0.002", ep="65000", ps="BOTH"):
    return {
        "s": symbol,
        "pa": pa,
        "ep": ep,
        "cr": "0",
        "up": "0",
        "mt": "cross",
        "iw": "0",
        "ps": ps,
    }


@pytest.fixture
async def deps(tmp_path: Path):
    db = Database(tmp_path / "proj.sqlite3")
    await db.connect()
    await db.migrate()
    deltas: list[MasterDelta] = []

    async def on_delta(delta: MasterDelta) -> None:
        deltas.append(delta)

    projector = MasterProjector(
        events=MasterEventRepo(db),
        trades=CopyTradeRepo(db),
        links=OrderLinkRepo(db),
        settings=SettingsRepo(db),
        on_delta=on_delta,
    )
    yield projector, deltas, db
    await db.close()


async def fill_open(deps, E=1000):
    projector, deltas, db = deps
    await projector.apply_raw(
        order_trade(
            E=E,
            x="TRADE",
            X="FILLED",
            l="0.002",
            z="0.002",
            ap="65000",
            L="65000",
            q="0.002",
        )
    )
    return projector


class TestOrderTradeUpdate:
    async def test_fill_zero_to_qty_opens_trade_and_position(self, deps):
        projector, deltas, db = deps
        await fill_open(deps)
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.002")
        trade = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert trade is not None and trade["status"] == "OPEN"
        assert deltas and "BTCUSDT" in deltas[0].changed_symbols

    async def test_fill_to_zero_closes_trade(self, deps):
        projector, _, db = deps
        await fill_open(deps)
        await projector.apply_raw(
            order_trade(
                E=1001,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.002",
                z="0.002",
                ap="65500",
                L="65500",
                q="0.002",
                rp="-1.5",
            )
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == 0
        assert await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH") is None

    async def test_reopen_same_symbol_new_trade_id(self, deps):
        projector, _, db = deps
        await fill_open(deps)
        await projector.apply_raw(order_trade(E=1001, S="SELL", x="TRADE", l="0.002", ap="65500"))
        await projector.apply_raw(order_trade(E=1002, x="TRADE", l="0.001", ap="65000"))
        repo = CopyTradeRepo(db)
        first = await db.fetchone("SELECT MIN(trade_id) AS t FROM copy_trades")
        active = await repo.active_trade("BTCUSDT", "BOTH")
        assert active["trade_id"] > first["t"]

    async def test_new_conditional_places_stop(self, deps):
        projector, deltas, _ = deps
        await projector.apply_raw(
            order_trade(
                E=1003,
                o="STOP_MARKET",
                ot="STOP_MARKET",
                X="NEW",
                x="NEW",
                c="ct-stop",
                i=101,
                sp="64000",
                S="SELL",
                R=True,
                wt="MARK_PRICE",
            )
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        pos = projector.state.positions.get(key)
        assert pos is not None
        assert pos.stop is not None
        assert pos.stop.client_order_id == "ct-stop"
        assert pos.stop.stop_price == D("64000")

    async def test_cancel_plus_new_replaces_stop(self, deps):
        projector, _, _ = deps
        await projector.apply_raw(
            order_trade(
                E=1003,
                o="STOP_MARKET",
                ot="STOP_MARKET",
                X="NEW",
                x="NEW",
                c="ct-old",
                i=101,
                sp="64000",
                S="SELL",
                R=True,
            )
        )
        await projector.apply_raw(
            order_trade(
                E=1004,
                o="STOP_MARKET",
                ot="STOP_MARKET",
                X="NEW",
                x="NEW",
                c="ct-new",
                i=102,
                sp="64500",
                S="SELL",
                R=True,
            )
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        pos = projector.state.positions[key]
        assert pos.stop.client_order_id == "ct-new"
        assert pos.stop.stop_price == D("64500")
        # cancel of the old one after replacement is a no-op
        await projector.apply_raw(
            order_trade(
                E=1005,
                o="STOP_MARKET",
                ot="STOP_MARKET",
                X="CANCELED",
                x="CANCELED",
                c="ct-old",
                i=101,
                sp="64000",
                S="SELL",
                R=True,
            )
        )
        assert projector.state.positions[key].stop.client_order_id == "ct-new"

    async def test_cancel_removes_stop(self, deps):
        projector, _, _ = deps
        await projector.apply_raw(
            order_trade(
                E=1003,
                o="STOP_MARKET",
                ot="STOP_MARKET",
                X="NEW",
                x="NEW",
                c="ct-stop",
                i=101,
                sp="64000",
                S="SELL",
                R=True,
            )
        )
        await projector.apply_raw(
            order_trade(
                E=1004,
                o="STOP_MARKET",
                ot="STOP_MARKET",
                X="CANCELED",
                x="CANCELED",
                c="ct-stop",
                i=101,
                sp="64000",
                S="SELL",
                R=True,
            )
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].stop is None


class TestDedupAndOrder:
    async def test_duplicate_event_ignored(self, deps):
        projector, deltas, db = deps
        raw = order_trade(E=1000, x="TRADE", l="0.002", ap="65000")
        await projector.apply_raw(raw)
        await projector.apply_raw(raw)  # same E, i, x -> duplicate
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.002")
        count = await db.fetchone("SELECT COUNT(*) AS n FROM master_events")
        assert count["n"] == 1

    async def test_out_of_order_trade_applied_not_dropped(self, deps):
        """A late-arriving older-E TRADE carries a real fill delta - it must be
        APPLIED, not dropped: dropping it loses position state until the next
        full REST rebuild. Only non-trade events are E-order guarded."""
        projector, deltas, _ = deps
        await projector.apply_raw(order_trade(E=2000, x="TRADE", l="0.002", ap="65000"))
        await projector.apply_raw(order_trade(E=1500, x="TRADE", l="0.002", ap="65000"))
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.004")

    async def test_out_of_order_non_trade_ignored(self, deps):
        projector, deltas, _ = deps
        await projector.apply_raw(
            order_trade(E=2000, x="NEW", ot="STOP_MARKET", c="ct-a", i=11, sp="64000")
        )
        await projector.apply_raw(
            order_trade(E=1500, x="NEW", ot="STOP_MARKET", c="ct-b", i=12, sp="63000")
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        pos = projector.state.positions.get(key)
        assert pos is not None and pos.stop is not None and pos.stop.stop_price == D("64000")

    async def test_same_ms_partial_fills_both_applied(self, deps):
        """Two partial fills of one order within the same millisecond differ by
        trade id t - dedup must not swallow the second one."""
        projector, _, _ = deps
        await projector.apply_raw(order_trade(E=3000, x="TRADE", l="0.001", ap="65000", t=1))
        await projector.apply_raw(order_trade(E=3000, x="TRADE", l="0.001", ap="65001", t=2))
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.002")

    async def test_delayed_trade_after_authoritative_snapshot_is_not_reapplied(self, deps):
        """PM-B2 red regression: a frame buffered before REST rebuild must not
        add the same fill again after the snapshot already contains it."""
        projector, _, _ = deps

        class Balance:
            asset = "USDT"
            available_balance = D("500")

        class Position:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65000")
            leverage = 5
            liquidation_price = D("30000")
            position_side = PosSide.BOTH

        class Client:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                return [Balance()]

            async def position_risk(self, account_id, symbol=None):
                return [Position()]

            async def open_orders(self, account_id, symbol=None):
                return []

        await projector.rebuild_full(Client(), "main")
        await projector.apply_raw(
            order_trade(
                E=1000,
                x="TRADE",
                X="FILLED",
                l="0.002",
                z="0.002",
                ap="65000",
                L="65000",
                q="0.002",
                t=44,
            )
        )

        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.002")

    async def test_frame_received_during_rebuild_keeps_previous_generation(self, deps):
        """The generation cutover is the completed snapshot, not its start."""
        projector, _, _ = deps
        balance_read_started = asyncio.Event()
        release_balance = asyncio.Event()

        class Balance:
            asset = "USDT"
            available_balance = D("500")

        class Position:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65000")
            leverage = 5
            liquidation_price = D("30000")
            position_side = PosSide.BOTH

        class Client:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                balance_read_started.set()
                await release_balance.wait()
                return [Balance()]

            async def position_risk(self, account_id, symbol=None):
                return [Position()]

            async def open_orders(self, account_id, symbol=None):
                return []

        previous_generation = projector.generation
        rebuild = asyncio.create_task(projector.rebuild_full(Client(), "main"))
        await balance_read_started.wait()
        received_generation = projector.generation
        assert received_generation == previous_generation

        release_balance.set()
        await rebuild
        assert projector.generation == previous_generation + 1

        await projector.apply_raw(
            order_trade(
                E=3000,
                x="TRADE",
                X="FILLED",
                l="0.002",
                z="0.002",
                ap="65000",
                L="65000",
                q="0.002",
                t=45,
            ),
            generation=received_generation,
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.002")

    async def test_server_cutoff_is_inclusive_when_local_clock_is_behind(self, deps, monkeypatch):
        """E == snapshot server time is ambiguous, so fail closed as stale.

        Using `< cutoff` would accept equality; `<= cutoff` is required because
        millisecond timestamps cannot prove that the event happened after the
        snapshot. A local cutoff would also be wrong within permitted drift.
        """
        projector, _, _ = deps
        monkeypatch.setattr("copytradeaster.master.projector.time.time", lambda: 1.0)

        class Balance:
            asset = "USDT"
            available_balance = D("500")

        class Position:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65000")
            leverage = 5
            liquidation_price = D("30000")
            position_side = PosSide.BOTH

        class Client:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                return [Balance()]

            async def position_risk(self, account_id, symbol=None):
                return [Position()]

            async def open_orders(self, account_id, symbol=None):
                return []

        await projector.rebuild_full(Client(), "main")
        await projector.apply_raw(
            order_trade(
                E=2000,
                x="TRADE",
                X="FILLED",
                l="0.002",
                z="0.002",
                ap="65000",
                L="65000",
                q="0.002",
                t=46,
            )
        )

        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].qty == D("0.002")
        assert projector.dirty_symbols == frozenset({"BTCUSDT"})


class TestAccountUpdate:
    async def test_balance_applied_positions_informational(self, deps):
        """AU must not mutate positions: TRADE deltas are the only mutator."""
        projector, deltas, _ = deps
        n_before = len(projector.state.positions)
        await projector.apply_raw(
            account_update(
                B=[{"a": "USDT", "wb": "1234.5", "cw": "1200", "bc": "0"}],
                P=[position_payload(pa="0.003", ep="64900")],
            )
        )
        assert projector.state.available_usdt == D("1234.5")
        assert len(projector.state.positions) == n_before

    async def test_zero_position_au_does_not_close_episode(self, deps):
        """Episode lifecycle belongs to TRADE deltas; a stray AU(pa=0) that lost
        the race against its own TRADE must not tear the episode down."""
        projector, _, db = deps
        await fill_open(deps)
        await projector.apply_raw(account_update(P=[position_payload(pa="0")], E=2001))
        trade = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert trade is not None and trade["status"] == "OPEN"


class TestConfigUpdate:
    async def test_leverage_change(self, deps):
        projector, deltas, _ = deps
        await fill_open(deps)
        await projector.apply_raw(
            {"e": "ACCOUNT_CONFIG_UPDATE", "E": 3000, "T": 2999, "ac": {"s": "BTCUSDT", "l": 10}}
        )
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        assert projector.state.positions[key].leverage == 10

    async def test_hedge_leverage_change_updates_both_legs_and_requests_rebuild(self, deps):
        projector, _, _ = deps
        reasons: list[str] = []

        async def on_critical(reason: str) -> None:
            reasons.append(reason)

        projector._on_critical = on_critical
        await projector.apply_raw(
            order_trade(
                E=1000,
                x="TRADE",
                X="FILLED",
                S="BUY",
                l="0.002",
                L="65000",
                ps="LONG",
                t=80,
            )
        )
        await projector.apply_raw(
            order_trade(
                E=1001,
                x="TRADE",
                X="FILLED",
                S="SELL",
                l="0.003",
                L="65000",
                ps="SHORT",
                t=81,
            )
        )
        reasons.clear()

        await projector.apply_raw(
            {"e": "ACCOUNT_CONFIG_UPDATE", "E": 3000, "ac": {"s": "BTCUSDT", "l": 10}}
        )

        assert projector.state.positions[PositionKey("BTCUSDT", PosSide.LONG)].leverage == 10
        assert projector.state.positions[PositionKey("BTCUSDT", PosSide.SHORT)].leverage == 10
        assert reasons == ["account-config-update"]

    async def test_position_mode_change_requests_authoritative_rebuild(self, deps):
        projector, _, _ = deps
        reasons: list[str] = []

        async def on_critical(reason: str) -> None:
            reasons.append(reason)

        projector._on_critical = on_critical
        await projector.apply_raw({"e": "ACCOUNT_CONFIG_UPDATE", "E": 3001, "ai": {"d": True}})

        assert reasons == ["account-config-update"]


class TestMarginCall:
    async def test_margin_call_sets_degraded_flag(self, deps):
        projector, deltas, db = deps
        raw = {"e": "MARGIN_CALL", "E": 4000, "cw": "100", "p": [position_payload(pa="0.002")]}
        await projector.apply_raw(raw)
        flag = await SettingsRepo(db).get_flag("degraded")
        assert flag is not None and flag.startswith("margin_call")

    async def test_margin_call_requests_immediate_authoritative_rebuild(self, deps):
        projector, _, _ = deps
        reasons: list[str] = []

        async def on_critical(reason: str) -> None:
            reasons.append(reason)

        projector._on_critical = on_critical
        await projector.apply_raw({"e": "MARGIN_CALL", "E": 4001, "cw": "100", "p": []})
        assert reasons == ["margin_call"]

    async def test_calculated_autoclose_requests_immediate_rebuild(self, deps):
        projector, _, _ = deps
        reasons: list[str] = []

        async def on_critical(reason: str) -> None:
            reasons.append(reason)

        projector._on_critical = on_critical
        await projector.apply_raw(
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 4002,
                "o": {
                    "s": "BTCUSDT",
                    "c": "autoclose-123",
                    "x": "CALCULATED",
                    "X": "NEW_ADL",
                    "o": "MARKET",
                    "ot": "MARKET",
                    "ps": "BOTH",
                },
            }
        )
        assert reasons == ["position-changing-order:CALCULATED"]


class TestRebuildFull:
    async def test_failed_snapshot_keeps_previous_generation_and_master_dirty(self, deps):
        projector, _, _ = deps
        previous_state = projector.state
        previous_generation = projector.generation

        class Client:
            async def balance(self, account_id):
                raise RuntimeError("snapshot unavailable")

        with pytest.raises(RuntimeError, match="snapshot unavailable"):
            await projector.rebuild_full(Client(), "main")

        assert projector.state is previous_state
        assert projector.generation == previous_generation
        assert projector.dirty_symbols == frozenset({"*"})

    async def test_snapshot_backfills_legacy_zero_direction(self, deps):
        projector, _, db = deps
        trade_id = await CopyTradeRepo(db).open_trade("BTCUSDT", "BOTH", None, direction=0)

        class Balance:
            asset = "USDT"
            available_balance = D("500")

        class Position:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65000")
            leverage = 5
            liquidation_price = D("30000")
            position_side = PosSide.BOTH

        class Client:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                return [Balance()]

            async def position_risk(self, account_id, symbol=None):
                return [Position()]

            async def open_orders(self, account_id, symbol=None):
                return []

        await projector.rebuild_full(Client(), "main")
        row = await db.fetchone("SELECT direction FROM copy_trades WHERE trade_id=?", (trade_id,))
        assert int(row["direction"]) == 1

    async def test_rest_snapshot(self, deps, monkeypatch):
        projector, _, _ = deps
        requested_order_symbols: list[str | None] = []

        class FakeBalanceDTO:
            def __init__(self):
                self.asset = "USDT"
                self.available_balance = D("500")

        class FakePositionDTO:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65500")
            leverage = 5
            liquidation_price = D("30000")
            margin_type = "isolated"
            isolated_margin = D("12.5")
            position_side = PosSide.BOTH
            un_realized_profit = D("1")

        class FakeOrderDTO:
            def __init__(self, t, sp, ut):
                self.symbol = "BTCUSDT"
                self.type = t
                self.stop_price = D(sp)
                self.reduce_only = True
                self.close_position = False
                self.position_side = PosSide.BOTH
                self.client_order_id = f"ct-{t}"
                self.order_id = 7
                self.side = "SELL"
                self.working_type = "MARK_PRICE"
                self.update_time = ut
                self.orig_qty = D("0.002")

        class FakeClient:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                return [FakeBalanceDTO()]

            async def position_risk(self, account_id, symbol=None):
                return [FakePositionDTO()]

            async def open_orders(self, account_id, symbol=None):
                requested_order_symbols.append(symbol)
                return [
                    FakeOrderDTO("STOP_MARKET", "64000", 1),
                    FakeOrderDTO("TAKE_PROFIT_MARKET", "70000", 2),
                    FakeOrderDTO("MARKET", "0", 0),
                ]

        state = await projector.rebuild_full(FakeClient(), "main")
        assert state.available_usdt == D("500")
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        pos = state.positions[key]
        assert pos.qty == D("0.002")
        assert pos.leverage == 5
        assert pos.margin_type == "ISOLATED"
        assert pos.isolated_margin == D("12.5")
        assert pos.stop is not None and pos.stop.stop_price == D("64000")
        assert pos.take_profit is not None and pos.take_profit.stop_price == D("70000")
        assert requested_order_symbols == ["BTCUSDT"]

    async def test_refresh_mark_prices_updates_authoritative_margin_mode(self, deps):
        projector, _, _ = deps
        key = PositionKey("BTCUSDT", PosSide.BOTH)
        projector.state.positions[key] = PositionState(
            key=key,
            qty=D("0.002"),
            entry_price=D("65000"),
            leverage=5,
            mark_price=D("65000"),
            liquidation_price=D("30000"),
            margin_type="CROSSED",
        )

        class Position:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65500")
            leverage = 15
            liquidation_price = D("30000")
            margin_type = " isolated "
            isolated_margin = D("8")
            position_side = PosSide.BOTH
            un_realized_profit = D("0")

        class Client:
            async def position_risk(self, account_id, symbol=None):
                assert account_id == "main" and symbol == "BTCUSDT"
                return [Position()]

        await projector.refresh_mark_prices(Client(), "main", {"BTCUSDT"})
        pos = projector.state.positions[key]
        assert pos.mark_price == D("65500")
        assert pos.leverage == 15
        assert pos.margin_type == "ISOLATED"
        assert pos.isolated_margin == D("8")

    async def test_flat_snapshot_does_not_require_open_order_history(self, deps):
        projector, _, _ = deps

        class Balance:
            asset = "USDT"
            available_balance = D("500")

        class FlatPosition:
            symbol = "BTCUSDT"
            position_amt = D(0)
            entry_price = D(0)
            mark_price = D("65000")
            leverage = 5
            liquidation_price = D(0)
            position_side = PosSide.BOTH

        class Client:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                return [Balance()]

            async def position_risk(self, account_id, symbol=None):
                return [FlatPosition()]

            async def open_orders(self, account_id, symbol=None):
                raise AssertionError("flat master must not request order history")

        state = await projector.rebuild_full(Client(), "main")

        assert state.available_usdt == D("500")
        assert state.positions == {}

    async def test_hedge_protection_without_reduce_only_is_recovered(self, deps):
        projector, _, _ = deps

        class Balance:
            asset = "USDT"
            available_balance = D("500")

        class Position:
            symbol = "BTCUSDT"
            position_amt = D("0.002")
            entry_price = D("65000")
            mark_price = D("65500")
            leverage = 5
            liquidation_price = D("30000")
            position_side = PosSide.LONG

        class HedgeStop:
            symbol = "BTCUSDT"
            type = "STOP_MARKET"
            stop_price = D("64000")
            reduce_only = False
            close_position = False
            position_side = PosSide.LONG
            client_order_id = "hedge-stop"
            order_id = 7
            side = "SELL"
            working_type = "MARK_PRICE"
            update_time = 1
            orig_qty = D("0.002")

        class Client:
            async def server_time(self):
                return 2000

            async def balance(self, account_id):
                return [Balance()]

            async def position_risk(self, account_id, symbol=None):
                return [Position()]

            async def open_orders(self, account_id, symbol=None):
                return [HedgeStop()]

        state = await projector.rebuild_full(Client(), "main")
        pos = state.positions[PositionKey("BTCUSDT", PosSide.LONG)]
        assert pos.stop is not None
        assert pos.stop.client_order_id == "hedge-stop"


class TestHedgeLegFills:
    async def test_long_leg_sell_fill_closes_not_reopens(self, deps):
        """Live incident: TP fill on the LONG leg is a SELL; the projector must
        close the position, not mirror-increase it (direction by S only)."""
        from copytradeaster.domain.models import PositionKey, PosSide

        projector, deltas, db = deps
        await projector.apply_raw(
            order_trade(
                E=1000,
                x="TRADE",
                X="FILLED",
                S="BUY",
                l="0.002",
                z="0.002",
                ap="65000",
                q="0.002",
                ps="LONG",
            )
        )
        key = PositionKey("BTCUSDT", PosSide.LONG)
        assert projector.state.positions[key].qty == D("0.002")
        # live ordering: ACCOUNT_UPDATE(pa=0) precedes the late TP TRADE;
        # AU is informational, the TRADE delta must still close cleanly
        await projector.apply_raw(
            account_update(E=2000, P=[position_payload(pa="0", ep="65000", ps="LONG")])
        )
        # The AU marks the symbol dirty (stream 0.002 vs pa 0); in production
        # the sweep boundary heals before the late TRADE arrives. Compress that
        # here so this unit test exercises the fill math, not the healer.
        # (mark_dirty() with no args marks "*", so clear the set directly.)
        projector._dirty_symbols.clear()
        await projector.apply_raw(
            order_trade(
                E=2001,
                x="TRADE",
                X="FILLED",
                S="SELL",
                l="0.002",
                z="0.002",
                ap="65500",
                q="0",
                ps="LONG",
            )
        )
        assert projector.state.positions[key].qty == 0
        assert await CopyTradeRepo(db).active_trade("BTCUSDT", "LONG") is None

    async def test_short_leg_open_and_close(self, deps):
        from copytradeaster.domain.models import PositionKey, PosSide

        projector, _, db = deps
        await projector.apply_raw(
            order_trade(
                E=1000,
                x="TRADE",
                X="FILLED",
                S="SELL",
                l="0.002",
                z="0.002",
                ap="65000",
                q="0.002",
                ps="SHORT",
            )
        )
        key = PositionKey("BTCUSDT", PosSide.SHORT)
        assert projector.state.positions[key].qty == D("-0.002")
        await projector.apply_raw(
            order_trade(
                E=2001,
                x="TRADE",
                X="FILLED",
                S="BUY",
                l="0.002",
                z="0.002",
                ap="64800",
                q="0",
                ps="SHORT",
            )
        )
        assert projector.state.positions[key].qty == 0
        assert await CopyTradeRepo(db).active_trade("BTCUSDT", "SHORT") is None
