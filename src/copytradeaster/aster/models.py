"""Pydantic DTOs validating exchange responses (Aster V3 field names)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pydantic.alias_generators import to_camel

from copytradeaster.domain.models import PosSide, Side, SymbolFilters


class _Camel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class BalanceDTO(_Camel):
    asset: str
    balance: Decimal
    cross_wallet_balance: Decimal
    cross_un_pnl: Decimal
    available_balance: Decimal
    max_withdraw_amount: Decimal


class _HistoryDTO(_Camel):
    """Exact history facts; unknown fields are intentionally not retained."""

    @field_validator("*", mode="before")
    @classmethod
    def _reject_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("history numbers must be exact")
        return value


class UserTradeDTO(_HistoryDTO):
    trade_id: StrictInt = Field(alias="id", ge=0)
    order_id: StrictInt = Field(ge=0)
    symbol: str = Field(min_length=1)
    side: Side
    position_side: PosSide
    qty: Decimal = Field(gt=0, allow_inf_nan=False)
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    quote_qty: Decimal = Field(ge=0, allow_inf_nan=False)
    realized_pnl: Decimal = Field(allow_inf_nan=False)
    commission: Decimal = Field(allow_inf_nan=False)
    commission_asset: str = Field(min_length=1)
    time: StrictInt = Field(ge=0)


class IncomeDTO(_HistoryDTO):
    symbol: str
    income_type: str = Field(min_length=1)
    income: Decimal = Field(allow_inf_nan=False)
    asset: str = Field(min_length=1)
    time: StrictInt = Field(ge=0)
    tran_id: str = Field(min_length=1)
    trade_id: str

    @field_validator("tran_id", "trade_id", mode="before")
    @classmethod
    def _exact_id(cls, value: object) -> object:
        if type(value) is int and value >= 0:
            return str(value)
        if isinstance(value, str) and (not value or value.isascii() and value.isdigit()):
            return value
        raise ValueError("invalid history id")


class AgentDTO(_Camel):
    """Aster V3 Agent permission record.

    The Agent endpoint is a USER_DATA endpoint and its response is a list.  A
    few fields are deliberately nullable here: an omitted/null critical field
    is a valid wire response but must be rejected by the onboarding validator
    (fail closed), rather than silently receiving a permissive default.
    """

    agent_address: str | None = Field(default=None, alias="agentAddress")
    agent_name: str | None = Field(default=None, alias="agentName")
    source: str | None = None
    expired: int | None = None
    ip_whitelist: str | list[str] | None = Field(default=None, alias="ipWhitelist")
    can_read: bool | None = Field(default=None, alias="canRead")
    can_spot_trade: bool | None = Field(default=None, alias="canSpotTrade")
    can_perp_trade: bool | None = Field(default=None, alias="canPerpTrade")
    can_withdraw: bool | None = Field(default=None, alias="canWithdraw")


class BuilderDTO(_Camel):
    """One Aster V3 approved-Builder record (``GET /fapi/v3/builder``).

    All critical fields are deliberately nullable here: an omitted/null
    ``userAddress``/``builderAddress``/``maxFeeRate`` is a valid wire shape
    but must be rejected by the attestation validator (fail closed), never
    treated as a verified authorization.  ``maxFeeRate`` stays a string —
    fee math never uses float.  The live wire sends it as a JSON number,
    so numeric input is coerced to its shortest round-trip string here
    (``str()``), keeping the downstream Decimal comparison exact.
    """

    user_address: str | None = Field(default=None, alias="userAddress")
    builder_address: str | None = Field(default=None, alias="builderAddress")
    builder_name: str | None = Field(default=None, alias="builderName")
    max_fee_rate: str | None = Field(default=None, alias="maxFeeRate")
    approve_time: int | None = Field(default=None, alias="approveTime")

    @field_validator("max_fee_rate", mode="before")
    @classmethod
    def _coerce_fee_rate(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, bool):
            raise ValueError("invalid maxFeeRate")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError("invalid maxFeeRate")
            return str(value)
        raise ValueError("invalid maxFeeRate")


class LeverageBracketDTO(_Camel):
    """One symbol-specific leverage bracket returned by Aster V3.

    Aster testnet currently returns ``initialLeverage`` values above the
    historical Binance-compatible 125x ceiling (for example 200x).  The
    adapter therefore validates only that the value is a positive integer and
    leaves the exchange's actual maximum authoritative.
    """

    bracket: int | None = None
    initial_leverage: StrictInt = Field(alias="initialLeverage", gt=0)
    notional_cap: Decimal | None = Field(default=None, alias="notionalCap")
    notional_floor: Decimal | None = Field(default=None, alias="notionalFloor")
    maint_margin_ratio: Decimal | None = Field(default=None, alias="maintMarginRatio")
    cum: Decimal | None = None


class LeverageBracketResponse(_Camel):
    """Typed response for ``GET /fapi/v3/leverageBracket?symbol=...``."""

    symbol: str
    brackets: list[LeverageBracketDTO]


class PositionDTO(BaseModel):
    symbol: str
    position_amt: Decimal = Field(alias="positionAmt")
    entry_price: Decimal = Field(alias="entryPrice")
    mark_price: Decimal = Field(alias="markPrice")
    leverage: int
    liquidation_price: Decimal = Field(alias="liquidationPrice")
    margin_type: str = Field(alias="marginType")
    position_side: PosSide = Field(alias="positionSide")
    un_realized_profit: Decimal = Field(alias="unRealizedProfit")


class OrderDTO(BaseModel):
    order_id: int = Field(alias="orderId")
    client_order_id: str = Field(alias="clientOrderId")
    symbol: str
    side: Side
    type: str
    status: str
    orig_qty: Decimal = Field(alias="origQty")
    executed_qty: Decimal = Field(alias="executedQty")
    avg_price: Decimal = Field(alias="avgPrice")
    price: Decimal
    stop_price: Decimal = Field(alias="stopPrice")
    reduce_only: bool = Field(alias="reduceOnly")
    close_position: bool = Field(alias="closePosition")
    position_side: PosSide = Field(alias="positionSide")
    working_type: str = Field(alias="workingType")
    time_in_force: str = Field(alias="timeInForce")
    update_time: int = Field(alias="updateTime")


class RateLimitDTO(BaseModel):
    rateLimitType: str
    interval: str
    intervalNum: int
    limit: int


class SymbolFilterDTO(BaseModel):
    filterType: str
    minQty: Decimal | None = None
    maxQty: Decimal | None = None
    stepSize: Decimal | None = None
    minPrice: Decimal | None = None
    maxPrice: Decimal | None = None
    tickSize: Decimal | None = None
    notional: Decimal | None = None


class SymbolInfoDTO(BaseModel):
    symbol: str
    baseAsset: str | None = None
    quoteAsset: str | None = None
    marginAsset: str | None = None
    status: str
    contractType: str
    # live testnet returns a decimal string (e.g. "0.0200"); mainnet may return bool
    triggerProtect: Decimal | str | bool = False
    filters: list[SymbolFilterDTO]
    orderTypes: list[str] = []


class ExchangeInfo(BaseModel):
    symbols: list[SymbolInfoDTO]
    rateLimits: list[RateLimitDTO] = []

    def tradable_filters(self) -> dict[str, SymbolFilters]:
        """symbol -> domain SymbolFilters for active perpetual symbols."""
        out: dict[str, SymbolFilters] = {}
        for s in self.symbols:
            if s.status != "TRADING" or s.contractType != "PERPETUAL":
                continue
            by_type = {f.filterType: f for f in s.filters}
            lot = by_type.get("LOT_SIZE")
            market_lot = by_type.get("MARKET_LOT_SIZE", lot)
            price = by_type.get("PRICE_FILTER")
            notional = by_type.get("MIN_NOTIONAL")
            if not (lot and price):
                continue
            out[s.symbol] = SymbolFilters(
                symbol=s.symbol,
                tick_size=price.tickSize or Decimal(0),
                step_size=lot.stepSize or Decimal(0),
                min_qty=lot.minQty or Decimal(0),
                max_qty=lot.maxQty or Decimal(0),
                market_max_qty=(market_lot.maxQty if market_lot else lot.maxQty) or Decimal(0),
                min_notional=(notional.notional if notional else Decimal(0)),
            )
        return out


__all__ = [
    "AgentDTO",
    "BalanceDTO",
    "BuilderDTO",
    "ExchangeInfo",
    "IncomeDTO",
    "LeverageBracketDTO",
    "LeverageBracketResponse",
    "OrderDTO",
    "PositionDTO",
    "RateLimitDTO",
    "SymbolFilterDTO",
    "UserTradeDTO",
    "SymbolInfoDTO",
]
