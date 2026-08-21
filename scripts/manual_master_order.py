"""Manual master actions for reproducible testnet smoke runs.

Usage:
  python scripts/manual_master_order.py open BTCUSDT 0.002 0.5   (qty, stop%)
  python scripts/manual_master_order.py move-stop BTCUSDT 63000
  python scripts/manual_master_order.py partial-close BTCUSDT 30  (percent)
  python scripts/manual_master_order.py close BTCUSDT
"""

from __future__ import annotations

import asyncio
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
from copytradeaster.storage.repositories import AccountRepo  # noqa: E402


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


async def cmd_open(symbol: str, qty: Decimal, stop_pct: Decimal) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        price = Decimal(str((await client.position_risk("main", symbol))[0].mark_price))
        stop = (price * (Decimal(1) - stop_pct / 100)).quantize(filters.tick_size)
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.BUY,
                type="MARKET",
                quantity=qty,
                new_order_resp_type="RESULT",
                position_side=PosSide.BOTH,
                new_client_order_id=f"manual-open-{int(price)}",
            ),
        )
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=qty,
                stop_price=stop,
                reduce_only=True,
                working_type="MARK_PRICE",
                position_side=PosSide.BOTH,
                new_client_order_id=f"manual-stop-{int(stop)}",
            ),
        )
        print(f"opened {symbol} {qty} @~{price}, stop {stop}")
    finally:
        await client._http.aclose()
        await db.close()


async def cmd_move_stop(symbol: str, new_stop: Decimal) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        orders = await client.open_orders("main", symbol=symbol)
        old = [o for o in orders if o.type == "STOP_MARKET"]
        positions = await client.position_risk("main", symbol)
        qty = abs(Decimal(str(positions[0].position_amt))) if positions else Decimal(0)
        if old:
            await client.cancel_order("main", symbol, orig_client_order_id=old[0].client_order_id)
        new_stop = new_stop.quantize(filters.tick_size)
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=qty,
                stop_price=new_stop,
                reduce_only=True,
                working_type="MARK_PRICE",
                position_side=PosSide.BOTH,
                new_client_order_id=f"manual-stop-{int(new_stop)}",
            ),
        )
        print(f"moved stop to {new_stop} (qty {qty})")
    finally:
        await client._http.aclose()
        await db.close()


async def cmd_partial_close(symbol: str, pct: Decimal) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        positions = await client.position_risk("main", symbol)
        pos = positions[0]
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
                reduce_only=True,
                new_order_resp_type="RESULT",
                position_side=PosSide.BOTH,
                new_client_order_id=f"manual-pc-{pct}",
            ),
        )
        print(f"partial close {pct}% -> {qty}")
    finally:
        await client._http.aclose()
        await db.close()


async def cmd_close(symbol: str) -> None:
    client, db = await make_client()
    try:
        orders = await client.open_orders("main", symbol=symbol)
        for o in orders:
            await client.cancel_order("main", symbol, orig_client_order_id=o.client_order_id)
        positions = await client.position_risk("main", symbol)
        for pos in positions:
            qty = abs(Decimal(str(pos.position_amt)))
            if qty == 0:
                continue
            side = Side.SELL if pos.position_amt > 0 else Side.BUY
            await client.place_order(
                "main",
                PlaceOrderRequest(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=qty,
                    reduce_only=True,
                    new_order_resp_type="RESULT",
                    position_side=PosSide.BOTH,
                    new_client_order_id="manual-close",
                ),
            )
        print(f"closed {symbol}")
    finally:
        await client._http.aclose()
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
    del AccountRepo
