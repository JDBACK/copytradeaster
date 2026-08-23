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

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("stop_price",)


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

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = (
        "qty",
        "entry_price",
        "mark_price",
        "liquidation_price",
    )


@dataclass(slots=True)
class AccountState(_DecimalStrict):
    account_id: str
    available_usdt: Decimal
    positions: dict[PositionKey, PositionState]
    open_orders: list[dict] = field(default_factory=list)

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("available_usdt",)


@dataclass(frozen=True, slots=True)
class RiskPolicy(_DecimalStrict):
    risk_percent: Decimal
    max_leverage: int
    max_entry_deviation_pct: Decimal
    enabled: bool = True
    allow_new_entries: bool = True

    _DECIMAL_FIELDS: ClassVar[tuple[str, ...]] = ("risk_percent", "max_entry_deviation_pct")


@dataclass(frozen=True, slots=True)
class TargetPosition(_DecimalStrict):
    key: PositionKey
    qty: Decimal  # signed in one-way; absolute in hedge
    stop_price: Decimal | None
    tp_price: Decimal | None
    leverage: int
    stop_type: str = "STOP_MARKET"
    tp_type: str = "TAKE_PROFIT_MARKET"
    # WHY is the target zero. True ONLY when a proportional reduction of the
    # SAME direction rounded down to zero (dust leg): the follower must stay
    # flat until the master episode ends. A TRANSITIONAL zero (master flipped:
    # close this side now, open the other side next pass) must NOT carry it -
    # otherwise zero_target blocks the new-direction entry forever.
    hold_flat_until_episode_end: bool = False

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
    # CANCEL_ORDER payloads intentionally contain only the exchange cancel
    # parameters; retain the leg here for trade/revision attribution.
    position_side: PosSide = PosSide.BOTH


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    account_id: str
    trade_id: int | None
    actions: list[ExecutionAction]


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
    "SkipReason",
    "SymbolFilters",
    "TargetPosition",
    "TargetState",
    "TradingIdentity",
]
