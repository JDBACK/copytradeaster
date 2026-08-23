"""Local exchange adapter for explicitly marked SHADOW follower accounts.

This adapter is intentionally separate from :class:`AsterClient`: it has no
credentials, no HTTP transport, and no signing path.  It implements the small
exchange surface consumed by the coordinator/executor so a load run can use
the real SQLite journal and trading pipeline for many logical followers while
only the configured REAL accounts touch testnet.

The adapter is not a production execution mode.  App enables it only when
``CT_SHADOW_ACCOUNTS_ENABLED=true`` and the configured REST endpoint is the
testnet endpoint.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.aster.models import BalanceDTO, ExchangeInfo, OrderDTO, PositionDTO
from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
from copytradeaster.domain.models import PosSide, Side


@dataclass(slots=True)
class _ShadowOrder:
    account_id: str
    symbol: str
    side: Side
    type: str
    orig_qty: Decimal
    price: Decimal
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    position_side: PosSide
    working_type: str
    client_order_id: str
    order_id: int
    status: str = "NEW"
    executed_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    update_time: int = 0

    def dto(self) -> OrderDTO:
        return OrderDTO(
            orderId=self.order_id,
            clientOrderId=self.client_order_id,
            symbol=self.symbol,
            side=self.side,
            type=self.type,
            status=self.status,
            origQty=self.orig_qty,
            executedQty=self.executed_qty,
            avgPrice=self.avg_price,
            price=self.price,
            stopPrice=self.stop_price,
            reduceOnly=self.reduce_only,
            closePosition=self.close_position,
            positionSide=self.position_side,
            workingType=self.working_type,
            timeInForce="GTC",
            updateTime=self.update_time,
        )


@dataclass(slots=True)
class _ShadowPosition:
    account_id: str
    symbol: str
    position_side: PosSide
    qty: Decimal = Decimal(0)
    entry_price: Decimal = Decimal(0)
    leverage: int = 20


class ShadowExchange:
    """Exchange-like state machine for SHADOW accounts.

    All state is namespaced by account id.  The public methods mirror the
    methods used by ``AsterClient``; they deliberately do not expose signing
    or credentials.  ``metrics`` is used by load reports and safety tests.
    """

    def __init__(
        self,
        *,
        hedge: bool,
        initial_balance: Decimal = Decimal("1000"),
        fill_price: Decimal = Decimal("65000"),
        auto_trigger_conditionals: bool = True,
    ) -> None:
        self.hedge = hedge
        self.initial_balance = initial_balance
        self.market_price = fill_price
        self.auto_trigger_conditionals = auto_trigger_conditionals
        self.balances: dict[str, Decimal] = {}
        self._positions: dict[tuple[str, str, PosSide], _ShadowPosition] = {}
        self._orders: dict[tuple[str, str], _ShadowOrder] = {}
        self._order_seq = 0
        self._update_seq = 0
        self._leverage: dict[tuple[str, str], int] = {}
        self.metrics: Counter[str] = Counter()
        self.signed_requests = 0

    def add_account(self, account_id: str, balance: Decimal | None = None) -> None:
        self.balances.setdefault(account_id, balance or self.initial_balance)

    def set_market_price(self, price: Decimal) -> None:
        if price <= 0:
            return
        self.market_price = price
        if self.auto_trigger_conditionals:
            self._trigger_conditionals()

    async def server_time(self) -> int:
        self.metrics["server_time"] += 1
        return int(time.time() * 1000)

    async def exchange_info(self, force: bool = False) -> ExchangeInfo:
        del force
        self.metrics["exchange_info"] += 1
        return ExchangeInfo.model_validate(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "triggerProtect": False,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            {
                                "filterType": "LOT_SIZE",
                                "minQty": "0.001",
                                "maxQty": "1000",
                                "stepSize": "0.001",
                            },
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0.001",
                                "maxQty": "120",
                                "stepSize": "0.001",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                        "orderTypes": ["MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"],
                    }
                ],
                "rateLimits": [],
            }
        )

    async def balance(self, account_id: str) -> list[BalanceDTO]:
        self._ensure_account(account_id)
        self.metrics["balance"] += 1
        balance = self.balances[account_id]
        return [
            BalanceDTO(
                asset="USDT",
                balance=balance,
                crossWalletBalance=balance,
                crossUnPnl=Decimal(0),
                availableBalance=balance,
                maxWithdrawAmount=balance,
            )
        ]

    async def position_risk(self, account_id: str, symbol: str | None = None) -> list[PositionDTO]:
        self._ensure_account(account_id)
        self.metrics["position_risk"] += 1
        out: list[PositionDTO] = []
        for position in self._positions.values():
            if position.account_id != account_id or position.qty == 0:
                continue
            if symbol is not None and position.symbol != symbol:
                continue
            out.append(
                PositionDTO(
                    symbol=position.symbol,
                    positionAmt=position.qty,
                    entryPrice=position.entry_price,
                    markPrice=self.market_price,
                    leverage=position.leverage,
                    liquidationPrice=Decimal(0),
                    marginType="cross",
                    positionSide=position.position_side,
                    unRealizedProfit=Decimal(0),
                )
            )
        return sorted(out, key=lambda p: (p.symbol, p.position_side.value))

    async def open_orders(self, account_id: str, symbol: str | None = None) -> list[OrderDTO]:
        self._ensure_account(account_id)
        self.metrics["open_orders"] += 1
        orders = [
            order
            for order in self._orders.values()
            if order.account_id == account_id
            and order.status == "NEW"
            and (symbol is None or order.symbol == symbol)
        ]
        return [o.dto() for o in sorted(orders, key=lambda o: o.update_time)]

    async def query_order(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> OrderDTO | None:
        self._ensure_account(account_id)
        self.metrics["query_order"] += 1
        order = None
        if orig_client_order_id is not None:
            order = self._orders.get((account_id, orig_client_order_id))
        elif order_id is not None:
            order = next(
                (
                    candidate
                    for candidate in self._orders.values()
                    if candidate.account_id == account_id and candidate.order_id == order_id
                ),
                None,
            )
        if order is None or order.symbol != symbol:
            return None
        return order.dto()

    async def place_order(self, account_id: str, req: PlaceOrderRequest) -> OrderDTO:
        self._ensure_account(account_id)
        self.metrics["place_order"] += 1
        self._order_seq += 1
        self._update_seq += 1
        quantity = req.quantity
        if quantity is None and req.close_position:
            quantity = abs(
                self._positions.get(
                    (account_id, req.symbol, req.position_side),
                    _ShadowPosition(account_id, req.symbol, req.position_side),
                ).qty
            )
        quantity = quantity or Decimal(0)
        order = _ShadowOrder(
            account_id=account_id,
            symbol=req.symbol,
            side=req.side,
            type=req.type,
            orig_qty=quantity,
            price=req.price or Decimal(0),
            stop_price=req.stop_price or Decimal(0),
            reduce_only=req.reduce_only,
            close_position=req.close_position,
            position_side=req.position_side,
            working_type=req.working_type or "MARK_PRICE",
            client_order_id=req.new_client_order_id,
            order_id=self._order_seq,
            update_time=self._update_seq,
        )
        self._orders[(account_id, order.client_order_id)] = order
        if req.type == "MARKET":
            self._fill_market(order, self.market_price)
        return order.dto()

    async def cancel_order(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> OrderDTO:
        self._ensure_account(account_id)
        self.metrics["cancel_order"] += 1
        order = None
        if orig_client_order_id is not None:
            order = self._orders.get((account_id, orig_client_order_id))
        elif order_id is not None:
            order = next(
                (
                    candidate
                    for candidate in self._orders.values()
                    if candidate.account_id == account_id and candidate.order_id == order_id
                ),
                None,
            )
        if order is None or order.symbol != symbol or order.status != "NEW":
            raise AsterError(-2011, "Unknown order sent.")
        self._update_seq += 1
        order.status = "CANCELED"
        order.update_time = self._update_seq
        return order.dto()

    async def set_leverage(self, account_id: str, symbol: str, leverage: int) -> None:
        self._ensure_account(account_id)
        self.metrics["set_leverage"] += 1
        self._leverage[(account_id, symbol)] = leverage
        for position in self._positions.values():
            if position.account_id == account_id and position.symbol == symbol:
                position.leverage = leverage

    async def get_position_mode(self, account_id: str) -> bool:
        self._ensure_account(account_id)
        self.metrics["get_position_mode"] += 1
        return self.hedge

    async def set_position_mode(self, account_id: str, dual: bool) -> None:
        self._ensure_account(account_id)
        if any(p.account_id == account_id and p.qty != 0 for p in self._positions.values()):
            raise AsterError(-4067, "Position mode cannot be changed while positions are open")
        self.metrics["set_position_mode"] += 1
        self.hedge = dual

    def audit(self, account_id: str) -> dict[str, object]:
        """Return non-secret state for load assertions and reports."""
        self._ensure_account(account_id)
        positions = [
            {
                "symbol": p.symbol,
                "position_side": p.position_side.value,
                "qty": str(p.qty),
            }
            for p in self._positions.values()
            if p.account_id == account_id and p.qty != 0
        ]
        orders = [
            o.client_order_id
            for o in self._orders.values()
            if o.account_id == account_id and o.status == "NEW"
        ]
        return {"positions": positions, "open_orders": sorted(orders)}

    def _ensure_account(self, account_id: str) -> None:
        if account_id not in self.balances:
            self.add_account(account_id)

    def _fill_market(self, order: _ShadowOrder, price: Decimal) -> None:
        key = (order.account_id, order.symbol, order.position_side)
        position = self._positions.get(key)
        if position is None:
            position = _ShadowPosition(
                account_id=order.account_id,
                symbol=order.symbol,
                position_side=order.position_side,
                leverage=self._leverage.get((order.account_id, order.symbol), 20),
            )
            self._positions[key] = position
        old_qty = position.qty
        signed_delta = order.orig_qty if order.side == Side.BUY else -order.orig_qty
        new_qty = old_qty + signed_delta
        if (old_qty != 0 and new_qty * old_qty < 0) or (old_qty == 0 and new_qty != 0):
            position.entry_price = price
        elif abs(new_qty) > abs(old_qty):
            total = abs(old_qty) + abs(signed_delta)
            if total > 0:
                position.entry_price = (
                    abs(old_qty) * position.entry_price + abs(signed_delta) * price
                ) / total
        position.qty = new_qty
        order.status = "FILLED"
        order.executed_qty = order.orig_qty
        order.avg_price = price
        self._update_seq += 1
        order.update_time = self._update_seq
        if new_qty == 0:
            for candidate in self._orders.values():
                if (
                    candidate.account_id == order.account_id
                    and candidate.symbol == order.symbol
                    and candidate.position_side == order.position_side
                    and candidate.status == "NEW"
                    and candidate.type
                    in {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}
                ):
                    candidate.status = "EXPIRED"
                    candidate.update_time = self._update_seq

    def _trigger_conditionals(self) -> None:
        for order in list(self._orders.values()):
            if order.status != "NEW" or order.type not in {
                "STOP_MARKET",
                "STOP",
                "TAKE_PROFIT_MARKET",
                "TAKE_PROFIT",
            }:
                continue
            position = self._positions.get((order.account_id, order.symbol, order.position_side))
            if position is None or position.qty == 0:
                continue
            is_long = position.qty > 0
            is_stop = order.type in {"STOP_MARKET", "STOP"}
            triggered = (
                is_long and order.side == Side.SELL and self.market_price <= order.stop_price
            ) or (not is_long and order.side == Side.BUY and self.market_price >= order.stop_price)
            if not is_stop:
                triggered = (
                    is_long and order.side == Side.SELL and self.market_price >= order.stop_price
                ) or (
                    not is_long and order.side == Side.BUY and self.market_price <= order.stop_price
                )
            if triggered:
                self._fill_market(order, self.market_price)


__all__ = ["ShadowExchange"]
