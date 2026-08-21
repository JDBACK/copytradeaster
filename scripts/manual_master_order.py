"""Manual master actions for reproducible testnet smoke runs.

Usage:
  python scripts/manual_master_order.py open BTCUSDT 0.002 0.5   (qty, stop%)
  python scripts/manual_master_order.py move-stop BTCUSDT 63000
  python scripts/manual_master_order.py partial-close BTCUSDT 30  (percent)
  python scripts/manual_master_order.py close BTCUSDT

Position mode is taken from CT_POSITION_MODE (ONE_WAY default; HEDGE sends
positionSide and no reduceOnly, matching exchange rules).
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from copytradeaster.aster.rate_limits import RateLimits  # noqa: E402
from copytradeaster.aster.trading_api import AsterClient, PlaceOrderRequest  # noqa: E402
from copytradeaster.config import Settings  # noqa: E402
from copytradeaster.credentials.service import SqliteCredentials  # noqa: E402
from copytradeaster.domain.models import PosSide, Side, SymbolFilters  # noqa: E402
from copytradeaster.storage.database import Database  # noqa: E402


def hedge() -> bool:
    return os.environ.get("CT_POSITION_MODE", "ONE_WAY") == "HEDGE"


def leg_side(pos_side: PosSide) -> PosSide:
    """In hedge mode orders must carry the concrete LONG/SHORT side."""
    if hedge() and pos_side == PosSide.BOTH:
        return PosSide.LONG
    return pos_side


async def make_client() -> tuple[AsterClient, Database]:
    cfg = Settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    creds = SqliteCredentials(db, cfg.encryption_key_bytes())
    await creds.load()
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    limiter = RateLimits(
        cfg.weight_limit_per_min, cfg.orders_limit_per_min, cfg.orders_limit_per_10s
    )
    client = AsterClient(
        http=http,
        base_url=cfg.rest_base_url,
        limiter=limiter,
        identities=creds,
        keys=creds,
        nonces=creds,
        dry_run=cfg.dry_run,
    )
    return client, db


async def get_filters(client: AsterClient, symbol: str) -> SymbolFilters:
    info = await client.exchange_info()
    filters = info.tradable_filters().get(symbol)
    if filters is None:
        raise SystemExit(f"symbol {symbol} not tradable")
    return filters


async def positions_of(client: AsterClient, symbol: str):
    return [p for p in await client.position_risk("main", symbol) if p.position_amt != 0]


async def cmd_open(symbol: str, qty: Decimal, stop_pct: Decimal) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        marks = await client.position_risk("main", symbol)
        price = (
            Decimal(str(marks[0].mark_price)) if marks else await _fallback_price(client, symbol)
        )
        stop = (price * (Decimal(1) - stop_pct / 100)).quantize(filters.tick_size)
        ps = leg_side(PosSide.BOTH)
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.BUY,
                type="MARKET",
                quantity=qty,
                new_order_resp_type="RESULT",
                position_side=ps,
                new_client_order_id=f"manual-open-{int(price)}",
            ),
        )
        stop_leg = (
            PosSide.LONG
            if ps == PosSide.LONG
            else (PosSide.SHORT if ps == PosSide.SHORT else PosSide.BOTH)
        )
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=qty,
                stop_price=stop,
                working_type="MARK_PRICE",
                position_side=stop_leg,
                new_client_order_id=f"manual-stop-{int(stop)}",
            ),
        )
        print(f"opened {symbol} {qty} @~{price}, stop {stop} ({ps.value})")
    finally:
        await client._http.aclose()  # noqa: SLF001 - script-level access
        await db.close()


async def _fallback_price(client: AsterClient, symbol: str) -> Decimal:
    import httpx

    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(
            "https://fapi.asterdex-testnet.com/fapi/v1/ticker/price", params={"symbol": symbol}
        )
        data = resp.json()
        return Decimal(str(data.get("price", "65000")))


async def cmd_move_stop(symbol: str, new_stop: Decimal) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        orders = await client.open_orders("main", symbol=symbol)
        olds = [o for o in orders if o.type == "STOP_MARKET"]
        if not olds:
            raise SystemExit("no STOP_MARKET to move")
        old = olds[0]
        qty = old.orig_qty
        stop_leg = old.position_side
        await client.cancel_order("main", symbol, orig_client_order_id=old.client_order_id)
        new_stop = new_stop.quantize(filters.tick_size)
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=qty,
                stop_price=new_stop,
                working_type="MARK_PRICE",
                position_side=stop_leg,
                new_client_order_id=f"manual-stop-{int(new_stop)}",
            ),
        )
        print(f"moved stop to {new_stop} (qty {qty})")
    finally:
        await client._http.aclose()  # noqa: SLF001
        await db.close()


async def cmd_partial_close(symbol: str, pct: Decimal) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        open_positions = await positions_of(client, symbol)
        if not open_positions:
            raise SystemExit("no position to reduce")
        pos = open_positions[0]
        qty = abs(Decimal(str(pos.position_amt))) * pct / 100
        qty = (qty // filters.step_size) * filters.step_size
        side = Side.SELL if pos.position_amt > 0 else Side.BUY
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty,
                new_order_resp_type="RESULT",
                position_side=pos.position_side,
                new_client_order_id=f"manual-pc-{pct}",
            ),
        )
        print(f"partial close {pct}% -> {qty}")
    finally:
        await client._http.aclose()  # noqa: SLF001
        await db.close()


async def cmd_close(symbol: str) -> None:
    client, db = await make_client()
    try:
        for o in await client.open_orders("main", symbol=symbol):
            await client.cancel_order("main", symbol, orig_client_order_id=o.client_order_id)
        for pos in await positions_of(client, symbol):
            qty = abs(Decimal(str(pos.position_amt)))
            side = Side.SELL if pos.position_amt > 0 else Side.BUY
            await client.place_order(
                "main",
                PlaceOrderRequest(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=qty,
                    new_order_resp_type="RESULT",
                    position_side=pos.position_side,
                    new_client_order_id=f"manual-close-{pos.position_side.value}",
                ),
            )
        print(f"closed {symbol}")
    finally:
        await client._http.aclose()  # noqa: SLF001
        await db.close()


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    cmd, symbol = sys.argv[1], sys.argv[2]
    if cmd == "open":
        await cmd_open(symbol, Decimal(sys.argv[3]), Decimal(sys.argv[4]))
    elif cmd == "move-stop":
        await cmd_move_stop(symbol, Decimal(sys.argv[3]))
    elif cmd == "partial-close":
        await cmd_partial_close(symbol, Decimal(sys.argv[3]))
    elif cmd == "close":
        await cmd_close(symbol)
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
