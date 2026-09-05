"""In-memory Aster V3 surface for end-to-end tests. No HTTP.

Supports: balances, positions (one-way and hedge), MARKET fills at a
configurable price, STOP_MARKET triggering on price movement, open-order
tracking, current/history order queries, cancel_order, leverage/mode endpoints and injected
failures (HTTP 503, timeout, -2019 margin).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.aster.trading_api import AsterError
from copytradeaster.domain.models import PosSide, Side, SymbolFilters

D = Decimal
CONDITIONAL_TYPES = {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}


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
        self.fail_next: dict[str, str | None] = {}
        # Values: 503/timeout/-2019 fail before acceptance; 503-after-fill
        # applies and journals a MARKET fill, then loses the response.
        self.fail_order_types: dict[tuple[str, str], int] = {}
        self.placed_log: list[tuple[str, str]] = []
        self.canceled_log: list[tuple[str, str]] = []
        self.partial_market_fill_ratio: Decimal | None = None

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
            if order.status != "NEW" or order.type not in CONDITIONAL_TYPES:
                continue
            account = key.split("|", 1)[0]
            qty = self.positions.get((account, f"{order.symbol}:{order.position_side.value}"), D(0))
            if qty == 0:
                order.status = "EXPIRED"
                self.open_order_coids.get(account, set()).discard(order.client_order_id)
                continue
            long_pos = qty > 0
            is_stop = order.type in {"STOP_MARKET", "STOP"}
            trigger = (
                (long_pos and order.side == Side.SELL and price <= order.stop_price)
                or (not long_pos and order.side == Side.BUY and price >= order.stop_price)
                if is_stop
                else (
                    (long_pos and order.side == Side.SELL and price >= order.stop_price)
                    or (not long_pos and order.side == Side.BUY and price <= order.stop_price)
                )
            )
            if trigger:
                self._fill_order(key, order, price)
                triggered.append(order)
        return triggered

    def _position_sign(self, account_id: str, symbol: str) -> int:
        qty = self.positions.get((account_id, f"{symbol}:BOTH"), D(0))
        return 1 if qty > 0 else (-1 if qty < 0 else 0)

    def _fill_order(self, key: str, order: FakeOrder, price: Decimal) -> None:
        """Exchange-like fill accounting: increases average-in at the last fill
        price, reductions keep the entry, flips re-base it. The previous fake
        averaged reductions too - mirroring (and thus hiding) the projector bug."""
        account = key.split("|", 1)[0]
        pk = (account, f"{order.symbol}:{order.position_side.value}")
        old = self.positions.get(pk, D(0))
        fill_qty = abs(old) if order.close_position else order.orig_qty
        signed_delta = fill_qty * (1 if order.side == Side.BUY else -1)
        closes = old != 0 and old * signed_delta < 0
        if order.type in CONDITIONAL_TYPES and not closes:
            order.status = "EXPIRED"
            return
        if (
            closes
            and abs(signed_delta) > abs(old)
            and (order.position_side != PosSide.BOTH or order.reduce_only or order.close_position)
        ):
            if order.position_side != PosSide.BOTH and not order.close_position:
                # Fixed oversized HEDGE conditionals are exchange-invalid at
                # trigger time; they do not silently flip the leg.
                order.status = "EXPIRED"
                return
            signed_delta = -old
            fill_qty = abs(old)
        order.status = "FILLED"
        order.executed_qty = fill_qty
        order.avg_price = price
        new_qty = old + signed_delta
        if new_qty == 0:
            self.positions.pop(pk, None)
            self.entry_prices.pop(pk, None)
            self._expire_conditionals(account, order.symbol, order.position_side, except_=order)
            return
        if old != 0 and new_qty * old < 0:
            self.entry_prices[pk] = price  # flip: remainder opened by this fill
        elif abs(new_qty) > abs(old):
            ep_old = self.entry_prices.get(pk, price)
            total = abs(old) + abs(signed_delta)
            self.entry_prices[pk] = (abs(old) * ep_old + abs(signed_delta) * price) / total
        # reduction: entry price stays
        self.positions[pk] = new_qty

    def _expire_conditionals(
        self, account_id: str, symbol: str, pos_side: PosSide, *, except_: FakeOrder
    ) -> None:
        for key, candidate in self.orders.items():
            if (
                candidate is not except_
                and key.startswith(f"{account_id}|")
                and candidate.symbol == symbol
                and candidate.position_side == pos_side
                and candidate.status == "NEW"
                and candidate.type in CONDITIONAL_TYPES
            ):
                candidate.status = "EXPIRED"

    def next_coid(self) -> str:
        self._seq += 1
        return f"fake-{self._seq}"

    def trigger_stop_fill(self, account_id: str) -> None:
        """Force-fill this account's resting STOP_MARKET (manual stop touch)."""
        for key, order in list(self.orders.items()):
            if (
                key.startswith(f"{account_id}|")
                and order.status == "NEW"
                and order.type in {"STOP_MARKET", "STOP"}
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
            entry = self.entry_prices.get((account, side_key), self.fill_price)
            leverage = self.leverages.get(account, 20) or 20
            # Realistic cross liquidation: entry*(1 -/+ 1/leverage) by side.
            # A hardcoded placeholder (e.g. 100) trips the PM-B7
            # liquidation-vs-stop safety net on every leg; the fake must not
            # lie about geometry the engine is required to respect.
            side_sign = D(1) if qty > 0 else D(-1)
            liq = (entry * (D(1) - side_sign / D(leverage))).quantize(D("0.1"))
            out.append(
                PositionDTO(
                    symbol=sym,
                    positionAmt=qty,
                    entryPrice=entry,
                    markPrice=self.price,
                    leverage=leverage,
                    liquidationPrice=liq if liq > 0 else D(0),
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

    async def all_orders(self, account_id: str, symbol: str, *, limit: int = 1000):
        orders = [
            order
            for key, order in self.orders.items()
            if key.startswith(f"{account_id}|") and order.symbol == symbol
        ]
        return sorted(orders, key=lambda order: order.update_seq, reverse=True)[:limit]

    async def query_order_history(self, account_id: str, symbol: str, *, orig_client_order_id: str):
        order = self.orders.get(self._k(account_id, orig_client_order_id))
        if order is None or order.symbol != symbol:
            return None
        return order

    async def place_order(self, account_id: str, req):
        failure_key = (account_id, req.type)
        remaining = self.fail_order_types.get(failure_key, 0)
        if remaining > 0:
            self.fail_order_types[failure_key] = remaining - 1
            raise AsterError(-2019, f"injected {req.type} failure")
        failure = self._consume_failure(account_id)
        if failure == "timeout":
            raise TimeoutError
        if failure == "503":
            raise AsterError(-1000, "Unknown error, please check your request.", http_status=503)
        if failure == "-2019":
            raise AsterError(-2019, "Margin is insufficient.")

        if req.close_position and (
            req.type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
            or req.quantity is not None
            or req.reduce_only
        ):
            raise AsterError(-1102, "closePosition is incompatible with quantity/reduceOnly")
        if req.position_side != PosSide.BOTH and req.reduce_only:
            raise AsterError(-1106, "reduceOnly is not accepted in Hedge Mode")
        if req.type == "MARKET" and (req.quantity is None or req.quantity <= 0):
            raise AsterError(-1102, "quantity is mandatory for MARKET")
        if (
            req.type in CONDITIONAL_TYPES
            and not req.close_position
            and (req.quantity is None or req.quantity <= 0)
        ):
            raise AsterError(-1102, "quantity is mandatory for fixed conditional")

        self._seq += 1
        order = FakeOrder(
            symbol=req.symbol,
            side=req.side,
            type=req.type,
            orig_qty=req.quantity or D(0),
            price=req.price or D(0),
            stop_price=req.stop_price or D(0),
            # Live Aster reports reduceOnly=true for accepted closePosition
            # conditionals even though the request correctly omits the field.
            reduce_only=req.reduce_only or req.close_position or req.type in CONDITIONAL_TYPES,
            close_position=req.close_position,
            position_side=req.position_side,
            working_type=req.working_type or "MARK_PRICE",
            client_order_id=req.new_client_order_id,
            order_id=self._seq,
            update_seq=self._seq,
        )
        if req.type == "MARKET":
            pk = (account_id, f"{req.symbol}:{req.position_side.value}")
            old = self.positions.get(pk, D(0))
            signed = order.orig_qty if order.side == Side.BUY else -order.orig_qty
            opens_hedge = (
                req.position_side == PosSide.LONG
                and req.side == Side.BUY
                or req.position_side == PosSide.SHORT
                and req.side == Side.SELL
            )
            if req.position_side != PosSide.BOTH:
                if not opens_hedge and (old == 0 or old * signed >= 0 or abs(signed) > abs(old)):
                    raise AsterError(-2022, "ReduceOnly Order is rejected.")
            elif (
                req.reduce_only
                and (old == 0 or old * signed >= 0 or abs(signed) > abs(old))
                and not opens_hedge
            ):
                raise AsterError(-2022, "ReduceOnly Order is rejected.")
            if self.partial_market_fill_ratio is not None:
                ratio = min(max(self.partial_market_fill_ratio, D(0)), D(1))
                order.orig_qty *= ratio
            self._fill_order(self._k(account_id, req.new_client_order_id), order, self.fill_price)
            order.status = "FILLED"
            self.orders[self._k(account_id, req.new_client_order_id)] = order
            self.placed_log.append((account_id, req.new_client_order_id))
            if failure == "503-after-fill":
                raise AsterError(
                    -1000,
                    "Unknown error after accepted MARKET.",
                    http_status=503,
                )
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
