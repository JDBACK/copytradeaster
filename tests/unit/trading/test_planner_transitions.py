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


def account(positions=None, symbol_leverages=None) -> AccountState:
    plist = list(positions or [])
    return AccountState(
        account_id="copyc1",
        available_usdt=D(1000),
        positions={p.key: p for p in plist},
        symbol_leverages=dict(symbol_leverages or {}),
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


def prot(
    coid,
    stop_price,
    side=Side.SELL,
    otype="STOP_MARKET",
    *,
    qty="0.05",
    close_position=None,
    working_type="MARK_PRICE",
) -> ProtectiveOrder:
    if close_position is None:
        close_position = otype in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    return ProtectiveOrder(
        client_order_id=coid,
        order_id=1,
        type=otype,
        side=side,
        stop_price=D(stop_price),
        reduce_only=not close_position,
        close_position=close_position,
        working_type=working_type,
        orig_qty=D(qty),
    )


KEY = PositionKey("BTCUSDT")
TARGET_LONG = TargetPosition(
    key=KEY, qty=D("0.05"), stop_price=D(64500), tp_price=None, leverage=20
)


def plan(actual, targets, flt=None):
    flt = flt or {"BTCUSDT": filters()}
    return build_plan(actual, TargetState("copyc1", targets), flt, coid_factory)


def test_margin_mode_precedes_leverage_and_open():
    actual = account(symbol_leverages={"BTCUSDT": 20})
    actual.symbol_margin_types["BTCUSDT"] = "CROSSED"
    target = TargetPosition(KEY, D("0.05"), D(64500), None, 12, margin_type="ISOLATED")
    actions = plan(actual, [target]).actions
    assert [action.kind for action in actions] == [
        ActionKind.SET_MARGIN_TYPE,
        ActionKind.SET_LEVERAGE,
        ActionKind.MARKET_OPEN,
        ActionKind.PLACE_STOP,
    ]
    assert actions[1].depends_on_action_id == actions[0].action_id
    assert actions[2].depends_on_action_id == actions[1].action_id
    assert actions[3].depends_on_action_id == actions[1].action_id


def test_margin_mode_mismatch_while_open_blocks_growth_but_preserves_stop():
    held = pos(KEY, "0.03", stop=prot("ct-old-stop", 64500))
    actual = account([held], symbol_leverages={"BTCUSDT": 20})
    actual.symbol_margin_types["BTCUSDT"] = "CROSSED"
    target = TargetPosition(KEY, D("0.05"), D(64000), None, 20, margin_type="ISOLATED")
    actions = plan(actual, [target]).actions
    assert all(
        action.kind not in {ActionKind.SET_MARGIN_TYPE, ActionKind.MARKET_OPEN}
        for action in actions
    )
    assert any(action.kind == ActionKind.PLACE_STOP for action in actions)


class TestOpenRules:
    def test_open_with_stop(self):
        p = plan(account(), [TARGET_LONG])
        kinds = [(a.kind, a.priority) for a in p.actions]
        # brand-new leg must land on the TARGET leverage (ONE_WAY WS-created
        # positions start with projected leverage 0)
        assert kinds == [
            (ActionKind.SET_LEVERAGE, 0),
            (ActionKind.MARKET_OPEN, 1),
            (ActionKind.PLACE_STOP, 2),
        ]
        open_action = p.actions[1]
        assert open_action.payload == {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.05",
            "newOrderRespType": "RESULT",
        }
        assert open_action.client_order_id == "ct-market_open-BTCUSDT-1"
        stop_action = p.actions[2]
        assert stop_action.payload["type"] == "STOP_MARKET"
        assert stop_action.payload["stopPrice"] == "64500"
        assert "quantity" not in stop_action.payload
        assert "reduceOnly" not in stop_action.payload
        assert stop_action.payload["closePosition"] == "true"
        assert stop_action.payload["workingType"] == "MARK_PRICE"

    def test_short_open_side_sell(self):
        t = TargetPosition(KEY, D("-0.05"), D(65500), None, 20)
        p = plan(account(), [t])
        assert [a.kind for a in p.actions] == [
            ActionKind.SET_LEVERAGE,
            ActionKind.MARKET_OPEN,
            ActionKind.PLACE_STOP,
        ]
        assert p.actions[1].payload["side"] == "SELL"

    def test_delta_smaller_than_step_is_noop_for_size(self):
        existing = pos(KEY, "0.0500")
        t = TargetPosition(KEY, D("0.05009"), D(64500), None, 20)
        p = plan(account([existing]), [t])
        assert [a.kind for a in p.actions] == [ActionKind.PLACE_STOP]

    def test_increase_existing_position(self):
        st = pos(
            KEY,
            "0.02",
            stop=prot("ct-old-stop", 64500, qty="0.02", close_position=False),
        )
        t = TargetPosition(KEY, D("0.05"), D(64500), None, 20)
        p = plan(account([st]), [t])
        assert [(a.kind, a.priority) for a in p.actions] == [
            (ActionKind.MARKET_OPEN, 1),
            (ActionKind.PLACE_STOP, 2),
            (ActionKind.CANCEL_ORDER, 2),
        ]
        assert p.actions[1].payload["closePosition"] == "true"

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
        assert cancel_old.depends_on_client_order_id == new_stop.client_order_id

    def test_one_tick_move_replaces_stop(self):
        """A one-tick level change is the smallest REAL move on the exchange
        grid - it must replace the stop, not be swallowed as noise."""
        st = pos(KEY, "0.05", stop=prot("ct-old-stop", 64500))
        t = TargetPosition(KEY, D("0.05"), D(64500 + D("0.1")), None, 20)
        p = plan(account([st]), [t])
        kinds = [a.kind for a in p.actions]
        assert ActionKind.PLACE_STOP in kinds
        assert kinds.index(ActionKind.PLACE_STOP) < kinds.index(ActionKind.CANCEL_ORDER)

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

    def test_fixed_limit_stop_same_level_wrong_qty_is_replaced(self):
        st = pos(
            KEY,
            "0.02",
            stop=prot("ct-old-stop", 64500, otype="STOP", qty="0.02"),
        )
        target = TargetPosition(
            KEY,
            D("0.05"),
            D(64500),
            None,
            20,
            stop_type="STOP",
        )
        p = plan(account([st]), [target])
        kinds = [a.kind for a in p.actions]
        assert kinds == [ActionKind.MARKET_OPEN, ActionKind.PLACE_STOP, ActionKind.CANCEL_ORDER]
        assert p.actions[1].payload["quantity"] == "0.05"
        assert p.actions[2].depends_on_client_order_id == p.actions[1].client_order_id

    def test_fixed_limit_stop_partial_reduce_resizes_to_remainder(self):
        st = pos(
            KEY,
            "0.10",
            stop=prot("ct-old-stop", 64500, otype="STOP", qty="0.10"),
        )
        target = TargetPosition(
            KEY,
            D("0.07"),
            D(64500),
            None,
            20,
            stop_type="STOP",
        )
        p = plan(account([st]), [target])
        assert [a.kind for a in p.actions] == [
            ActionKind.MARKET_CLOSE,
            ActionKind.PLACE_STOP,
            ActionKind.CANCEL_ORDER,
        ]
        assert p.actions[1].payload["quantity"] == "0.07"

    def test_working_type_mismatch_replaces_protection(self):
        st = pos(
            KEY,
            "0.05",
            stop=prot("ct-old-stop", 64500, working_type="CONTRACT_PRICE"),
        )
        p = plan(account([st]), [TARGET_LONG])
        assert [a.kind for a in p.actions] == [ActionKind.PLACE_STOP, ActionKind.CANCEL_ORDER]

    def test_market_protection_payload_is_close_position_in_hedge(self):
        key = PositionKey("BTCUSDT", PosSide.LONG)
        target = TargetPosition(key, D("0.05"), D(64500), None, 20)
        stop = next(a for a in plan(account(), [target]).actions if a.kind == ActionKind.PLACE_STOP)
        assert stop.payload["closePosition"] == "true"
        assert stop.payload["positionSide"] == "LONG"
        assert "quantity" not in stop.payload
        assert "reduceOnly" not in stop.payload


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

    def test_new_position_sets_target_leverage(self):
        """ONE_WAY WS-created positions start with projected leverage 0 - the
        entry must carry SET_LEVERAGE to land on the target leverage."""
        p = plan(account(), [TARGET_LONG])
        assert any(
            a.kind == ActionKind.SET_LEVERAGE and a.payload["leverage"] == "20" for a in p.actions
        )

    def test_zero_target_leverage_emits_nothing(self):
        t = TargetPosition(KEY, D("0.05"), D(64500), None, 0)
        assert all(a.kind != ActionKind.SET_LEVERAGE for a in plan(account(), [t]).actions)

    def test_preconfigured_flat_symbol_skips_set_leverage(self):
        """positionRisk keeps reporting the exchange-persisted leverage for
        flat symbols; when it already matches the target, a brand-new entry
        must NOT pay a redundant SET_LEVERAGE."""
        p = plan(account(symbol_leverages={"BTCUSDT": 20}), [TARGET_LONG])
        kinds = [a.kind for a in p.actions]
        assert ActionKind.SET_LEVERAGE not in kinds
        assert ActionKind.MARKET_OPEN in kinds

    def test_flat_symbol_wrong_leverage_still_sets(self):
        actual = account(symbol_leverages={"BTCUSDT": 10})
        actions = plan(actual, [TARGET_LONG]).actions
        assert actions[0].kind == ActionKind.SET_LEVERAGE
        assert actions[0].payload["leverage"] == "20"

    def test_set_leverage_wires_dependency_ids_to_entry_and_protection(self):
        actual = account(symbol_leverages={"BTCUSDT": 10})
        actions = plan(actual, [TARGET_LONG]).actions
        by_kind = {a.kind: a for a in actions}
        set_action = by_kind[ActionKind.SET_LEVERAGE]
        assert set_action.action_id == "leverage:BTCUSDT"
        assert set_action.depends_on_action_id is None
        assert by_kind[ActionKind.MARKET_OPEN].depends_on_action_id == "leverage:BTCUSDT"
        assert by_kind[ActionKind.PLACE_STOP].depends_on_action_id == "leverage:BTCUSDT"

    def test_close_targets_never_carry_dependency(self):
        st = pos(KEY, "0.05", lev=20)
        close = TargetPosition(KEY, D("0"), None, None, 20)
        p = plan(account([st]), [close])
        assert all(
            a.kind != ActionKind.SET_LEVERAGE and a.depends_on_action_id is None for a in p.actions
        )


class TestHedgeMode:
    LONG_KEY = PositionKey("BTCUSDT", PosSide.LONG)
    SHORT_KEY = PositionKey("BTCUSDT", PosSide.SHORT)

    def test_hedge_open_carries_position_side(self):
        t = TargetPosition(self.LONG_KEY, D("0.05"), D(64500), None, 20)
        p = plan(account(), [t])
        lev_a, open_a, stop_a = p.actions
        assert lev_a.kind == ActionKind.SET_LEVERAGE
        assert open_a.payload["positionSide"] == "LONG"
        assert "reduceOnly" not in open_a.payload
        assert stop_a.payload["positionSide"] == "LONG"
        assert "reduceOnly" not in stop_a.payload
        assert stop_a.payload["side"] == "SELL"  # closes the long leg

    def test_hedge_short_open_sells(self):
        t = TargetPosition(self.SHORT_KEY, D("0.05"), D(65500), None, 20)
        lev_a, open_a, stop_a = plan(account(), [t]).actions
        assert open_a.payload["side"] == "SELL"
        assert open_a.payload["positionSide"] == "SHORT"
        assert stop_a.payload["side"] == "BUY"  # protective closes short leg

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


class TestDuplicateProtectionConvergence:
    """Rule 9: a FAILED dependent cancel leaves the old protection resting next
    to the new one; the next reconcile must remove the stale order even though
    current==target produced an empty plan before the fix."""

    def test_extra_stale_stop_canceled_even_when_target_matches(self):
        st = pos(KEY, "0.05", stop=prot("ct-new-stop", 64000))
        st.extra_stops = (prot("ct-old-stop", 64500),)
        t = TargetPosition(KEY, D("0.05"), D(64000), None, 20)
        p = plan(account([st]), [t])
        assert [(a.kind, a.priority) for a in p.actions] == [(ActionKind.CANCEL_ORDER, 1)]
        assert p.actions[0].payload["origClientOrderId"] == "ct-old-stop"

    def test_extras_canceled_alongside_normal_replacement_without_double_cancel(self):
        st = pos(KEY, "0.05", stop=prot("ct-newest-stop", 63500))
        st.extra_stops = (prot("ct-stale-1", 64500), prot("ct-stale-2", 65000))
        t = TargetPosition(KEY, D("0.05"), D(63000), None, 20)
        p = plan(account([st]), [t])
        places = [a for a in p.actions if a.kind == ActionKind.PLACE_STOP]
        assert len(places) == 1 and places[0].payload["stopPrice"] == "63000"
        cancels = [a for a in p.actions if a.kind == ActionKind.CANCEL_ORDER]
        # picked stop's cancel + both stale extras
        assert {a.payload["origClientOrderId"] for a in cancels} == {
            "ct-newest-stop",
            "ct-stale-1",
            "ct-stale-2",
        }
        # only the PICKED stop's cancel waits for the new place; extras are free
        assert sum(1 for a in cancels if a.depends_on_client_order_id is not None) == 1

    def test_extra_tp_canceled_even_when_target_matches(self):
        tp = prot("ct-new-tp", 70000, side=Side.SELL, otype="TAKE_PROFIT_MARKET")
        st = pos(KEY, "0.05", tp=tp)
        st.extra_tps = (prot("ct-old-tp", 71000, side=Side.SELL, otype="TAKE_PROFIT_MARKET"),)
        t = TargetPosition(KEY, D("0.05"), None, D(70000), 20)
        p = plan(account([st]), [t])
        assert [(a.kind, a.priority) for a in p.actions] == [(ActionKind.CANCEL_ORDER, 1)]
        assert p.actions[0].payload["origClientOrderId"] == "ct-old-tp"

    def test_flat_leg_cancels_picked_and_extra_protection(self):
        st = pos(KEY, "0")
        st.extra_stops = (prot("ct-orphan-stop-b", 64500),)
        st.take_profit = prot("ct-orphan-tp", 70000, side=Side.BUY, otype="TAKE_PROFIT_MARKET")
        st.extra_tps = (prot("ct-orphan-tp-b", 70500, side=Side.BUY, otype="TAKE_PROFIT_MARKET"),)
        t = TargetPosition(KEY, D(0), None, None, 20)
        p = plan(account([st]), [t])
        cancels = {
            a.payload["origClientOrderId"] for a in p.actions if a.kind == ActionKind.CANCEL_ORDER
        }
        assert cancels == {"ct-orphan-stop-b", "ct-orphan-tp", "ct-orphan-tp-b"}
