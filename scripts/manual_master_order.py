"""Manual master actions for reproducible testnet smoke runs.

Commands:
  python scripts/manual_master_order.py open BTCUSDT 0.002 0.5 [long|short]
  python scripts/manual_master_order.py open-nostop BTCUSDT 0.002 [long|short]
  python scripts/manual_master_order.py set-stop BTCUSDT 78000     (place when absent)
  python scripts/manual_master_order.py move-stop BTCUSDT 78000    (replace existing)
  python scripts/manual_master_order.py set-tp BTCUSDT 78500       (place when absent)
  python scripts/manual_master_order.py move-tp BTCUSDT 78500      (replace existing)
  python scripts/manual_master_order.py partial-close BTCUSDT 30  (percent)
  python scripts/manual_master_order.py close BTCUSDT
"""

from __future__ import annotations

import asyncio
import secrets
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
from copytradeaster.storage.repositories import NonceStore  # noqa: E402


def _order_marker() -> str:
    """Unique per order so a repeated price never reuses a clientOrderId.

    Aster answers a reused clientOrderId with -4015 while the mark price sits
    still, so the id needs a varying suffix. The old
    ``int(time.time()) % 100000`` collided for two operations in the same
    second and repeated the whole suffix after ~27.8h. Random hex keeps the
    operator-readable prefixes and is unique for the life of the run.
    """
    return secrets.token_hex(4)


def hedge() -> bool:
    return Settings().position_mode == "HEDGE"


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
    http = httpx.AsyncClient(timeout=httpx.Timeout(20))
    client = AsterClient(
        http,
        cfg.rest_base_url,
        RateLimits(cfg.weight_limit_per_min, cfg.orders_limit_per_min, cfg.orders_limit_per_10s),
        creds,
        creds,
        NonceStore(db),
    )
    return client, db


async def get_filters(client: AsterClient, symbol: str) -> SymbolFilters:
    info = await client.exchange_info()
    filters = info.tradable_filters()
    if symbol not in filters:
        raise SystemExit(f"no filters for {symbol}")
    return filters[symbol]


async def positions_of(client: AsterClient, symbol: str):
    return [p for p in await client.position_risk("main", symbol) if p.position_amt != 0]


async def _fallback_price(client: AsterClient, symbol: str) -> Decimal:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(
            f"{Settings().rest_base_url}/fapi/v3/ticker/price",
            params={"symbol": symbol},
        )
        data = r.json()
        return Decimal(str(data.get("price", "65000")))


async def _mark_price(client: AsterClient, symbol: str) -> Decimal:
    marks = await client.position_risk("main", symbol)
    price = next((Decimal(str(m.mark_price)) for m in marks if m.mark_price), None)
    return price or await _fallback_price(client, symbol)


def _close_side(pos_amt: Decimal) -> Side:
    return Side.SELL if pos_amt > 0 else Side.BUY


async def _place_protective(
    client: AsterClient,
    symbol: str,
    pos,
    kind: str,
    price: Decimal,
    tick: Decimal,
) -> None:
    price_q = price.quantize(tick)
    # Same -4015 hazard as the entry: a repeated protective id is rejected.
    coid = f"manual-{kind}-{int(price_q)}-{_order_marker()}"
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=symbol,
            side=_close_side(pos.position_amt),
            type=kind,
            quantity=abs(Decimal(str(pos.position_amt))),
            stop_price=price_q,
            working_type="MARK_PRICE",
            position_side=pos.position_side,
            new_client_order_id=coid,
        ),
    )


