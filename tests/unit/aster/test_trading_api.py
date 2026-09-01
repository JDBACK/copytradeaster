from decimal import Decimal as D
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import EIP712_DOMAIN_BASE
from copytradeaster.aster.trading_api import (
    AsterClient,
    AsterError,
    PlaceOrderRequest,
    RateLimitError,
)
from copytradeaster.domain.models import PosSide, Side
from tests.unit.aster.conftest import KEY, SIGNER, USER, FakeIdentities, FakeKeys, FakeNonces

BASE = "https://fapi.test"


def make_client(limiter=None, dry_run=False):
    http = httpx.AsyncClient()
    return AsterClient(
        http=http,
        base_url=BASE,
        limiter=limiter or RateLimits(10**9, 10**9, 10**9),
        identities=FakeIdentities(),
        keys=FakeKeys(),
        nonces=FakeNonces(),
        dry_run=dry_run,
    )


def sent_pairs(call) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(call.request.url)).query, keep_blank_values=True)


ORDER_RESPONSE = {
    "orderId": 1,
    "clientOrderId": "ct-x",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "type": "MARKET",
    "status": "FILLED",
    "origQty": "0.002",
    "executedQty": "0.002",
    "avgPrice": "65000",
    "price": "0",
    "stopPrice": "0",
    "reduceOnly": False,
    "closePosition": False,
    "positionSide": "BOTH",
    "workingType": "MARK_PRICE",
    "timeInForce": "GTC",
    "updateTime": 123,
}


class TestToParams:
    def test_omits_none_and_formats_decimal(self):
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=D("0.05"),
            stop_price=D(64500),
            reduce_only=True,
            position_side=PosSide.BOTH,
            new_client_order_id="ct-abc",
            working_type="MARK_PRICE",
        )
        params = req.to_params()
        assert params == {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": "0.05",
            "stopPrice": "64500",
            "reduceOnly": "true",
            "newClientOrderId": "ct-abc",
            "newOrderRespType": "ACK",
            "workingType": "MARK_PRICE",
        }
        assert "price" not in params
        assert "positionSide" not in params
        assert "closePosition" not in params

    def test_hedge_position_side_included(self):
        req = PlaceOrderRequest(
            symbol="ETHUSDT",
            side=Side.SELL,
            type="MARKET",
            quantity=D("1"),
            position_side=PosSide.SHORT,
            new_client_order_id="ct-h",
        )
        assert req.to_params()["positionSide"] == "SHORT"

    def test_decimal_no_exponent(self):
        req = PlaceOrderRequest(
            symbol="X", side=Side.BUY, type="MARKET", quantity=D("1E-3"), new_client_order_id="c"
        )
        assert req.to_params()["quantity"] == "0.001"

    def test_working_type_only_for_conditional(self):
        req = PlaceOrderRequest(
            symbol="X",
            side=Side.BUY,
            type="MARKET",
            quantity=D(1),
            new_client_order_id="c",
            working_type="MARK_PRICE",
        )
        assert "workingType" not in req.to_params()

        stop_limit = PlaceOrderRequest(
            symbol="X",
            side=Side.SELL,
            type="STOP",
            quantity=D(1),
            price=D(99),
            stop_price=D(100),
            working_type="MARK_PRICE",
        )
        assert stop_limit.to_params()["workingType"] == "MARK_PRICE"


