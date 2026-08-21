"""Pure risk engine: "% of equity at risk until stop" -> position quantity.

No I/O. All functions take/return domain types; business-boundary outcomes are
QtyDecision values, never exceptions.
"""

from __future__ import annotations

from decimal import Decimal

from copytradeaster.domain.models import (
    QtyDecision,
    RiskPolicy,
    SkipReason,
    SymbolFilters,
)

ZERO = Decimal(0)
ONE = Decimal(1)
MARGIN_SAFETY = Decimal("0.8")  # required margin may use at most 80% of available equity


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value // step) * step


def entry_quantity(
    policy: RiskPolicy,
    equity_available: Decimal,
    expected_entry: Decimal,
    stop_price: Decimal,
    filters: SymbolFilters,
    fee_buffer_pct: Decimal,
) -> QtyDecision:
    distance = abs(expected_entry - stop_price)
    if distance == 0:
        # stop==entry: division-by-zero guard; treated as "no usable stop"
        return QtyDecision(None, SkipReason.NO_STOP)
    per_unit_loss = distance * (ONE + fee_buffer_pct)
    risk_budget = equity_available * policy.risk_percent
    qty_raw = risk_budget / per_unit_loss
    qty = floor_to_step(qty_raw, filters.step_size)
    if qty < filters.min_qty:
        return QtyDecision(None, SkipReason.BELOW_MIN_QTY)
    if qty * expected_entry < filters.min_notional:
        return QtyDecision(None, SkipReason.BELOW_MIN_NOTIONAL)
    if qty > filters.market_max_qty:
        qty = floor_to_step(filters.market_max_qty, filters.step_size)
        if qty < filters.min_qty:
            return QtyDecision(None, SkipReason.BELOW_MIN_QTY)
    if not margin_check(qty, expected_entry, policy.max_leverage, equity_available):
        return QtyDecision(None, SkipReason.INSUFFICIENT_MARGIN)
    return QtyDecision(qty, None)


def margin_check(qty: Decimal, entry: Decimal, leverage: int, equity_available: Decimal) -> bool:
    required = qty * entry / Decimal(leverage)
    return required <= equity_available * MARGIN_SAFETY


def post_fill_cap(
    policy: RiskPolicy,
    equity_available: Decimal,
    fill_price: Decimal,
    stop_price: Decimal,
    filters: SymbolFilters,
    fee_buffer_pct: Decimal,
) -> Decimal:
    """Maximum quantity allowed after an actual fill (real risk cap)."""
    distance = abs(fill_price - stop_price)
    if distance == 0:
        return ZERO
    risk_budget = equity_available * policy.risk_percent
    per_unit_loss = distance * (ONE + fee_buffer_pct)
    return floor_to_step(risk_budget / per_unit_loss, filters.step_size)


def scaled_reduce(
    actual_qty: Decimal, master_old_abs: Decimal, master_new_abs: Decimal, filters: SymbolFilters
) -> Decimal:
    """Proportional reduction of follower quantity by master's relative change."""
    if master_old_abs == 0:
        return ZERO
    return floor_to_step(actual_qty * master_new_abs / master_old_abs, filters.step_size)


def deviation_exceeded(expected_entry: Decimal, actual_fill: Decimal, policy: RiskPolicy) -> bool:
    """Strictly-greater-than threshold comparison."""
    return abs(actual_fill - expected_entry) / expected_entry > policy.max_entry_deviation_pct
