"""Endpoint coverage for AsterClient: leverage, margin type, position mode,
listenKey lifecycle, exchangeInfo caching, query by orderId."""

import httpx
import pytest
import respx

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient
from tests.unit.aster.conftest import FakeIdentities, FakeKeys, FakeNonces

BASE = "https://fapi.test"


def make_client():
    return AsterClient(
        http=httpx.AsyncClient(),
        base_url=BASE,
        limiter=RateLimits(10**9, 10**9, 10**9),
        identities=FakeIdentities(),
        keys=FakeKeys(),
        nonces=FakeNonces(),
    )


@respx.mock
async def test_set_leverage_sends_params():
    route = respx.post(f"{BASE}/fapi/v3/leverage").respond(json={"leverage": 20})
    client = make_client()
    await client.set_leverage("copyc1", "BTCUSDT", 20)
    sent = dict(route.calls[0].request.url.params)
    assert sent["symbol"] == "BTCUSDT"
    assert sent["leverage"] == "20"


@respx.mock
async def test_set_margin_type_swallows_no_change():
    respx.post(f"{BASE}/fapi/v3/marginType").respond(
        json={"code": -4046, "msg": "No need to change margin type."}
    )
    client = make_client()
    await client.set_margin_type("copyc1", "BTCUSDT", "CROSSED")  # must not raise


@respx.mock
async def test_set_margin_type_reraises_other_errors():
    respx.post(f"{BASE}/fapi/v3/marginType").respond(json={"code": -1000, "msg": "boom"})
    client = make_client()
    from copytradeaster.aster.trading_api import AsterError

    with pytest.raises(AsterError):
        await client.set_margin_type("copyc1", "BTCUSDT", "CROSSED")


@respx.mock
async def test_position_mode_get_set():
    get_route = respx.get(f"{BASE}/fapi/v3/positionSide/dual").respond(
        json={"dualSidePosition": False}
    )
    post_route = respx.post(f"{BASE}/fapi/v3/positionSide/dual").respond(json={"msg": "success"})
    client = make_client()
    assert await client.get_position_mode("main") is False
    await client.set_position_mode("main", dual=True)
    assert dict(post_route.calls[0].request.url.params)["dualSidePosition"] == "true"
    del get_route


@respx.mock
async def test_listen_key_lifecycle():
    start = respx.post(f"{BASE}/fapi/v3/listenKey").respond(json={"listenKey": "lk-1"})
    keep = respx.put(f"{BASE}/fapi/v3/listenKey").respond(json={})
    close = respx.delete(f"{BASE}/fapi/v3/listenKey").respond(json={})
    client = make_client()
    assert await client.start_listen_key("main") == "lk-1"
    await client.keepalive_listen_key("main")
    await client.close_listen_key("main")
    assert start.called and keep.called and close.called


@respx.mock
async def test_exchange_info_cached():
    body = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "triggerProtect": True,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "100000",
                        "tickSize": "0.1",
                    },
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
                    {"filterType": "MAX_NUM_ORDERS", "limit": 200},
                    {"filterType": "MAX_NUM_ALGO_ORDERS", "limit": 10},
                    {
                        "filterType": "PERCENT_PRICE",
                        "multiplierUp": "1.05",
                        "multiplierDown": "0.95",
                    },
                ],
            }
        ],
        "rateLimits": [
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": 2400,
            }
        ],
    }
    route = respx.get(f"{BASE}/fapi/v3/exchangeInfo").respond(json=body)
    client = make_client()
    info1 = await client.exchange_info()
    await client.exchange_info()
    assert len(route.calls) == 1  # cached
    filters = info1.tradable_filters()["BTCUSDT"]
    assert filters.tick_size.as_tuple().digits[-1] == 1
    assert str(filters.min_notional) == "5"


@respx.mock
async def test_query_order_by_order_id():
    body = {
        "orderId": 7,
        "clientOrderId": "ct-x",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "status": "FILLED",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "65000",
        "price": "0",
        "stopPrice": "0",
        "reduceOnly": False,
        "closePosition": False,
        "positionSide": "BOTH",
        "workingType": "MARK_PRICE",
        "timeInForce": "GTC",
        "updateTime": 5,
    }
    route = respx.get(f"{BASE}/fapi/v3/order").respond(json=body)
    client = make_client()
    order = await client.query_order("copyc1", "BTCUSDT", order_id=7)
    assert order.order_id == 7
    assert dict(route.calls[0].request.url.params)["orderId"] == "7"


@respx.mock
async def test_position_risk_and_open_orders_weights():
    respx.get(f"{BASE}/fapi/v3/positionRisk").respond(json=[])
    respx.get(f"{BASE}/fapi/v3/openOrders").respond(json=[])
    client = make_client()
    await client.position_risk("copyc1")
    await client.open_orders("copyc1")  # no symbol -> weight 40 path
