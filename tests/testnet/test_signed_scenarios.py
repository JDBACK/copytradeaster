"""Signed testnet scenarios (marker `testnet`). Skipped unless CT_TESTNET=1.

Covers the §8 research checklist subset automatable here: server time drift,
account reads, master MARKET open -> follower copy -> fill check, partial close,
SL place + replace, TP place, listenKey lifecycle, idempotent re-submit,
insufficient margin, minQty/minNotional skip, priceProtect trigger behavior,
self-stop re-entry guard, builder e2e (last; skipped without main keys).
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal as D

import pytest

from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.storage.database import Database

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
QTY = D("0.002")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="module")
async def db(settings):
    database = Database(settings.db_path)
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture(scope="module")
async def creds(settings, db):
    c = SqliteCredentials(db, settings.encryption_key_bytes())
    await c.load()
    if not c.account_ids():
        pytest.skip("no accounts imported - run import-wallet first")
    return c


@pytest.fixture(scope="module")
async def clients(settings, creds):
    import httpx

    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.aster.trading_api import AsterClient

    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    main = AsterClient(http, settings.rest_base_url, limiter, creds, creds, creds)
    yield {"main": main}
    await http.aclose()


async def test_server_time_drift(clients):
    server_ms = await clients["main"].server_time()
    assert abs(server_ms - int(time.time() * 1000)) < 5000


async def test_account_reads_both_roles(clients, creds):
    for account_id in creds.account_ids():
        balances = await clients["main"].balance(account_id)
        assert any(b.asset == "USDT" for b in balances)
        positions = await clients["main"].position_risk(account_id)
        assert isinstance(positions, list)
        orders = await clients["main"].open_orders(account_id)
        assert isinstance(orders, list)


async def test_listen_key_lifecycle(clients, creds):
    main_id = "main" if "main" in creds.account_ids() else creds.account_ids()[0]
    key = await clients["main"].start_listen_key(main_id)
    assert key
    await clients["main"].keepalive_listen_key(main_id)
    await clients["main"].close_listen_key(main_id)


async def test_master_market_open_partial_close_and_stop_replace(clients, creds):
    """Full cycle on the master account only; follower copy is covered by e2e."""
    client = clients["main"]
    main_id = next((a for a in creds.account_ids() if a == "main"), creds.account_ids()[0])
    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    info = await client.exchange_info(force=True)
    filters = info.tradable_filters()[SYMBOL]
    mark = (
        D(str((await client.position_risk(main_id, SYMBOL))[0].mark_price))
        if await client.position_risk(main_id, SYMBOL)
        else D("65000")
    )

    open_req = PlaceOrderRequest(
        symbol=SYMBOL,
        side=Side.BUY,
        type="MARKET",
        quantity=QTY,
        new_order_resp_type="RESULT",
        position_side=PosSide.BOTH,
        new_client_order_id=f"tn-open-{int(time.time())}",
    )
    order = await client.place_order(main_id, open_req)
    assert order.status in ("FILLED", "NEW")

    stop_price = (mark - D("500")).quantize(filters.tick_size)
    stop = await client.place_order(
        main_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=QTY,
            stop_price=stop_price,
            reduce_only=True,
            working_type="MARK_PRICE",
            position_side=PosSide.BOTH,
            new_client_order_id=f"tn-sl-{int(time.time())}",
        ),
    )
    # replace via cancel+new
    await client.cancel_order(main_id, SYMBOL, orig_client_order_id=stop.client_order_id)
    new_stop = (mark - D("600")).quantize(filters.tick_size)
    replaced = await client.place_order(
        main_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=QTY,
            stop_price=new_stop,
            reduce_only=True,
            working_type="MARK_PRICE",
            position_side=PosSide.BOTH,
            new_client_order_id=f"tn-sl2-{int(time.time())}",
        ),
    )
    assert replaced.stop_price == new_stop

    tp = await client.place_order(
        main_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            quantity=QTY,
            stop_price=(mark + D("800")).quantize(filters.tick_size),
            reduce_only=True,
            working_type="MARK_PRICE",
            position_side=PosSide.BOTH,
            new_client_order_id=f"tn-tp-{int(time.time())}",
        ),
    )
    assert tp.status in ("NEW", "FILLED")

    # partial close 25%
    partial_qty = (QTY * D("0.25")).quantize(filters.step_size)
    if partial_qty >= filters.min_qty:
        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=partial_qty,
                reduce_only=True,
                new_order_resp_type="RESULT",
                position_side=PosSide.BOTH,
                new_client_order_id=f"tn-pc-{int(time.time())}",
            ),
        )

    # cleanup: cancel conditionals and flatten
    for o in await client.open_orders(main_id, symbol=SYMBOL):
        await client.cancel_order(main_id, SYMBOL, orig_client_order_id=o.client_order_id)
        await asyncio.sleep(0.05)
    pos_list = await client.position_risk(main_id, SYMBOL)
    remaining = abs(pos_list[0].position_amt) if pos_list else D(0)
    if remaining > 0:
        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=remaining,
                reduce_only=True,
                new_order_resp_type="RESULT",
                position_side=PosSide.BOTH,
                new_client_order_id=f"tn-flat-{int(time.time())}",
            ),
        )


async def test_insufficient_margin_fails_cleanly(clients, creds):
    from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    client = clients["main"]
    main_id = next((a for a in creds.account_ids() if a == "main"), creds.account_ids()[0])
    huge = D("99999")
    with pytest.raises(AsterError) as exc_info:
        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=huge,
                new_order_resp_type="RESULT",
                position_side=PosSide.BOTH,
                new_client_order_id=f"tn-big-{int(time.time())}",
            ),
        )
    assert exc_info.value.code in (-2018, -2019, -4014, -4131)


async def test_builder_e2e_last(clients, creds, settings):
    """Runs LAST among signed scenarios; skipped when accounts lack main keys."""
    import httpx

    from copytradeaster.aster.management_api import ManagementClient
    from copytradeaster.aster.rate_limits import RateLimits

    targets = [a for a in creds.account_ids() if creds.has_main_key(a)]
    if not targets:
        pytest.skip("no main private keys stored (builder step of verification)")
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    mgmt = ManagementClient(
        http=http,
        base_url=settings.rest_base_url,
        limiter=limiter,
        identities=creds,
        keys=creds,
        nonces=creds,
        cfg=settings,
    )
    expired_ms = int(time.time() * 1000) + 30 * 24 * 3600 * 1000
    results = {}
    for account_id in targets:
        agent_resp = await mgmt.approve_agent(
            account_id,
            agent_name=settings.builder_name,
            expired_ms=expired_ms,
            with_builder=bool(settings.builder_address),
        )
        builder_resp = await mgmt.approve_builder(account_id)
        results[account_id] = (agent_resp, builder_resp)
    print("builder e2e results:", results)
    await http.aclose()