class TestSignedRequests:
    @respx.mock
    async def test_signature_present_and_covers_params(self, limiter):
        route = respx.post(f"{BASE}/fapi/v3/order").respond(json=ORDER_RESPONSE)
        client = make_client(limiter)
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            quantity=D("0.002"),
            new_client_order_id="ct-x",
            new_order_resp_type="RESULT",
        )
        order = await client.place_order("copyc1", req)
        assert order.status == "FILLED"
        pairs = sent_pairs(route.calls[0])
        sent = dict(pairs)
        sig = sent.pop("signature")
        # canonical order: business params first, then user/signer/nonce last
        key_names = [k for k, _ in pairs if k != "signature"]
        assert key_names[-3:] == ["user", "signer", "nonce"]
        assert sent["user"] == USER
        assert sent["signer"] == SIGNER
        # rebuild the exact signed string and recover the signer address
        qs = "&".join(f"{k}={v}" for k, v in pairs if k != "signature")
        typed = encode_typed_data(
            domain_data={**EIP712_DOMAIN_BASE, "chainId": 714},
            message_types={"Message": [{"name": "msg", "type": "string"}]},
            message_data={"msg": qs},
        )
        recovered = Account.recover_message(typed, signature="0x" + sig)
        assert recovered == Account.from_key(KEY).address

    @respx.mock
    async def test_param_order_stable(self, limiter):
        route = respx.get(f"{BASE}/fapi/v3/openOrders").respond(json=[])
        client = make_client(limiter)
        await client.open_orders("copyc1", symbol="BTCUSDT")
        await client.open_orders("copyc1", symbol="BTCUSDT")
        p1 = sent_pairs(route.calls[0])
        p2 = sent_pairs(route.calls[1])
        assert [k for k, _ in p1] == [k for k, _ in p2]
        assert dict(p1)["symbol"] == dict(p2)["symbol"] == "BTCUSDT"

    @respx.mock
    async def test_symbol_open_orders_falls_back_to_active_all_orders(self, limiter):
        respx.get(f"{BASE}/fapi/v3/openOrders").respond(
            status_code=400,
            json={"code": -1000, "msg": "System error"},
        )
        all_orders = respx.get(f"{BASE}/fapi/v3/allOrders").respond(
            json=[
                {**ORDER_RESPONSE, "orderId": 11, "status": "NEW"},
                {**ORDER_RESPONSE, "orderId": 12, "status": "PARTIALLY_FILLED"},
                {**ORDER_RESPONSE, "orderId": 13, "status": "FILLED"},
                {**ORDER_RESPONSE, "orderId": 14, "status": "CANCELED"},
            ]
        )
        client = make_client(limiter)

        orders = await client.open_orders("copyc1", symbol="BTCUSDT")

        assert [order.order_id for order in orders] == [11, 12]
        sent = dict(all_orders.calls[0].request.url.params)
        assert sent["symbol"] == "BTCUSDT"
        assert sent["limit"] == "1000"

    @respx.mock
    async def test_all_symbol_open_orders_never_degrades_to_partial_history(self, limiter):
        respx.get(f"{BASE}/fapi/v3/openOrders").respond(
            status_code=400,
            json={"code": -1000, "msg": "System error"},
        )
        history = respx.get(f"{BASE}/fapi/v3/allOrders").respond(json=[])
        client = make_client(limiter)

        with pytest.raises(AsterError) as exc_info:
            await client.open_orders("copyc1")

        assert exc_info.value.code == -1000
        assert not history.called

    @respx.mock
    async def test_query_order_minus2013_returns_none(self):
        respx.get(f"{BASE}/fapi/v3/order").respond(json={"code": -2013, "msg": "no such order"})
        client = make_client()
        assert await client.query_order("copyc1", "BTCUSDT", orig_client_order_id="gone") is None

    @respx.mock
    async def test_429_raises_rate_limit_error(self):
        respx.get(f"{BASE}/fapi/v3/balance").respond(
            status_code=429, json={"code": -1003, "msg": "too many"}
        )
        client = make_client()
        with pytest.raises(RateLimitError):
            await client.balance("copyc1")

    @respx.mock
    async def test_negative_code_raises_aster_error(self):
        respx.post(f"{BASE}/fapi/v3/order").respond(json={"code": -2019, "msg": "margin"})
        client = make_client()
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            quantity=D("1"),
            new_client_order_id="ct-1",
        )
        with pytest.raises(AsterError) as exc_info:
            await client.place_order("copyc1", req)
        assert exc_info.value.code == -2019


class TestDryRun:
    async def test_dry_run_makes_no_http_and_returns_stub(self):
        with respx.mock(assert_all_called=False) as mock:
            route = respx.post(f"{BASE}/fapi/v3/order").respond(json=ORDER_RESPONSE)
            client = make_client(dry_run=True)
            req = PlaceOrderRequest(
                symbol="BTCUSDT",
                side=Side.BUY,
                type="MARKET",
                quantity=D("1"),
                new_client_order_id="ct-d",
                new_order_resp_type="RESULT",
            )
            order = await client.place_order("copyc1", req)
            assert order.status == "DRY_RUN"
            assert order.client_order_id == "ct-d"
            assert len(route.calls) == 0
            del mock


class TestWeights:
    @respx.mock
    async def test_weights_reach_limiter(self):
        seen: list[tuple[str, int]] = []

        class SpyLimiter(RateLimits):
            async def acquire_request(self, weight: int) -> None:
                seen.append(("req", weight))

        client = make_client(SpyLimiter(10**9, 10**9, 10**9), dry_run=True)
        route = respx.get(f"{BASE}/fapi/v3/balance").respond(
            json=[
                {
                    "asset": "USDT",
                    "balance": "1",
                    "crossWalletBalance": "1",
                    "crossUnPnl": "0",
                    "availableBalance": "1",
                    "maxWithdrawAmount": "1",
                }
            ]
        )
        await client.balance("copyc1")
        assert ("req", 5) in seen
        assert len(route.calls) == 1
