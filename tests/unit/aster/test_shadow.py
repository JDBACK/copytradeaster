from decimal import Decimal as D

import pytest

from copytradeaster.aster.shadow import ShadowExchange
from copytradeaster.aster.trading_api import PlaceOrderRequest
from copytradeaster.domain.models import PosSide, Side


@pytest.fixture
def shadow() -> ShadowExchange:
    exchange = ShadowExchange(hedge=True, fill_price=D("65000"))
    exchange.add_account("shadow-001", D("1000"))
    return exchange


async def test_shadow_exchange_isolated_fill_protection_and_cleanup(shadow):
    opened = await shadow.place_order(
        "shadow-001",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            quantity=D("0.002"),
            position_side=PosSide.LONG,
            new_client_order_id="shadow-open",
        ),
    )
    assert opened.status == "FILLED"
    position = await shadow.position_risk("shadow-001", "BTCUSDT")
    assert position[0].position_amt == D("0.002")
    assert position[0].position_side == PosSide.LONG

    stop = await shadow.place_order(
        "shadow-001",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=D("0.002"),
            stop_price=D("64000"),
            position_side=PosSide.LONG,
            new_client_order_id="shadow-stop",
        ),
    )
    assert stop.status == "NEW"
    assert len(await shadow.open_orders("shadow-001", "BTCUSDT")) == 1

    await shadow.cancel_order("shadow-001", "BTCUSDT", orig_client_order_id="shadow-stop")
    await shadow.place_order(
        "shadow-001",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="MARKET",
            quantity=D("0.002"),
            position_side=PosSide.LONG,
            new_client_order_id="shadow-close",
        ),
    )
    assert (await shadow.position_risk("shadow-001", "BTCUSDT")) == []
    assert shadow.audit("shadow-001") == {"positions": [], "open_orders": []}
    assert shadow.signed_requests == 0


async def test_shadow_stop_can_fill_and_expires_other_protection(shadow):
    await shadow.place_order(
        "shadow-001",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            quantity=D("0.002"),
            position_side=PosSide.LONG,
            new_client_order_id="shadow-open-2",
        ),
    )
    await shadow.place_order(
        "shadow-001",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=D("0.002"),
            stop_price=D("64000"),
            position_side=PosSide.LONG,
            new_client_order_id="shadow-stop-2",
        ),
    )
    shadow.set_market_price(D("63999"))
    assert await shadow.position_risk("shadow-001", "BTCUSDT") == []
    assert await shadow.open_orders("shadow-001", "BTCUSDT") == []


async def test_shadow_deterministic_mode_does_not_auto_trigger():
    exchange = ShadowExchange(
        hedge=True,
        fill_price=D("65000"),
        auto_trigger_conditionals=False,
    )
    exchange.add_account("shadow-deterministic", D("1000"))
    await exchange.place_order(
        "shadow-deterministic",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            quantity=D("0.002"),
            position_side=PosSide.LONG,
            new_client_order_id="det-open",
        ),
    )
    await exchange.place_order(
        "shadow-deterministic",
        PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=D("0.002"),
            stop_price=D("64000"),
            position_side=PosSide.LONG,
            new_client_order_id="det-stop",
        ),
    )
    exchange.set_market_price(D("63999"))
    assert (await exchange.position_risk("shadow-deterministic", "BTCUSDT"))[0].position_amt == D(
        "0.002"
    )
    assert [o.client_order_id for o in await exchange.open_orders("shadow-deterministic")] == [
        "det-stop"
    ]
