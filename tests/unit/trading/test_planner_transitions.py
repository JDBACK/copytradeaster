from decimal import Decimal as D

from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    PositionKey,
    PositionState,
    PosSide,
    ProtectiveOrder,
    Side,
    SymbolFilters,
    TargetPosition,
    TargetState,
)
from copytradeaster.trading.planner import build_plan


def filters(symbol="BTCUSDT", step="0.001", tick="0.1") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=D(tick),
        step_size=D(step),
        min_qty=D("0.001"),
        max_qty=D(1000),
        market_max_qty=D(120),
        min_notional=D(5),
    )


def coid_factory(kind, symbol, seq):
    return f"ct-{kind.value.lower()}-{symbol}-{seq}"


def account(positions=None) -> AccountState:
    plist = list(positions or [])
    return AccountState(
        account_id="copyc1", available_usdt=D(1000), positions={p.key: p for p in plist}
    )


def pos(key, qty, lev=20, stop=None, tp=None) -> PositionState:
    return PositionState(
        key=key,
        qty=D(qty),
        entry_price=D(65000),
        leverage=lev,
        mark_price=D(65000),
        liquidation_price=D(30000),
        stop=stop,
        take_profit=tp,
    )


def prot(coid, stop_price, side=Side.SELL, otype="STOP_MARKET") -> ProtectiveOrder:
    return ProtectiveOrder(
        client_order_id=coid,
        order_id=1,
        type=otype,
        side=side,
        stop_price=D(stop_price),
        reduce_only=True,
        close_position=False,
        working_type="MARK_PRICE",
    )


KEY = PositionKey("BTCUSDT")
TARGET_LONG = TargetPosition(
    key=KEY, qty=D("0.05"), stop_price=D(64500), tp_price=None, leverage=20
)


def plan(actual, targets, flt=None):
    flt = flt or {"BTCUSDT": filters()}
    return build_plan(actual, TargetState("copyc1", targets), flt, coid_factory)


