"""Offline checks of the actual production network and test-collection boundaries."""

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from copytradeaster.app import App
from copytradeaster.config import Settings
from tests.conftest import pytest_collection_modifyitems


def test_all_testnet_paths_are_marked_and_skipped_without_optin(monkeypatch):
    monkeypatch.setenv("CT_TESTNET", "0")
    marks = []
    item = SimpleNamespace(
        path=Path(__file__).parents[1] / "testnet" / "new_unmarked.py", add_marker=marks.append
    )
    pytest_collection_modifyitems([item])
    assert {mark.name for mark in marks} == {"testnet", "skip"}


def test_mixed_profile_rejected_before_app_construction():
    with pytest.raises(ValueError, match="trading chain"):
        Settings(
            _env_file=None,
            encryption_key="ab" * 32,
            rest_base_url="https://fapi.asterdex.com",
            ws_user_base="wss://fstream.asterdex.com/ws",
            allow_mainnet=True,
            testnet_ip_whitelist_override=False,
            trading_chain_id=714,
        )


async def test_mainnet_identity_and_boot_preflight_are_real_wiring(tmp_path):
    cfg = Settings(
        _env_file=None,
        encryption_key="ab" * 32,
        db_path=tmp_path / "app.db",
        log_path=tmp_path / "app.log",
        rest_base_url="https://fapi.asterdex.com",
        ws_user_base="wss://fstream.asterdex.com/ws",
        allow_mainnet=True,
        testnet_ip_whitelist_override=False,
        trading_chain_id=1666,
    )
    app = App(cfg)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000)})
        return httpx.Response(400, json={"code": -2015, "msg": "synthetic preflight denial"})

    await app.http.aclose()
    app.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.master_client = app._make_client()
    try:
        await app.db.connect()
        await app.db.migrate()
        await app.credentials.import_identity(
            account_id="main",
            role="master",
            label="main",
            user="0x" + "aa" * 20,
            signer_private_key_hex="0x" + "11" * 32,
        )
        assert app.credentials.identity("main").chain_id == 1666
        with pytest.raises(RuntimeError, match="mainnet preflight main"):
            await app.start()
        assert not app._ready and not app._entry_admission_ready
        assert ("GET", "/fapi/v3/agent") in calls
        assert all(method == "GET" for method, _ in calls)
    finally:
        await app.stop()
