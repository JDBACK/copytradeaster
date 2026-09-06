from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from copytradeaster.aster.trading_api import AsterError
from tests.unit.aster.test_trading_api import make_client

FILL = {
    "id": 9007199254740993,
    "orderId": 123,
    "symbol": "BTCUSDT",
    "side": "SELL",
    "positionSide": "LONG",
    "qty": "0.002",
    "price": "7819.01",
    "quoteQty": "15.63802",
    "realizedPnl": "-0.91539999",
    "commission": "-0.07819010",
    "commissionAsset": "USDT",
    "time": 100,
}
FLOW = {
    "symbol": "",
    "incomeType": "TRANSFER",
    "income": "-0.37500000",
    "asset": "USDT",
    "time": 100,
    "tranId": "9689322392",
    "tradeId": "",
}


async def test_history_preserves_exact_facts_and_uses_weighted_signed_reads():
    client = make_client()
    client._signed = AsyncMock(return_value=[FILL])
    rows = await client.user_trades("a", "BTCUSDT", from_id=9007199254740993)
    assert rows[0].trade_id == 9007199254740993
    assert rows[0].commission == Decimal("-0.07819010")
    client._signed.assert_awaited_once_with(
        "a",
        "GET",
        "/fapi/v3/userTrades",
        {"limit": "1000", "symbol": "BTCUSDT", "fromId": "9007199254740993"},
        weight=5,
    )
    client._signed.reset_mock(return_value=True)
    client._signed.return_value = [FLOW]
    rows = await client.income_history("a", start_time=0, end_time=100)
    assert rows[0].income == Decimal("-0.375")
    assert rows[0].trade_id == ""
    client._signed.assert_awaited_once_with(
        "a",
        "GET",
        "/fapi/v3/income",
        {"limit": "1000", "startTime": "0", "endTime": "100"},
        weight=30,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": 1001},
        {"limit": True},
        {"start_time": -1},
        {"start_time": 10, "end_time": 1},
        {"start_time": 0, "end_time": 604800001},
        {"from_id": 1, "start_time": 0},
        {"from_id": False},
    ],
)
async def test_invalid_window_never_calls_transport(overrides):
    client = make_client()
    client._signed = AsyncMock()
    with pytest.raises(ValueError):
        await client.user_trades("a", "BTCUSDT", **overrides)
    client._signed.assert_not_called()


@pytest.mark.parametrize(
    "patch",
    [
        {"qty": "NaN"},
        {"price": "Infinity"},
        {"qty": 0.002},
        {"qty": "0"},
        {"id": True},
        {"time": -1},
        {"symbol": "ETHUSDT"},
        {"commissionAsset": ""},
    ],
)
async def test_bad_fill_rejects_entire_page_without_payload_in_error(patch):
    client = make_client()
    client._signed = AsyncMock(return_value=[FILL, {**FILL, **patch}])
    with pytest.raises(AsterError, match="^-1000: invalid user trades response$"):
        await client.user_trades("a", "BTCUSDT")


@pytest.mark.parametrize("body", [{}, [{**FLOW, "tranId": 1.2}], [{**FLOW, "income": "NaN"}]])
async def test_bad_income_rejects_page(body):
    client = make_client()
    client._signed = AsyncMock(return_value=body)
    with pytest.raises(AsterError, match="invalid income history response"):
        await client.income_history("a", start_time=0, end_time=100)