class TestOpenRules:
    def test_open_with_stop(self):
        p = plan(account(), [TARGET_LONG])
        kinds = [(a.kind, a.priority) for a in p.actions]
        assert kinds == [(ActionKind.MARKET_OPEN, 1), (ActionKind.PLACE_STOP, 2)]
        open_action = p.actions[0]
        assert open_action.payload == {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.05",
            "newOrderRespType": "RESULT",
        }
        assert open_action.client_order_id == "ct-market_open-BTCUSDT-1"
        stop_action = p.actions[1]
        assert stop_action.payload["type"] == "STOP_MARKET"
        assert stop_action.payload["stopPrice"] == "64500"
        assert stop_action.payload["quantity"] == "0.05"
        assert stop_action.payload["reduceOnly"] == "true"
        assert stop_action.payload["workingType"] == "MARK_PRICE"
        assert stop_action.payload["priceProtect"] == "false"

    def test_short_open_side_sell(self):
        t = TargetPosition(KEY, D("-0.05"), D(65500), None, 20)
        p = plan(account(), [t])
        assert [a.kind for a in p.actions] == [ActionKind.MARKET_OPEN, ActionKind.PLACE_STOP]
        assert p.actions[0].payload["side"] == "SELL"

    def test_delta_smaller_than_step_is_noop_for_size(self):
        existing = pos(KEY, "0.0500")
        t = TargetPosition(KEY, D("0.05009"), D(64500), None, 20)
        p = plan(account([existing]), [t])
        assert [a.kind for a in p.actions] == [ActionKind.PLACE_STOP]

    def test_increase_existing_position(self):
        st = pos(KEY, "0.02", stop=prot("ct-old-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64500), None, 20)
        p = plan(account([st]), [t])
        # same stop level -> only the open; protections untouched
        assert [(a.kind, a.priority) for a in p.actions] == [(ActionKind.MARKET_OPEN, 1)]

    def test_increase_and_move_stop(self):
        st = pos(KEY, "0.02", stop=prot("ct-old-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64000), None, 20)
        p = plan(account([st]), [t])
        seq = [(a.kind, a.priority) for a in p.actions]
        assert seq == [
            (ActionKind.MARKET_OPEN, 1),
            (ActionKind.PLACE_STOP, 2),
            (ActionKind.CANCEL_ORDER, 2),
        ]
        # place-new precedes cancel-old
        kinds = [k for k, _ in seq]
        assert kinds.index(ActionKind.PLACE_STOP) < kinds.index(ActionKind.CANCEL_ORDER)

    def test_increase_keeps_unchanged_stop(self):
        st = pos(KEY, "0.02", stop=prot("ct-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64500), None, 20)
        p = plan(account([st]), [t])
        assert [a.kind for a in p.actions] == [ActionKind.MARKET_OPEN]


class TestReduceRules:
    def test_partial_close_keeps_protective(self):
        st = pos(KEY, "0.10", stop=prot("ct-old-stop", 64500))
        t = TargetPosition(KEY, D("0.07"), D(64500), None, 20)
        p = plan(account([st]), [t])
        assert [(a.kind, a.priority) for a in p.actions] == [(ActionKind.MARKET_CLOSE, 1)]
        assert p.actions[0].payload["quantity"] == "0.03"
        assert p.actions[0].payload["reduceOnly"] == "true"
        assert p.actions[0].payload["side"] == "SELL"

    def test_flat_cancels_then_closes(self):
        st = pos(
            KEY,
            "0.05",
            stop=prot("ct-old-stop", 64500),
            tp=prot("ct-old-tp", 70000, side=Side.BUY, otype="TAKE_PROFIT_MARKET"),
        )
        t = TargetPosition(KEY, D(0), None, None, 20)
        p = plan(account([st]), [t])
        assert [(a.kind, a.priority) for a in p.actions] == [
            (ActionKind.CANCEL_ORDER, 1),
            (ActionKind.CANCEL_ORDER, 1),
            (ActionKind.MARKET_CLOSE, 2),
        ]
        cancel_payloads = {a.payload["origClientOrderId"] for a in p.actions[:2]}
        assert cancel_payloads == {"ct-old-stop", "ct-old-tp"}
        close = p.actions[2]
        assert close.payload["quantity"] == "0.05"
        assert close.replaces_client_order_id is None

    def test_flat_without_protection_closes_only(self):
        st = pos(KEY, "0.05")
        t = TargetPosition(KEY, D(0), None, None, 20)
        p = plan(account([st]), [t])
        assert [(a.kind, a.priority) for a in p.actions] == [(ActionKind.MARKET_CLOSE, 2)]


class TestStopReplacement:
    def test_place_new_before_cancel_old(self):
        st = pos(KEY, "0.05", stop=prot("ct-old-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64000), None, 20)
        p = plan(account([st]), [t])
        kinds = [a.kind for a in p.actions]
        assert kinds == [ActionKind.PLACE_STOP, ActionKind.CANCEL_ORDER]
        new_stop, cancel_old = p.actions
        assert new_stop.payload["stopPrice"] == "64000"
        assert new_stop.priority == 1
        assert cancel_old.payload["origClientOrderId"] == "ct-old-stop"
        assert cancel_old.replaces_client_order_id == "ct-old-stop"
        assert cancel_old.priority == 2

    def test_stop_within_tick_noop(self):
        st = pos(KEY, "0.05", stop=prot("ct-old-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64500 + D("0.1")), None, 20)
        p = plan(account([st]), [t])
        assert p.actions == []

    def test_tp_replacement(self):
        st = pos(
            KEY, "0.05", tp=prot("ct-old-tp", 70000, side=Side.BUY, otype="TAKE_PROFIT_MARKET")
        )
        t = TargetPosition(KEY, D("0.05"), None, D(71000), 20)
        p = plan(account([st]), [t])
        kinds = [a.kind for a in p.actions]
        assert kinds == [ActionKind.PLACE_TP, ActionKind.CANCEL_ORDER]
        assert p.actions[0].payload["type"] == "TAKE_PROFIT_MARKET"
        assert p.actions[0].payload["stopPrice"] == "71000"
        assert p.actions[0].payload["side"] == "SELL"


class TestLeverageRule:
    def test_leverage_mismatch_emits_set_leverage_first(self):
        st = pos(KEY, "0.05", lev=10)
        t = TargetPosition(KEY, D("0.05"), None, None, 20)
        p = plan(account([st]), [t])
        assert p.actions[0].kind == ActionKind.SET_LEVERAGE
        assert p.actions[0].priority == 0
        assert p.actions[0].payload == {"symbol": "BTCUSDT", "leverage": "20"}

    def test_no_set_leverage_when_equal(self):
        st = pos(KEY, "0.05", lev=20)
        t = TargetPosition(KEY, D("0.05"), None, None, 20)
        assert plan(account([st]), [t]).actions == []

    def test_new_position_no_set_leverage_action(self):
        p = plan(account(), [TARGET_LONG])
        assert all(a.kind != ActionKind.SET_LEVERAGE for a in p.actions)


class TestHedgeMode:
    LONG_KEY = PositionKey("BTCUSDT", PosSide.LONG)
    SHORT_KEY = PositionKey("BTCUSDT", PosSide.SHORT)

    def test_hedge_open_carries_position_side(self):
        t = TargetPosition(self.LONG_KEY, D("0.05"), D(64500), None, 20)
        p = plan(account(), [t])
        open_a, stop_a = p.actions
        assert open_a.payload["positionSide"] == "LONG"
        assert "reduceOnly" not in open_a.payload
        assert stop_a.payload["positionSide"] == "LONG"
        assert "reduceOnly" not in stop_a.payload
        assert stop_a.payload["side"] == "SELL"  # closes the long leg

    def test_hedge_short_open_sells(self):
        t = TargetPosition(self.SHORT_KEY, D("0.05"), D(65500), None, 20)
        p = plan(account(), [t])
        assert p.actions[0].payload["side"] == "SELL"
        assert p.actions[0].payload["positionSide"] == "SHORT"
        assert p.actions[1].payload["side"] == "BUY"  # protective closes short leg

    def test_hedge_close_no_reduce_only(self):
        st = pos(self.LONG_KEY, "0.05")
        t = TargetPosition(self.LONG_KEY, D(0), None, None, 20)
        p = plan(account([st]), [t])
        close_a = p.actions[-1]
        assert close_a.kind == ActionKind.MARKET_CLOSE
        assert close_a.payload["positionSide"] == "LONG"
        assert close_a.payload["side"] == "SELL"
        assert "reduceOnly" not in close_a.payload

    def test_hedge_partial_reduce_absolute(self):
        st = pos(self.SHORT_KEY, "0.10")
        t = TargetPosition(self.SHORT_KEY, D("0.07"), None, None, 20)
        p = plan(account([st]), [t])
        assert p.actions[0].kind == ActionKind.MARKET_CLOSE
        assert p.actions[0].payload["side"] == "BUY"
        assert p.actions[0].payload["quantity"] == "0.03"
        assert p.actions[0].payload["positionSide"] == "SHORT"


class TestIdempotency:
    def test_matching_state_empty_plan_three_times(self):
        st = pos(KEY, "0.05", stop=prot("ct-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64500), None, 20)
        actual = account([st])
        for _ in range(3):
            p = plan(actual, [t])
            assert p.actions == []

    def test_sign_flip_closes_full(self):
        st = pos(KEY, "0.05")
        t = TargetPosition(KEY, D("-0.02"), D(66000), None, 20)
        p = plan(account([st]), [t])
        assert [a.kind for a in p.actions] == [ActionKind.MARKET_CLOSE]
        assert p.actions[0].payload["quantity"] == "0.05"

    def test_unknown_symbol_skipped(self):
        t = TargetPosition(PositionKey("SOLUSDT"), D("1"), None, None, 20)
        p = plan(account(), [t], flt={"BTCUSDT": filters()})
        assert p.actions == []
