"""App-level recovery gate: unresolved exchange outcomes block READY/writes."""

import json
import time

import pytest
import respx

from copytradeaster.app import App
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import CommandRepo

BASE = "https://fapi.asterdex-testnet.com"


@respx.mock
async def test_app_does_not_reconcile_or_publish_ready_with_unresolved_sending(tmp_path):
    db_path = tmp_path / "boot.sqlite3"
    key = bytes.fromhex("ab" * 32)
    db = Database(db_path)
    await db.connect()
    await db.migrate()
    credentials = SqliteCredentials(db, key)
    await credentials.import_identity(
        account_id="main",
        user="0x" + "aa" * 20,
        signer_private_key_hex="0x" + "11" * 32,
        role="master",
        label="main",
    )
    await credentials.import_identity(
        account_id="copyc1",
        user="0x" + "bb" * 20,
        signer_private_key_hex="0x" + "22" * 32,
        role="follower",
        label="copyc1",
    )
    commands = CommandRepo(db)
    await commands.insert(
        "boot-unresolved-key",
        "copyc1",
        None,
        "MARKET_OPEN",
        json.dumps(
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "0.001",
                "newOrderRespType": "RESULT",
            }
        ),
        1,
        "ct-unresolved-app",
    )
    await commands.set_status("boot-unresolved-key", "SENDING")
    await db.close()

    respx.get(f"{BASE}/fapi/v3/time").respond(json={"serverTime": int(time.time() * 1000)})
    respx.get(f"{BASE}/fapi/v3/order").respond(
        status_code=503, json={"code": -1006, "msg": "transport unavailable"}
    )
    mutation = respx.post(f"{BASE}/fapi/v3/order").respond(json={})
    cfg = Settings(
        encryption_key="ab" * 32,
        db_path=db_path,
        log_path=tmp_path / "logs" / "app.log",
        rest_base_url=BASE,
        ws_user_base="wss://fstream.asterdex-testnet.com/ws",
    )
    app = App(cfg)
    try:
        with pytest.raises(RuntimeError, match="boot recovery required"):
            await app.start()
        assert len(mutation.calls) == 0
        assert await app.settings.get_flag("ready") == "false"
        assert (await app.settings.get_flag("degraded_reason")).startswith("boot-unresolved")
    finally:
        await app.stop()