async def cmd_open(
    symbol: str, qty: Decimal, stop_pct: Decimal | None, direction: str = "long"
) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        price = await _mark_price(client, symbol)
        short = direction == "short"
        entry_side = Side.SELL if short else Side.BUY
        if stop_pct is not None:
            stop = (
                price * ((Decimal(1) + stop_pct / 100) if short else (Decimal(1) - stop_pct / 100))
            ).quantize(filters.tick_size)
        else:
            stop = None
        ps = leg_side(PosSide.SHORT if short else PosSide.LONG)
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=entry_side,
                type="MARKET",
                quantity=qty,
                new_order_resp_type="RESULT",
                position_side=ps,
                # Aster rejects a clientOrderId it has already seen with
                # -4015 while the mark price sits still between runs, so the
                # id must carry a varying suffix rather than the price alone.
                new_client_order_id=f"manual-open-{int(price)}-{_order_marker()}",
            ),
        )
        if stop_pct is not None:
            await _place_protective(
                client,
                symbol,
                type(
                    "P",
                    (),
                    {
                        "position_amt": -qty if short else qty,
                        "position_side": ps,
                    },
                )(),
                "STOP_MARKET",
                stop,
                filters.tick_size,
            )
        print(
            f"opened {symbol} {qty} {'SHORT' if short else 'LONG'} @~{price}"
            f"{f', stop {stop}' if stop_pct is not None else ', NO STOP'}"
        )
    finally:
        await client._http.aclose()  # noqa: SLF001 - script-level access
        await db.close()


async def _find_position(client: AsterClient, symbol: str):
    poss = await positions_of(client, symbol)
    if not poss:
        raise SystemExit(f"no open position on {symbol}")
    return poss[0]


async def cmd_set_stop(symbol: str, price: Decimal, replace: bool) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        orders = await client.open_orders("main", symbol=symbol)
        olds = [o for o in orders if o.type == "STOP_MARKET"]
        if olds and not replace:
            raise SystemExit(f"stop already exists: {olds[0].client_order_id}")
        if not olds and replace:
            raise SystemExit("no STOP_MARKET to move")
        pos = await _find_position(client, symbol)
        if olds:
            # place-new BEFORE cancel-old: rejected new level keeps protection
            await _place_protective(client, symbol, pos, "STOP_MARKET", price, filters.tick_size)
            await client.cancel_order("main", symbol, orig_client_order_id=olds[0].client_order_id)
        else:
            await _place_protective(client, symbol, pos, "STOP_MARKET", price, filters.tick_size)
        print(f"{'moved' if replace else 'set'} stop to {price}")
    finally:
        await client._http.aclose()  # noqa: SLF001
        await db.close()


async def cmd_set_tp(symbol: str, price: Decimal, replace: bool) -> None:
    client, db = await make_client()
    try:
        filters = await get_filters(client, symbol)
        orders = await client.open_orders("main", symbol=symbol)
        olds = [o for o in orders if o.type == "TAKE_PROFIT_MARKET"]
        if olds and not replace:
            raise SystemExit(f"TP already exists: {olds[0].client_order_id}")
        if not olds and replace:
            raise SystemExit("no TAKE_PROFIT_MARKET to move")
        pos = await _find_position(client, symbol)
        if olds:
            await _place_protective(
                client, symbol, pos, "TAKE_PROFIT_MARKET", price, filters.tick_size
            )
            await client.cancel_order("main", symbol, orig_client_order_id=olds[0].client_order_id)
        else:
            await _place_protective(
                client, symbol, pos, "TAKE_PROFIT_MARKET", price, filters.tick_size
            )
        print(f"{'moved' if replace else 'set'} TP to {price}")
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
        if qty < filters.min_qty:
            raise SystemExit(
                f"partial size {qty} is below min step {filters.step_size}; increase pct"
            )
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=symbol,
                side=_close_side(pos.position_amt),
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
            await client.place_order(
                "main",
                PlaceOrderRequest(
                    symbol=symbol,
                    side=_close_side(pos.position_amt),
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
        direction = sys.argv[5] if len(sys.argv) > 5 else "long"
        await cmd_open(symbol, Decimal(sys.argv[3]), Decimal(sys.argv[4]), direction)
    elif cmd == "open-nostop":
        direction = sys.argv[4] if len(sys.argv) > 4 else "long"
        await cmd_open(symbol, Decimal(sys.argv[3]), None, direction)
    elif cmd in ("set-stop", "move-stop"):
        await cmd_set_stop(symbol, Decimal(sys.argv[3]), replace=cmd == "move-stop")
    elif cmd in ("set-tp", "move-tp"):
        await cmd_set_tp(symbol, Decimal(sys.argv[3]), replace=cmd == "move-tp")
    elif cmd == "partial-close":
        await cmd_partial_close(symbol, Decimal(sys.argv[3]))
    elif cmd == "close":
        await cmd_close(symbol)
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
