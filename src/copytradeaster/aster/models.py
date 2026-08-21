"""Pydantic DTOs validating exchange responses (Aster V3 field names)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
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
    "BalanceDTO",
    "ExchangeInfo",
    "OrderDTO",
    "PositionDTO",
    "RateLimitDTO",
    "SymbolFilterDTO",
    "SymbolInfoDTO",
]
