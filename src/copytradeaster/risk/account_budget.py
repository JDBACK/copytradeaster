"""Conservative reservations for one account generation; no netting HEDGE legs."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from copytradeaster.domain.models import (
    AccountState,
    PositionKey,
    PositionState,
    SymbolFilters,
    TargetPosition,
)
from copytradeaster.risk.engine import MARGIN_SAFETY, floor_to_step

ZERO = Decimal(0)


def reserve_account_budget(
    actual: AccountState,
    targets: list[TargetPosition],
    master_positions: dict[PositionKey, PositionState],
    filters: dict[str, SymbolFilters],
    *,
    max_total_risk_pct: Decimal,
    fee_buffer_pct: Decimal,
) -> list[TargetPosition]:
    """Reserve all increments together; unsettled reductions release no budget.

    Existing positions consume mark-to-stop risk, with no hedge/PnL offsets.
    Missing protection blocks increases until a later authoritative snapshot.
    Fees are a fraction of notional. This is a stop-loss budget, not a guarantee
    against gaps or an exchange cross-margin liquidation calculator.
    """
    equity = actual.equity_usdt if actual.equity_usdt is not None else actual.available_usdt
    risk_room = max(ZERO, equity * max_total_risk_pct)
    margin_room = max(ZERO, actual.available_usdt * MARGIN_SAFETY)
    by_key = {t.key: t for t in targets}
    leverage: dict[str, int] = {}
    for target in targets:
        if target.qty and target.leverage > 0:
            symbol = target.key.symbol
            leverage[symbol] = min(leverage.get(symbol, target.leverage), target.leverage)
    for key, position in actual.positions.items():
        if not position.qty:
            continue
        price = position.mark_price or position.entry_price
        current_stop = position.stop.stop_price if position.stop else None
        target = by_key.get(key)
        wanted_stop = target.stop_price if target is not None else current_stop
        if price <= 0 or current_stop is None or current_stop <= 0:
            risk_room = ZERO
        else:
            # A planned tighter stop cannot free capacity before it is confirmed.
            distance = max(
                abs(price - stop) for stop in (current_stop, wanted_stop) if stop is not None
            )
            risk_room -= abs(position.qty) * (distance + price * fee_buffer_pct)
        desired = leverage.get(key.symbol, position.leverage)
        if desired > 0 and position.leverage > 0:
            margin_room -= (
                abs(position.qty)
                * price
                * max(ZERO, Decimal(1) / desired - Decimal(1) / position.leverage)
            )
        else:
            margin_room = ZERO
    result: dict[PositionKey, TargetPosition] = {}
    for original in sorted(targets, key=lambda t: (t.key.symbol, t.key.pos_side.value)):
        target = replace(original, leverage=leverage.get(original.key.symbol, original.leverage))
        current = actual.positions.get(target.key)
        current_qty = current.qty if current is not None else ZERO
        increase = abs(target.qty) - abs(current_qty)
        if increase <= 0 or current_qty * target.qty < 0:
            result[target.key] = target
            continue
        master = master_positions.get(target.key)
        price = (master.mark_price or master.entry_price) if master is not None else ZERO
        symbol_filter = filters.get(target.key.symbol)
        allowed = ZERO
        if price > 0 and target.stop_price is not None and target.leverage > 0 and symbol_filter:
            unit_risk = abs(price - target.stop_price) + price * fee_buffer_pct
            unit_margin = price / target.leverage
            if unit_risk > 0:
                allowed = floor_to_step(
                    max(ZERO, min(increase, risk_room / unit_risk, margin_room / unit_margin)),
                    symbol_filter.step_size,
                )
                if allowed < symbol_filter.min_qty or allowed * price < symbol_filter.min_notional:
                    allowed = ZERO
                risk_room -= allowed * unit_risk
                margin_room -= allowed * unit_margin
        sign = Decimal(-1) if target.qty < 0 else Decimal(1)
        result[target.key] = replace(target, qty=sign * (abs(current_qty) + allowed))
    return [result[t.key] for t in targets]
