"""Pure planner: (actual state, target state) -> minimal ExecutionPlan.

No Aster API calls, no I/O. Transition rules are fixed by the stage-1 plan
(docs/ARCHITECTURE.md §6):
1. leverage mismatch -> SET_LEVERAGE (prio 0);
2. increase -> MARKET_OPEN delta (prio 1), then missing/shifted protections;
3. partial reduce -> MARKET_CLOSE delta (prio 1), protections untouched;
4. flat target -> cancel protections (prio 1), then full close (prio 2);
5. stop replacement -> place new (prio 1..2) BEFORE cancel old (prio 2):
   short double-protection window is allowed, unprotected window is not;
6. hedge mode: closing orders carry positionSide, no reduceOnly;
   one-way: reduceOnly=true, no positionSide;
8. actual == target -> empty plan;
9. duplicate protection convergence: every additional bot-owned conditional
   beyond the picked stop/take_profit (left by a FAILED dependent cancel after
   a CONFIRMED replacement) is canceled - one leg keeps exactly ONE stop and
   ONE take-profit.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PositionState,
    PosSide,
    Side,
    SymbolFilters,
    TargetPosition,
    TargetState,
)

DEFAULT_WORKING_TYPE: Final = "MARK_PRICE"
PRICE_PROTECT: Final = "false"


def _hedge(pos_side: PosSide) -> bool:
    return pos_side != PosSide.BOTH


def _open_side(pos_side: PosSide, delta_sign: int) -> Side:
    if pos_side == PosSide.LONG:
        return Side.BUY
    if pos_side == PosSide.SHORT:
        return Side.SELL
    return Side.BUY if delta_sign > 0 else Side.SELL


def _close_side(pos_side: PosSide, qty_sign: int) -> Side:
    if pos_side == PosSide.LONG:
        return Side.SELL
    if pos_side == PosSide.SHORT:
        return Side.BUY
    return Side.SELL if qty_sign > 0 else Side.BUY


def _apply_close_mode(payload: dict[str, str], pos_side: PosSide) -> None:
    """Closing orders: hedge -> positionSide, no reduceOnly; one-way -> reduceOnly."""
    if _hedge(pos_side):
        payload["positionSide"] = pos_side.value
    else:
        payload["reduceOnly"] = "true"


def _apply_open_mode(payload: dict[str, str], pos_side: PosSide) -> None:
    if _hedge(pos_side):
        payload["positionSide"] = pos_side.value


def _order_payload(
    symbol: str,
    side: Side,
    order_type: str,
    quantity: Decimal | None,
    pos_side: PosSide,
    *,
    closing: bool,
    stop_price: Decimal | None = None,
    working_type: str | None = None,
    limit_price: Decimal | None = None,
    close_position: bool = False,
) -> dict[str, str]:
    payload: dict[str, str] = {
        "symbol": symbol,
        "side": side.value,
        "type": order_type,
        "newOrderRespType": "RESULT" if order_type == "MARKET" else "ACK",
    }
    if quantity is not None:
        payload["quantity"] = format(quantity, "f")
    if stop_price is not None:
        payload["stopPrice"] = format(stop_price, "f")
    if limit_price is not None:
        payload["price"] = format(limit_price, "f")
    if working_type is not None:
        payload["workingType"] = working_type
        payload["priceProtect"] = PRICE_PROTECT
    if close_position:
        payload["closePosition"] = "true"
        _apply_open_mode(payload, pos_side)
    elif closing:
        _apply_close_mode(payload, pos_side)
    else:
        _apply_open_mode(payload, pos_side)
    return payload


class _SeqIds:
    def __init__(self, factory: Callable[[ActionKind, str, int], str]) -> None:
        self._factory = factory
        self.seq = 0

    def next(self, kind: ActionKind, symbol: str) -> str:
        self.seq += 1
        return self._factory(kind, symbol, self.seq)


def _qty_equal(a: Decimal, b: Decimal, step: Decimal) -> bool:
    return abs(a - b) < step


def _level_equal(a: Decimal | None, b: Decimal | None, tick: Decimal) -> bool:
    if a is None or b is None:
        return a is None and b is None
    # exchange levels live ON the tick grid: a one-tick move (64500.0 -> 64500.1
    # at tick 0.1) is the smallest real change, not float noise - require grid
    # equality instead of "<= tick"
    if tick <= 0:
        return a == b
    return (a / tick).to_integral_value() == (b / tick).to_integral_value()


def _protection_matches(
    current,
    *,
    order_type: str,
    side: Side,
    level: Decimal,
    quantity: Decimal,
    working_type: str,
    filters: SymbolFilters,
) -> bool:
    """Compare every exchange field that defines protection convergence."""
    if current is None:
        return False
    if not _level_equal(current.stop_price, level, filters.tick_size):
        return False
    if current.type != order_type or current.side != side:
        return False
    if current.working_type != working_type:
        return False
    market_protection = order_type in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    if market_protection:
        # closePosition protects the live remainder, including later partial
        # fills. The REQUEST must omit reduceOnly, but live Aster openOrders /
        # queryOrder normalize the RESPONSE to reduceOnly=true together with
        # closePosition=true; convergence therefore keys on closePosition.
        return current.close_position
    if current.close_position:
        return False
    # Like closePosition orders, Aster normalizes accepted HEDGE fixed
    # conditionals to reduceOnly=true in responses even though the request
    # correctly omitted reduceOnly. positionSide is already represented by
    # the PositionKey used to select this order.
    return _qty_equal(current.orig_qty, quantity, filters.step_size)


def _cancel_action(
    symbol: str,
    prot_order_coid: str,
    priority: int,
    *,
    depends_on_client_order_id: str | None = None,
    pos_side: PosSide = PosSide.BOTH,
) -> ExecutionAction:
    return ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol=symbol,
        payload={"symbol": symbol, "origClientOrderId": prot_order_coid},
        client_order_id=None,
        replaces_client_order_id=prot_order_coid,
        priority=priority,
        depends_on_client_order_id=depends_on_client_order_id,
        position_side=pos_side,
    )


def _close_action(
    symbol: str, pos_side: PosSide, qty_sign: int, quantity: Decimal, ids: _SeqIds, priority: int
) -> ExecutionAction:
    payload = _order_payload(
        symbol,
        _close_side(pos_side, qty_sign),
        "MARKET",
        quantity,
        pos_side,
        closing=True,
    )
    return ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol=symbol,
        payload=payload,
        client_order_id=ids.next(ActionKind.MARKET_CLOSE, symbol),
        replaces_client_order_id=None,
        priority=priority,
    )


def _plan_for_target(
    actual_pos: PositionState | None,
    target: TargetPosition,
    filters: SymbolFilters,
    ids: _SeqIds,
    working_type: str,
) -> list[ExecutionAction]:
    actions: list[ExecutionAction] = []
    symbol = target.key.symbol
    pos_side = target.key.pos_side
    actual_qty = actual_pos.qty if actual_pos is not None else Decimal(0)
    actual_abs = abs(actual_qty)
    target_abs = abs(target.qty)

    # --- rule 4: flat target --------------------------------------------------
    if target.qty == 0:
        # flat target: cancel orphan protection EVEN without a position
        # (zero-qty legs carrying conditionals are not a converged state);
        # duplicate extras follow the same rule as rule 9 below
        if actual_pos is not None:
            for prot in (
                actual_pos.stop,
                actual_pos.take_profit,
                *actual_pos.extra_stops,
                *actual_pos.extra_tps,
            ):
                if prot is not None:
                    actions.append(
                        _cancel_action(symbol, prot.client_order_id, priority=1, pos_side=pos_side)
                    )
            if actual_abs > 0:
                actions.append(
                    _close_action(
                        symbol, pos_side, 1 if actual_qty > 0 else -1, actual_abs, ids, priority=2
                    )
                )
        return actions

    # --- sign flip (one-way): close everything now; new side on next reconcile
    if actual_abs > 0 and not _hedge(pos_side) and actual_qty * target.qty < 0:
        actions.append(
            _close_action(
                symbol, pos_side, 1 if actual_qty > 0 else -1, actual_abs, ids, priority=1
            )
        )
        return actions

    if actual_pos is None:
        actual_pos = PositionState(
            key=target.key,
            qty=Decimal(0),
            entry_price=Decimal(0),
            leverage=target.leverage,
            mark_price=Decimal(0),
            liquidation_price=Decimal(0),
        )

    opened_or_closed = False
    # --- rule 2: increase (state-based: grow toward target magnitude) ---------
    if target_abs - actual_abs >= filters.step_size:
        delta_sign = 1 if target.qty >= 0 else -1
        open_payload = _order_payload(
            symbol,
            _open_side(pos_side, delta_sign),
            "MARKET",
            target_abs - actual_abs,
            pos_side,
            closing=False,
        )
        actions.append(
            ExecutionAction(
                kind=ActionKind.MARKET_OPEN,
                symbol=symbol,
                payload=open_payload,
                client_order_id=ids.next(ActionKind.MARKET_OPEN, symbol),
                replaces_client_order_id=None,
                priority=1,
            )
        )
        opened_or_closed = True
    # --- rule 3: partial reduce -------------------------------------------------
    elif target_abs > 0 and actual_abs - target_abs >= filters.step_size:
        actions.append(
            _close_action(
                symbol,
                pos_side,
                1 if actual_qty > 0 else -1,
                actual_abs - target_abs,
                ids,
                priority=1,
            )
        )
        opened_or_closed = True

    # --- protective orders (rules 2 tail and 5) ---------------------------------
    place_priority = 2 if opened_or_closed else 1
    qty_sign = 1 if (actual_qty if actual_abs > 0 else target.qty) >= 0 else -1
    for kind, wanted_level, current in (
        (ActionKind.PLACE_STOP, target.stop_price, actual_pos.stop),
        (ActionKind.PLACE_TP, target.tp_price, actual_pos.take_profit),
    ):
        if wanted_level is None:
            # target-state semantics: master removed the level -> follower
            # cancels its own; "continue" here would leak stale protection
            if current is not None:
                actions.append(
                    _cancel_action(symbol, current.client_order_id, priority=1, pos_side=pos_side)
                )
            continue
        order_type = target.stop_type if kind == ActionKind.PLACE_STOP else target.tp_type
        close_side = _close_side(pos_side, qty_sign)
        if _protection_matches(
            current,
            order_type=order_type,
            side=close_side,
            level=wanted_level,
            quantity=target_abs,
            working_type=working_type,
            filters=filters,
        ):
            continue
        # limit-flavor conditionals (STOP / TAKE_PROFIT) require an explicit
        # limit price: give the fill a 0.05% buffer beyond the trigger
        limit_price = None
        if order_type in ("STOP", "TAKE_PROFIT"):
            drift = wanted_level * Decimal("0.0005")
            raw_limit = wanted_level - drift if close_side == Side.SELL else wanted_level + drift
            # Decimal.quantize("0.10") keeps two decimals instead of snapping to
            # the 0.1 grid; divide by the tick and multiply back for alignment
            limit_price = (raw_limit / filters.tick_size).to_integral_value() * filters.tick_size
        payload = _order_payload(
            symbol,
            close_side,
            order_type,
            None if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET") else target_abs,
            pos_side,
            closing=order_type in ("STOP", "TAKE_PROFIT"),
            stop_price=wanted_level,
            working_type=working_type,
            limit_price=limit_price,
            close_position=order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"),
        )
        place_action = ExecutionAction(
            kind=kind,
            symbol=symbol,
            payload=payload,
            client_order_id=ids.next(kind, symbol),
            replaces_client_order_id=None,
            priority=place_priority,
        )
        actions.append(place_action)
        if current is not None:
            actions.append(
                _cancel_action(
                    symbol,
                    current.client_order_id,
                    priority=max(place_priority, 2),
                    depends_on_client_order_id=place_action.client_order_id,
                    pos_side=pos_side,
                )
            )

    # --- rule 9: duplicate protection convergence ------------------------------
    # A dependent cancel that FAILED after its replacement was CONFIRMED leaves
    # the previous stop/tp resting next to the new one. The snapshot keeps such
    # orders in extra_stops/extra_tps; without this sweep the next reconcile
    # would see current==target and converge to "no actions" with TWO bot-owned
    # stops resting (live-observed duplicate-stop incident). Cancel them at
    # priority 1: they carry no dependency, so a retry next pass is guaranteed.
    for extras in (actual_pos.extra_stops, actual_pos.extra_tps):
        for extra in extras:
            if extra is None or extra.client_order_id in {
                action.payload.get("origClientOrderId")
                for action in actions
                if action.kind == ActionKind.CANCEL_ORDER
            }:
                continue
            actions.append(
                _cancel_action(symbol, extra.client_order_id, priority=1, pos_side=pos_side)
            )
    return actions


def build_plan(
    actual: AccountState,
    target: TargetState,
    filters_by_symbol: dict[str, SymbolFilters],
    client_order_id_for: Callable[[ActionKind, str, int], str],
    working_type: str = DEFAULT_WORKING_TYPE,
) -> ExecutionPlan:
    ids = _SeqIds(client_order_id_for)
    actions: list[ExecutionAction] = []
    for tp in target.targets:
        filters = filters_by_symbol.get(tp.key.symbol)
        if filters is None:
            continue
        actual_pos = actual.positions.get(tp.key)
        # --- rule 1: leverage ---------------------------------------------------
        if tp.leverage > 0 and (actual_pos is None or actual_pos.leverage != tp.leverage):
            # also for brand-new legs: a ONE_WAY WS-created position starts with
            # projected leverage 0/unknown - the entry must land on the target
            # leverage, not on whatever the account happens to have
            actions.append(
                ExecutionAction(
                    kind=ActionKind.SET_LEVERAGE,
                    symbol=tp.key.symbol,
                    payload={"symbol": tp.key.symbol, "leverage": str(tp.leverage)},
                    client_order_id=None,
                    replaces_client_order_id=None,
                    priority=0,
                )
            )
        actions.extend(_plan_for_target(actual_pos, tp, filters, ids, working_type))
    actions.sort(key=lambda a: a.priority)
    return ExecutionPlan(account_id=target.account_id, trade_id=None, actions=actions)


__all__ = ["build_plan"]
