"""Internal domain models. stdlib dataclasses only; all money values are Decimal.

Invariant enforced at construction: float is rejected for Decimal fields
(silent precision loss is a financial bug); str/int are coerced to Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PosSide(StrEnum):
    BOTH = "BOTH"  # one-way mode
    LONG = "LONG"
    SHORT = "SHORT"


class ActionKind(StrEnum):
    SET_LEVERAGE = "SET_LEVERAGE"
    MARKET_OPEN = "MARKET_OPEN"
    MARKET_CLOSE = "MARKET_CLOSE"
    PLACE_STOP = "PLACE_STOP"
    PLACE_TP = "PLACE_TP"
    CANCEL_ORDER = "CANCEL_ORDER"


class SkipReason(StrEnum):
    NO_STOP = "NO_STOP"
    BELOW_MIN_QTY = "BELOW_MIN_QTY"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    ENTRY_DEVIATION = "ENTRY_DEVIATION"
    DISABLED = "DISABLED"
    PAUSED = "PAUSED"
    STALE_MASTER = "STALE_MASTER"
    OWN_PROTECTIVE_FILLED = "OWN_PROTECTIVE_FILLED"
    INTENTIONAL_FLAT = "INTENTIONAL_FLAT"  # dust target=0 inside an open episode
    LEVERAGE_NOT_READY = "LEVERAGE_NOT_READY"


class ZeroReason(StrEnum):
    """WHY a TargetPosition quantity is zero (explicit per PM-B5).

    ONLY ``PROPORTIONAL_REDUCTION_DUST`` latches ``hold_flat_until_episode_end``
    (the follower must stay flat until the master episode ends). Every other
    zero is transient or terminal and MUST allow same-episode re-entry once
    its cause clears: risk-cap / stop-move recomputes, flip closes (the
    reversed leg opens next pass), master-closed legs, and safety flattens
    (naked unprotected legs, unprotected fresh entries, liquidation-vs-stop
    violations).
    """

    PROPORTIONAL_REDUCTION_DUST = "PROPORTIONAL_REDUCTION_DUST"
    RISK_CAP = "RISK_CAP"
    STOP_MOVE = "STOP_MOVE"
    FLIP_CLOSE = "FLIP_CLOSE"
    MASTER_CLOSED = "MASTER_CLOSED"
    SAFETY_NAKED = "SAFETY_NAKED"
    SAFETY_UNPROTECTED_ENTRY = "SAFETY_UNPROTECTED_ENTRY"
    LIQUIDATION_VIOLATION = "LIQUIDATION_VIOLATION"


class _DecimalStrict:
    """Coerces declared Decimal fields from str/int; rejects float."""

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        for name in self._DECIMAL_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, float):
                raise TypeError(
                    f"{type(self).__name__}.{name}: float is forbidden, use str/int/Decimal"
                )
            if not isinstance(value, Decimal):
                object.__setattr__(self, name, Decimal(value))


@dataclass(frozen=True, slots=True)
class PositionKey:
    symbol: str
    pos_side: PosSide = PosSide.BOTH


@dataclass(frozen=True, slots=True)
class ProtectiveOrder(_DecimalStrict):
    client_order_id: str
    order_id: int
    type: str
    side: Side
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    working_type: str
    # Fixed-quantity STOP/TP orders protect only this amount. Exchange
    # closePosition orders normally report zero here because their coverage is
    # evaluated against the live position at trigger time.
    orig_qty: Decimal = Decimal(0)

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("stop_price", "orig_qty")


@dataclass(slots=True)
class PositionState(_DecimalStrict):
    key: PositionKey
    qty: Decimal  # signed in one-way (positive=long); absolute in hedge
    entry_price: Decimal
    leverage: int
    mark_price: Decimal
    liquidation_price: Decimal
    stop: ProtectiveOrder | None = None
    take_profit: ProtectiveOrder | None = None
    # Additional bot-visible conditional orders of the same slot found on the
    # exchange BEYOND the picked (newest) stop/take_profit. A failed dependent
    # CANCEL after a successful replacement leaves exactly this shape; the
    # planner cancels them so one leg converges to ONE stop and ONE TP.
    # Items are exchange order DTOs (duck-typed: client_order_id/update_time),
    # mirroring how snapshot code stores `stop`/`take_profit`.
    extra_stops: tuple[ProtectiveOrder, ...] = ()
    extra_tps: tuple[ProtectiveOrder, ...] = ()
    unrealized_profit: Decimal = Decimal(0)

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = (
        "qty",
        "entry_price",
        "mark_price",
        "liquidation_price",
        "unrealized_profit",
    )


@dataclass(slots=True)
class AccountState(_DecimalStrict):
    account_id: str
    available_usdt: Decimal
    positions: dict[PositionKey, PositionState]
    open_orders: list[dict] = field(default_factory=list)
    # Exchange-configured leverage per symbol as reported by the authoritative
    # positionRisk read (flat symbols included). Recorded only when every row
    # of the symbol agrees; an absent entry means "unknown", never "unset".
    # Appended LAST so historical positional constructions keep their meaning.
    symbol_leverages: dict[str, int] = field(default_factory=dict)

    equity_usdt: Decimal | None = None

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("available_usdt", "equity_usdt")


@dataclass(frozen=True, slots=True)
class RiskPolicy(_DecimalStrict):
    risk_percent: Decimal
    max_leverage: int
    max_entry_deviation_pct: Decimal
    enabled: bool = True
    allow_new_entries: bool = True

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("risk_percent", "max_entry_deviation_pct")

    def __post_init__(self) -> None:
        _DecimalStrict.__post_init__(self)
        if not Decimal(0) < self.risk_percent <= Decimal(1):
            raise ValueError("RiskPolicy.risk_percent must be in (0, 1]")
        if self.max_leverage <= 0:
            raise ValueError("RiskPolicy.max_leverage must be positive")
        if not Decimal(0) <= self.max_entry_deviation_pct <= Decimal(1):
            raise ValueError("RiskPolicy.max_entry_deviation_pct must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class TargetPosition(_DecimalStrict):
    key: PositionKey
    qty: Decimal  # signed in one-way; absolute in hedge
    stop_price: Decimal | None
    tp_price: Decimal | None
    leverage: int
    stop_type: str = "STOP_MARKET"
    tp_type: str = "TAKE_PROFIT_MARKET"
    hold_flat_until_episode_end: bool = False
    # Explicit zero taxonomy (PM-B5). Set on EVERY zero-qty target; None only
    # for non-zero targets. _persist_refs latches the sticky marker ONLY when
    # this equals PROPORTIONAL_REDUCTION_DUST.
    zero_reason: ZeroReason | None = None

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("qty", "stop_price", "tp_price")


@dataclass(frozen=True, slots=True)
class TargetState:
    account_id: str
    targets: list[TargetPosition]


@dataclass(frozen=True, slots=True)
class ExecutionAction:
    kind: ActionKind
    symbol: str
    payload: dict[str, str]
    client_order_id: str | None
    replaces_client_order_id: str | None
    priority: int
    trade_id: int | None = None  # episode this action belongs to (overrides plan.trade_id)
    revision: int = 0  # desired-state transition revision (idempotency identity)
    # Planner-only stable id used to gate a dependent action inside one plan.
    # For protection replacement, CANCEL_ORDER must not run unless the new
    # PLACE_STOP/PLACE_TP was confirmed by the exchange.
    depends_on_client_order_id: str | None = None
    # Planner-only stable ids for intra-plan prerequisites that are not
    # orders: a FAILED/missing prerequisite blocks the dependent action while
    # risk-reducing actions (closes, cancels) carry no dependency.
    action_id: str | None = None
    depends_on_action_id: str | None = None
    # CANCEL_ORDER payloads intentionally contain only the exchange cancel
    # parameters; retain the leg here for trade/revision attribution.
    position_side: PosSide = PosSide.BOTH


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    account_id: str
    trade_id: int | None
    actions: list[ExecutionAction]
    # Persisted logical operation generation. Used by operator workflows such
    # as emergency-close-all to distinguish two valid identical invocations.
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class TradingIdentity:
    account_id: str
    user: str
    signer: str
    chain_id: int


@dataclass(frozen=True, slots=True)
class SymbolFilters(_DecimalStrict):
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = (
        "tick_size",
        "step_size",
        "min_qty",
        "max_qty",
        "market_max_qty",
        "min_notional",
    )


@dataclass(slots=True)
class QtyDecision(_DecimalStrict):
    """Exactly one of qty / reason is not None."""

    qty: Decimal | None
    reason: SkipReason | None

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("qty",)

    def __post_init__(self) -> None:
        # Explicit base call: zero-arg super() is broken inside classes
        # regenerated by dataclass(slots=True).
        _DecimalStrict.__post_init__(self)
        if (self.qty is None) == (self.reason is None):
            raise ValueError("QtyDecision: exactly one of qty/reason must be set")


__all__ = [
    "AccountState",
    "ActionKind",
    "ExecutionAction",
    "ExecutionPlan",
    "PosSide",
    "PositionKey",
    "PositionState",
    "ProtectiveOrder",
    "QtyDecision",
    "RiskPolicy",
    "Side",
    "TargetPosition",
    "TargetState",
    "TradingIdentity",
    "ZeroReason",
]
