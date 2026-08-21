"""Coverage for the heavier CLI commands (reconcile, emergency-close-all,
approve-builder, run dispatch) with respx intercepting all HTTP."""

from pathlib import Path

import pytest
import respx

from copytradeaster.cli.main import _approve_builder, _emergency_close_all, _reconcile
from copytradeaster.storage.database import Database

BASE = "https://fapi.asterdex-testnet.com"

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.001",
                    "maxQty": "1000",
                    "stepSize": "0.001",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    ],
    "rateLimits": [],
}

ORDER_OK = {
    "orderId": 1,
    "clientOrderId": "ct-x",
    "symbol": "BTCUSDT",
    "side": "SELL",
    "type": "MARKET",
    "status": "FILLED",
    "origQty": "0.05",
    "executedQty": "0.05",
    "avgPrice": "65000",
    "price": "0",
    "stopPrice": "0",
    "reduceOnly": True,
    "closePosition": False,
    "positionSide": "BOTH",
    "workingType": "MARK_PRICE",
    "timeInForce": "GTC",
    "updateTime": 1,
}

POSITION_OPEN = {
    "symbol": "BTCUSDT",
    "positionAmt": "0.05",
    "entryPrice": "65000",
    "markPrice": "65000",
    "leverage": "20",
    "liquidationPrice": "100",
    "marginType": "cross",
    "positionSide": "BOTH",
    "unRealizedProfit": "0",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CT_DB_PATH", str(tmp_path / "heavy.sqlite3"))
    monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.setenv("CT_REST_BASE_URL", BASE)
    return tmp_path


async def seed_account(env):
    from copytradeaster.credentials.service import SqliteCredentials

    db = Database(Path(__import__("os").environ["CT_DB_PATH"]))
    await db.connect()
    await db.migrate()
    creds = SqliteCredentials(db, bytes.fromhex("ab" * 32))
    await creds.import_identity(
        account_id="main",
        user="0x" + "bb" * 20,
        signer_private_key_hex="0x" + "11" * 32,
        role="master",
        label="main test",
        main_private_key_hex="0x" + "55" * 32,
    )
    await creds.import_identity(
        account_id="copyc1",
        user="0x" + "aa" * 20,
        signer_private_key_hex="0x" + "33" * 32,
        role="follower",
        label="copyc1",
    )
    await db.close()


@respx.mock
async def test_reconcile_creates_and_reports(env, capsys):
    await seed_account(env)
    respx.get(f"{BASE}/fapi/v3/time").respond(json={"serverTime": 1_000})
    respx.get(f"{BASE}/fapi/v3/exchangeInfo").respond(json=EXCHANGE_INFO)
    respx.get(f"{BASE}/fapi/v3/balance").respond(
        json=[
            {
                "asset": "USDT",
                "balance": "1000",
                "crossWalletBalance": "1000",
                "crossUnPnl": "0",
                "availableBalance": "1000",
                "maxWithdrawAmount": "1000",
            }
        ]
    )
    respx.get(f"{BASE}/fapi/v3/positionRisk").respond(json=[])
    respx.get(f"{BASE}/fapi/v3/openOrders").respond(json=[])
    rc = await _reconcile(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "copyc1: commands created: 0" in out


@respx.mock
async def test_emergency_close_all_flattens(env, capsys):
    await seed_account(env)
    respx.get(f"{BASE}/fapi/v3/time").respond(json={"serverTime": 1_000})
    respx.get(f"{BASE}/fapi/v3/exchangeInfo").respond(json=EXCHANGE_INFO)
    respx.get(f"{BASE}/fapi/v3/balance").respond(
        json=[
            {
                "asset": "USDT",
                "balance": "1000",
                "crossWalletBalance": "1000",
                "crossUnPnl": "0",
                "availableBalance": "1000",
                "maxWithdrawAmount": "1000",
            }
        ]
    )
    respx.get(f"{BASE}/fapi/v3/positionRisk").respond(json=[POSITION_OPEN])
    respx.get(f"{BASE}/fapi/v3/openOrders").respond(json=[])
    respx.post(f"{BASE}/fapi/v3/order").respond(json=ORDER_OK)
    rc = await _emergency_close_all()
    out = capsys.readouterr().out
    assert rc == 0
    assert "closed:" in out


@respx.mock
async def test_approve_builder_reports_responses(env, capsys):
    await seed_account(env)
    # add main key by re-import with main key
    from copytradeaster.credentials.service import SqliteCredentials

    db = Database(Path(__import__("os").environ["CT_DB_PATH"]))
    await db.connect()
    creds = SqliteCredentials(db, bytes.fromhex("ab" * 32))
    await creds.load()
    await creds.import_identity(
        account_id="copyc1",
        user="0x" + "aa" * 20,
        signer_private_key_hex="0x" + "33" * 32,
        role="follower",
        label="copyc1",
        main_private_key_hex="0x" + "44" * 32,
    )
    await db.close()

    respx.post(f"{BASE}/fapi/v3/approveAgent").respond(json={"code": 0, "msg": "ok"})
    respx.post(f"{BASE}/fapi/v3/approveBuilder").respond(json={"code": 0, "msg": "ok"})
    rc = await _approve_builder("all", "0.0001")
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "approveAgent" in captured.out and "approveBuilder" in captured.out


@respx.mock
async def test_approve_builder_skips_without_main_key(env, capsys):
    await seed_account(env)
    rc = await _approve_builder("all", None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "WARN" in out and "skipped" in out
