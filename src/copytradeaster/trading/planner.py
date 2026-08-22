"""Pure planner: (actual state, target state) -> minimal ExecutionPlan.

No Aster API calls, no I/O. Transition rules are fixed by the stage-1 plan
(ARCHITECTURE.md §6):
1. leverage mismatch -> SET_LEVERAGE (prio 0);
2. increase -> MARKET_OPEN delta (prio 1), then missing/shifted protections;
3. partial reduce -> MARKET_CLOSE delta (prio 1), protections untouched;
4. flat target -> cancel protections (prio 1), then full close (prio 2);
5. stop replacement -> place new (prio 1..2) BEFORE cancel old (prio 2):
   short double-protection window is allowed, unprotected window is not;
6. hedge mode: closing orders carry positionSide, no reduceOnly;
   one-way: reduceOnly=true, no positionSide;
8. actual == target -> empty plan.
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
    if working_type is not None:
        payload["workingType"] = working_type
        payload["priceProtect"] = PRICE_PROTECT
    if closing:
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
    return abs(a - b) <= tick


def _cancel_action(symbol: str, prot_order_coid: str, priority: int) -> ExecutionAction:
    return ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol=symbol,
        payload={"symbol": symbol, "origClientOrderId": prot_order_coid},
        client_order_id=None,
        replaces_client_order_id=prot_order_coid,
        priority=priority,
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
        if actual_pos is None or actual_abs == 0:
            return []
        for prot in (actual_pos.stop, actual_pos.take_profit):
            if prot is not None:
                actions.append(_cancel_action(symbol, prot.client_order_id, priority=1))
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
            continue
        order_type = target.stop_type if kind == ActionKind.PLACE_STOP else target.tp_type
        current_level = current.stop_price if current is not None else None
        if _level_equal(current_level, wanted_level, filters.tick_size):
            continue
        payload = _order_payload(
            symbol,
            _close_side(pos_side, qty_sign),
            order_type,
            target_abs,
            pos_side,
            closing=True,
            stop_price=wanted_level,
            working_type=working_type,
        )
        actions.append(
            ExecutionAction(
                kind=kind,
                symbol=symbol,
                payload=payload,
                client_order_id=ids.next(kind, symbol),
                replaces_client_order_id=None,
                priority=place_priority,
            )
        )
        if current is not None:
            actions.append(
                _cancel_action(symbol, current.client_order_id, priority=max(place_priority, 2))
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
        if actual_pos is not None and actual_pos.leverage != tp.leverage:
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
