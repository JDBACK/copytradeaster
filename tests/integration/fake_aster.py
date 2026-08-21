"""In-memory Aster V3 surface for end-to-end tests. No HTTP.

Supports: balances, positions (one-way and hedge), MARKET fills at a
configurable price, STOP_MARKET triggering on price movement, open-order
tracking, query_order, cancel_order, leverage/mode endpoints and injected
failures (HTTP 503, timeout, -2019 margin).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.domain.models import PosSide, Side, SymbolFilters

D = Decimal


class InjectedFailure(Exception):
    """Simulates transport-level failure (503/timeout)."""


@dataclass(slots=True)
class FakeOrder:
    symbol: str
    side: Side
    type: str
    orig_qty: Decimal
    price: Decimal = D(0)
    stop_price: Decimal = D(0)
    avg_price: Decimal = D(0)
    executed_qty: Decimal = D(0)
    status: str = "NEW"
    reduce_only: bool = False
    close_position: bool = False
    position_side: PosSide = PosSide.BOTH
    working_type: str = "MARK_PRICE"
    client_order_id: str = ""
    order_id: int = 0
    update_time: int = 0
    update_seq: int = 0

    def touch(self, seq: int) -> None:
        self.update_seq = seq
        self.update_time = seq

    def model_dump(self):
        return {"clientOrderId": self.client_order_id}


def btc_filters(step="0.001", tick="0.1", min_qty="0.001") -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=D(tick),
        step_size=D(step),
        min_qty=D(min_qty),
        max_qty=D(1000),
        market_max_qty=D(120),
        min_notional=D(5),
    )


class FakeAster:
    def __init__(self, fill_price: Decimal | None = None) -> None:
        fill_price = fill_price if fill_price is not None else D("65000")
        self.fill_price = fill_price
        self.price = fill_price
        self.balances: dict[str, Decimal] = {}
        self.positions: dict[tuple[str, str], Decimal] = {}  # (account, pos_side)->signed qty
        self.entry_prices: dict[tuple[str, str], Decimal] = {}
        self.leverages: dict[str, int] = {}
        self.orders: dict[str, FakeOrder] = {}  # key: account|coid
        self.open_order_coids: dict[str, set[str]] = {}
        self._seq = 0
        self._lock = asyncio.Lock()
        self.fail_next: dict[str, str | None] = {}  # account -> "503"|"timeout"|"-2019"
        self.placed_log: list[tuple[str, str]] = []
        self.canceled_log: list[tuple[str, str]] = []

    # --- helpers ---------------------------------------------------------------

    def add_account(self, account_id: str, usdt: Decimal) -> None:
        self.balances[account_id] = usdt
        self.leverages.setdefault(account_id, 20)

    @staticmethod
    def _k(account_id: str, coid: str) -> str:
        return f"{account_id}|{coid}"

    def set_price(self, price: Decimal) -> list[FakeOrder]:
        """Moves the market; returns protective orders triggered by the move."""
        self.price = price
        triggered: list[FakeOrder] = []
        for key, order in self.orders.items():
            if order.status != "NEW" or order.type != "STOP_MARKET":
                continue
            account = key.split("|", 1)[0]
            long_pos = self._position_sign(account, order.symbol) > 0
            if (long_pos and order.side == Side.SELL and price <= order.stop_price) or (
                not long_pos and order.side == Side.BUY and price >= order.stop_price
            ):
                self._fill_order(key, order, price)
                triggered.append(order)
        return triggered

    def _position_sign(self, account_id: str, symbol: str) -> int:
        qty = self.positions.get((account_id, f"{symbol}:BOTH"), D(0))
        return 1 if qty > 0 else (-1 if qty < 0 else 0)

    def _fill_order(self, key: str, order: FakeOrder, price: Decimal) -> None:
        account = key.split("|", 1)[0]
        order.status = "FILLED"
        order.executed_qty = order.orig_qty
        order.avg_price = price
        signed_delta = order.orig_qty * (1 if order.side == Side.BUY else -1)
        pk = (account, f"{order.symbol}:{order.position_side.value}")
        old = self.positions.get(pk, D(0))
        new_qty = old + signed_delta
        if new_qty == 0:
            self.positions.pop(pk, None)
            self.entry_prices.pop(pk, None)
        else:
            ep_old = self.entry_prices.get(pk, abs(old) * self.fill_price or price)
            total = abs(old) + abs(signed_delta)
            self.entry_prices[pk] = (abs(old) * ep_old + abs(signed_delta) * price) / total
            self.positions[pk] = new_qty

    def next_coid(self) -> str:
        self._seq += 1
        return f"fake-{self._seq}"

    def trigger_stop_fill(self, account_id: str) -> None:
        """Force-fill this account's resting STOP_MARKET (manual stop touch)."""
        for key, order in list(self.orders.items()):
            if (
                key.startswith(f"{account_id}|")
                and order.status == "NEW"
                and order.type == "STOP_MARKET"
            ):
                self._fill_order(key, order, order.stop_price)

    def _consume_failure(self, account_id: str) -> str | None:
        failure = self.fail_next.get(account_id)
        if failure is not None:
            self.fail_next[account_id] = None
        return failure

    # --- AsterClient-compatible surface -----------------------------------------

    async def server_time(self) -> int:
        return int(time.time() * 1000)

    async def exchange_info(self, force: bool = False):
        del force
        from copytradeaster.aster.models import ExchangeInfo

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

    async def balance(self, account_id: str):
        from copytradeaster.aster.models import BalanceDTO

        bal = self.balances.get(account_id, D(1000))
        return [
            BalanceDTO(
                asset="USDT",
                balance=bal,
                crossWalletBalance=bal,
                crossUnPnl=D(0),
                availableBalance=bal,
                maxWithdrawAmount=bal,
            )
        ]

    async def position_risk(self, account_id: str, symbol: str | None = None):
        from copytradeaster.aster.models import PositionDTO

        out = []
        for (account, side_key), qty in self.positions.items():
            if account != account_id or qty == 0:
                continue
            sym = side_key.rsplit(":", 1)[0]
            ps = PosSide(side_key.rsplit(":", 1)[1])
            if symbol is not None and sym != symbol:
                continue
            out.append(
                PositionDTO(
                    symbol=sym,
                    positionAmt=qty,
                    entryPrice=self.entry_prices.get((account, side_key), self.fill_price),
                    markPrice=self.price,
                    leverage=self.leverages.get(account, 20),
                    liquidationPrice=D(100),
                    marginType="cross",
                    positionSide=ps,
                    unRealizedProfit=D(0),
                )
            )
        return out

    async def open_orders(self, account_id: str, symbol: str | None = None):
        out = []
        for key, order in self.orders.items():
            if not key.startswith(f"{account_id}|"):
                continue
            if order.status != "NEW":
                continue
            if symbol is not None and order.symbol != symbol:
                continue
            out.append(order)
        return sorted(out, key=lambda o: o.update_seq)

    async def query_order(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ):
        del symbol
        if orig_client_order_id is None:
            return None
        return self.orders.get(self._k(account_id, orig_client_order_id))

    async def place_order(self, account_id: str, req):
        failure = self._consume_failure(account_id)
        if failure == "timeout":
            raise TimeoutError
        if failure == "503":
            from copytradeaster.aster.trading_api import AsterError

            raise AsterError(-1000, "Unknown error, please check your request.", http_status=503)
        if failure == "-2019":
            from copytradeaster.aster.trading_api import AsterError

            raise AsterError(-2019, "Margin is insufficient.")

        self._seq += 1
        order = FakeOrder(
            symbol=req.symbol,
            side=req.side,
            type=req.type,
            orig_qty=req.quantity or D(0),
            price=req.price or D(0),
            stop_price=req.stop_price or D(0),
            reduce_only=req.reduce_only,
            close_position=req.close_position,
            position_side=req.position_side,
            working_type=req.working_type or "MARK_PRICE",
            client_order_id=req.new_client_order_id,
            order_id=self._seq,
            update_seq=self._seq,
        )
        if req.type == "MARKET":
            self._fill_order(self._k(account_id, req.new_client_order_id), order, self.fill_price)
            order.status = "FILLED"
            self.orders[self._k(account_id, req.new_client_order_id)] = order
            self.placed_log.append((account_id, req.new_client_order_id))
            return order
        # conditional: rest as NEW until price touches it
        self.orders[self._k(account_id, req.new_client_order_id)] = order
        self.placed_log.append((account_id, req.new_client_order_id))
        return order

    async def cancel_order(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ):
        from copytradeaster.aster.trading_api import AsterError

        key = self._k(account_id, orig_client_order_id or "")
        order = self.orders.get(key)
        if order is None or order.status != "NEW":
            raise AsterError(-2011, "Unknown order sent.")
        order.status = "CANCELED"
        self.canceled_log.append((account_id, orig_client_order_id or ""))
        return order

    async def set_leverage(self, account_id: str, symbol: str, leverage: int) -> None:
        del symbol
        self.leverages[account_id] = leverage

    async def get_position_mode(self, account_id: str) -> bool:
        del account_id
        return False

    async def set_position_mode(self, account_id: str, dual: bool) -> None:
        del account_id, dual
