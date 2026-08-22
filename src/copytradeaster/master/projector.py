"""Master projector: raw WS payloads -> normalized MasterState + deltas.

Raw events are persisted first (dedup by UNIQUE dedup_key); duplicates and
out-of-order (E older than last processed) events are ignored. Trade episodes:
a copy_trade opens on 0->non-zero (or sign flip) position fill and closes when
the position returns to zero.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.domain.models import PositionKey, PositionState, PosSide, ProtectiveOrder
from copytradeaster.logging_utils import get_logger
from copytradeaster.storage.repositories import (
    CopyTradeRepo,
    MasterEventRepo,
    OrderLinkRepo,
    SettingsRepo,
)

log = get_logger(__name__)


@dataclass(slots=True)
class MasterState:
    available_usdt: Decimal = Decimal(0)
    positions: dict[PositionKey, PositionState] = field(default_factory=dict)
    """Wall-clock ms of the last applied WS event; 0 = snapshot/manual (fresh)."""
    event_time: int = 0


@dataclass(slots=True)
class MasterDelta:
    changed_symbols: frozenset[str]
    state: MasterState


def _pos_side(raw_ps: str) -> PosSide:
    try:
        return PosSide(raw_ps or "BOTH")
    except ValueError:
        return PosSide.BOTH


class MasterProjector:
    def __init__(
        self,
        events: MasterEventRepo,
        trades: CopyTradeRepo,
        links: OrderLinkRepo,
        settings: SettingsRepo,
        on_delta: Callable[[MasterDelta], Awaitable[None]],
    ) -> None:
        self._events = events
        self._trades = trades
        self._links = links
        self._settings = settings
        self._on_delta = on_delta
        self.state = MasterState()
        self._last_event_time: int | None = None

    # --- entry points -----------------------------------------------------------

    async def apply_raw(self, raw: dict) -> None:
        event_type = str(raw.get("e", ""))
        event_time = int(raw.get("E", 0))
        if event_type == "ORDER_TRADE_UPDATE":
            o = raw.get("o", {})
            dedup_key = f"{event_type}:{event_time}:{o.get('i')}:{o.get('x')}"
        else:
            dedup_key = f"{event_type}:{event_time}"
        event_id = await self._events.append(
            event_type, event_time, json.dumps(raw, separators=(",", ":")), dedup_key
        )
        if event_id is None:
            log.debug("duplicate event dropped: %s", dedup_key)
            return
        if self._last_event_time is not None and event_time < self._last_event_time:
            log.debug("out-of-order event ignored: E=%s < %s", event_time, self._last_event_time)
            await self._events.mark_processed(event_id)
            return
        self._last_event_time = event_time
        self.state.event_time = int(time.time() * 1000)

        if event_type == "ORDER_TRADE_UPDATE":
            await self._on_order_update(raw["o"], event_id)
        elif event_type == "ACCOUNT_UPDATE":
            await self._on_account_update(raw.get("a", {}), event_id)
        elif event_type == "ACCOUNT_CONFIG_UPDATE":
            await self._on_config_update(raw, event_id)
        elif event_type == "MARGIN_CALL":
            await self._on_margin_call(raw, event_id)
        else:
            await self._events.mark_processed(event_id)

    async def rebuild_full(self, client: AsterClient, account_id: str) -> MasterState:
        """REST snapshot: balance + positionRisk + openOrders -> MasterState."""
        balances = await client.balance(account_id)
        available = Decimal(0)
        for b in balances:
            if b.asset == "USDT":
                available = b.available_balance
                break
        positions: dict[PositionKey, PositionState] = {}
        open_orders = {str(o.symbol): [] for o in await client.open_orders(account_id)}
        for dto in await client.open_orders(account_id):
            open_orders.setdefault(str(dto.symbol), []).append(dto)
        for p in await client.position_risk(account_id):
            key = PositionKey(symbol=str(p.symbol), pos_side=p.position_side)
            if p.position_amt == 0 and key.pos_side == PosSide.BOTH:
                continue
            same_symbol = [
                o
                for o in open_orders.get(str(p.symbol), [])
                if _matches_position_side(o.position_side, key.pos_side)
            ]
            stop = _pick_protective(same_symbol, "STOP_MARKET")
            tp = _pick_protective(same_symbol, "TAKE_PROFIT_MARKET")
            positions[key] = PositionState(
                key=key,
                qty=p.position_amt,
                entry_price=p.entry_price,
                leverage=int(p.leverage),
                mark_price=p.mark_price,
                liquidation_price=p.liquidation_price,
                stop=stop,
                take_profit=tp,
            )
        self.state = MasterState(available_usdt=available, positions=positions)
        self.state.event_time = int(time.time() * 1000)
        return self.state

    # --- handlers -----------------------------------------------------------------

    async def _on_order_update(self, o: dict, event_id: int) -> None:
        symbol = str(o.get("s", ""))
        execution_type = str(o.get("x", ""))
        order_type = str(o.get("ot", ""))
        status = str(o.get("X", ""))
        coid = str(o.get("c", ""))
        pos_side = _pos_side(str(o.get("ps", "BOTH")))
        changed = False

        if execution_type == "TRADE":
            changed |= await self._apply_fill(symbol, pos_side, o, event_id)
        elif execution_type in ("NEW", "CANCELED") and order_type in (
            "STOP_MARKET",
            "TAKE_PROFIT_MARKET",
        ):
            changed |= await self._update_protective(symbol, pos_side, order_type, status, coid, o)

        if changed or execution_type == "TRADE":
            await self._refresh_and_emit(client=None, symbols={symbol})
        await self._events.mark_processed(event_id)

    async def _apply_fill(self, symbol: str, pos_side: PosSide, o: dict, event_id: int) -> bool:
        """Update position from a TRADE fill; manage trade episodes."""
        last_qty = Decimal(str(o.get("l", "0")))
        side_str = str(o.get("S", "BUY"))
        # Binance semantics: S is the ORDER side. LONG leg: BUY opens / SELL
        # closes; SHORT leg: SELL opens / BUY closes. The previous code overrode
        # direction by pos_side and turned closing fills into opens (live TP-fill
        # incident: master flat on the exchange, projector re-opened it).
        direction = 1 if side_str == "BUY" else -1
        key = PositionKey(symbol=symbol, pos_side=pos_side)
        current = self.state.positions.get(key)
        old_qty = current.qty if current is not None else Decimal(0)
        new_qty = old_qty + direction * last_qty

        active = await self._trades.active_trade(symbol, pos_side.value)
        if (old_qty == 0 or old_qty * new_qty < 0) and active is None:
            trade_id = await self._trades.open_trade(symbol, pos_side.value, event_id)
            log.info("master opened %s %s (trade_id=%s)", symbol, pos_side.value, trade_id)
        if new_qty == 0 and active is not None:
            await self._trades.close_trade(int(active["trade_id"]))
            log.info(
                "master closed %s %s (trade_id=%s)", symbol, pos_side.value, active["trade_id"]
            )
        # a closing fill (or flip) invalidates the leg's protective orders: the
        # exchange expires them server-side and a stale level here would poison
        # the next episode's sizing (live incident: 0.277 BTC sized off a dead stop)
        if (
            current is not None
            and (new_qty == 0 or new_qty * old_qty < 0)
            and (current.stop is not None or current.take_profit is not None)
        ):
            current.stop = None
            current.take_profit = None

        if current is None:
            current = PositionState(
                key=key,
                qty=new_qty,
                entry_price=Decimal(str(o.get("ap", "0"))),
                leverage=0,
                mark_price=Decimal(str(o.get("ap", "0"))),
                liquidation_price=Decimal(0),
            )
            self.state.positions[key] = current
        else:
            current.qty = new_qty
            fill_price = Decimal(str(o.get("ap", "0")))
            if fill_price > 0:
                total = abs(old_qty) + last_qty
                if total > 0:
                    current.entry_price = (
                        abs(old_qty) * current.entry_price + last_qty * fill_price
                    ) / total
        realized = str(o.get("rp", ""))
        if realized:
            log.debug("fill rp=%s", realized)
        return True

    async def _update_protective(
        self, symbol: str, pos_side: PosSide, order_type: str, status: str, coid: str, o: dict
    ) -> bool:
        key = PositionKey(symbol=symbol, pos_side=pos_side)
        pos = self.state.positions.get(key)
        slot_name = "stop" if order_type == "STOP_MARKET" else "take_profit"
        current: ProtectiveOrder | None = getattr(pos, slot_name) if pos else None

        if status == "CANCELED" or status == "EXPIRED":
            if current is not None and current.client_order_id == coid:
                setattr(pos, slot_name, None)
                log.info("master %s removed (%s)", slot_name, symbol)
                return True
            return False
        if status == "NEW":
            prot = ProtectiveOrder(
                client_order_id=coid,
                order_id=int(o.get("i", 0)),
                type=order_type,
                side=_side_enum(str(o.get("S", "SELL"))),
                stop_price=Decimal(str(o.get("sp", "0"))),
                reduce_only=bool(o.get("R", False)),
                close_position=bool(o.get("cp", False)),
                working_type=str(o.get("wt", "CONTRACT_PRICE")),
            )
            if pos is None:
                pos = PositionState(
                    key=key,
                    qty=Decimal(0),
                    entry_price=Decimal(0),
                    leverage=0,
                    mark_price=Decimal(0),
                    liquidation_price=Decimal(0),
                )
                self.state.positions[key] = pos
            setattr(pos, slot_name, prot)
            log.info("master %s placed (%s @ %s)", slot_name, symbol, prot.stop_price)
            return True
        return False

    async def _on_account_update(self, a: dict, event_id: int) -> None:
        """USDT balance is applied; position rows are INFORMATIONAL only.

        ORDER_TRADE_UPDATE TRADE deltas are the single mutator of positions -
        applying both the absolute snapshot and the delta double-counts the
        same fill (live incident: TP fill re-opened a closed position because
        ACCOUNT_UPDATE preceded the TRADE event). Drift is logged and healed
        by rebuild_full on reconnect/boot.
        """
        for b in a.get("B", []):
            if str(b.get("a")) == "USDT":
                self.state.available_usdt = Decimal(str(b.get("wb", "0")))
        for p in a.get("P", []):
            symbol = str(p.get("s", ""))
            key = PositionKey(symbol=symbol, pos_side=_pos_side(str(p.get("ps", "BOTH"))))
            qty = Decimal(str(p.get("pa", "0")))
            pos = self.state.positions.get(key)
            projected = pos.qty if pos is not None else Decimal(0)
            if projected != qty:
                log.warning(
                    "master position drift %s/%s: stream=%s exchange=%s "
                    "(will reconcile on next reconnect)",
                    symbol,
                    key.pos_side.value,
                    projected,
                    qty,
                )
        await self._events.mark_processed(event_id)

    async def _on_config_update(self, body: dict, event_id: int) -> None:
        ac = body.get("ac")
        if ac is not None:
            symbol = str(ac.get("s", ""))
            leverage = int(ac.get("l", 0))
            for key, pos in self.state.positions.items():
                if key.symbol == symbol:
                    pos.leverage = leverage
                    await self._emit(frozenset({symbol}))
                    break
        ai = body.get("ai")
        if ai is not None and bool(ai.get("d")):
            log.warning("position mode switch detected - full rebuild required")
        await self._events.mark_processed(event_id)

    async def _on_margin_call(self, raw: dict, event_id: int) -> None:
        log.critical("MARGIN CALL: %s", json.dumps(raw))
        await self._settings.set_flag("degraded", f"margin_call:{raw.get('E', '')}")
        await self._events.mark_processed(event_id)

    # --- snapshot refresh ----------------------------------------------------------

    async def _refresh_and_emit(self, client: AsterClient | None, symbols: set[str]) -> None:
        """WS-only refresh; REST snapshot happens via rebuild_full/reconciler."""
        await self._emit(frozenset(symbols))

    async def _emit(self, symbols: frozenset[str]) -> None:
        if not symbols:
            return
        await self._on_delta(MasterDelta(changed_symbols=symbols, state=self.state))


def _side_enum(side: str):
    from copytradeaster.domain.models import Side

    try:
        return Side(side)
    except ValueError:
        return Side.SELL


def _matches_position_side(order_ps, key_pos: PosSide) -> bool:
    if key_pos == PosSide.BOTH:
        return order_ps == PosSide.BOTH
    return order_ps in (key_pos, PosSide.BOTH)


def _pick_protective(orders: list, order_type: str) -> ProtectiveOrder | None:
    """Prefer the newest conditional protective order of `order_type`."""
    candidates = [o for o in orders if o.type == order_type and (o.reduce_only or o.close_position)]
    if not candidates:
        return None
    latest = max(candidates, key=lambda o: o.update_time)
    return ProtectiveOrder(
        client_order_id=str(latest.client_order_id),
        order_id=int(latest.order_id),
        type=str(latest.type),
        side=latest.side,
        stop_price=latest.stop_price,
        reduce_only=bool(latest.reduce_only),
        close_position=bool(latest.close_position),
        working_type=str(latest.working_type),
    )
