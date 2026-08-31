"""Transport tests for the Mini App aiohttp server (Stage 3 part 1)."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import (
    BuilderOnboardedDTO,
    BuilderPrepareDTO,
    MiniAppAccessState,
    MiniAppSessionDTO,
)
from copytradeaster.config import Settings
from copytradeaster.miniapp.server import create_miniapp_app

BOT_TOKEN = "123456:TEST-TOKEN"


def _make_init_data(token: str, user_id: int) -> str:
    """Build initData exactly the way Telegram signs it (for tests only)."""
    pairs = {
        "auth_date": "1700000000",
        "query_id": "AAF" + "0" * 10,
        "user": f'{{"id":{user_id},"first_name":"T"}}',
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


@dataclass
class FakeApplication:
    """Records calls and returns canned DTOs; mimics ApplicationProtocol."""

    user_id_seen: int | None = None
    fail_with: Exception | None = None
    submit_fail_with: Exception | None = None
    session_dto: MiniAppSessionDTO = field(
        default_factory=lambda: MiniAppSessionDTO(
            user_id=777,
            access_state=MiniAppAccessState.APPROVED,
            environment="testnet",
            account_connected=False,
        )
    )
    prepare_calls: list = field(default_factory=list)
    submit_calls: list = field(default_factory=list)

    async def miniapp_session(self, user_id: int) -> MiniAppSessionDTO:
        self.user_id_seen = user_id
        if self.fail_with:
            raise self.fail_with
        return self.session_dto

    async def builder_prepare(self, user_id: int, user_address: str) -> BuilderPrepareDTO:
        self.prepare_calls.append((user_id, user_address))
        return BuilderPrepareDTO(pending_id="pid-1", user_masked="0xABC…DEF", eip712={})

    async def builder_submit(
        self, user_id: int, pending_id: str, signature: str
    ) -> BuilderOnboardedDTO:
        self.submit_calls.append((user_id, pending_id, signature))
        if self.submit_fail_with:
            raise self.submit_fail_with
        return BuilderOnboardedDTO(
            account_id="tg-777",
            account_masked="0xABC…DEF",
            signer_masked="0x111…222",
            builder_name="copytradeaster",
            max_fee_rate="0.0001",
            position_mode="ONE_WAY",
            available_usdt=1000,
            agent_expires_at_ms=0,
            paused=True,
        )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        encryption_key="00" * 32,
        telegram_bot_token=BOT_TOKEN,
        miniapp_enabled=True,
    )


@pytest.fixture
def fake_app() -> FakeApplication:
    return FakeApplication()


@pytest.fixture
async def client(settings, fake_app):
    app = create_miniapp_app(settings, fake_app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


def _auth_header(init_data: str) -> dict:
    return {"X-Telegram-Init-Data": init_data}


async def test_session_rejects_missing_or_bad_init_data(client, fake_app):
    resp = await client.post("/miniapp/api/session")
    assert resp.status == 401
    resp = await client.post("/miniapp/api/session", headers=_auth_header("hash=deadbeef"))
    assert resp.status == 401
    assert fake_app.user_id_seen is None


async def test_session_verifies_init_data_and_derives_user_id(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post("/miniapp/api/session", headers=_auth_header(init_data))
    assert resp.status == 200
    body = await resp.json()
    assert fake_app.user_id_seen == 777
    assert body["access_state"] == "approved"
    assert body["environment"] == "testnet"


async def test_init_data_signed_by_other_token_is_rejected(client, fake_app):
    init_data = _make_init_data("999:OTHER-TOKEN", 777)
    resp = await client.post("/miniapp/api/session", headers=_auth_header(init_data))
    assert resp.status == 401


async def test_builder_prepare_passes_address_and_returns_dto(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/builder/prepare",
        json={"user_address": "0xABC"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 200
    body = await resp.json()
    assert fake_app.prepare_calls == [(777, "0xABC")]
    assert body["pending_id"] == "pid-1"


async def test_builder_prepare_requires_auth(client, fake_app):
    resp = await client.post("/miniapp/api/builder/prepare", json={"user_address": "0xABC"})
    assert resp.status == 401
    assert fake_app.prepare_calls == []


async def test_builder_submit_maps_to_typed_error(client, fake_app):
    fake_app.submit_fail_with = RuntimeError("boom")
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/builder/submit",
        json={"pending_id": "p", "signature": "0xsig"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "RuntimeError"


async def test_builder_submit_success_returns_dto(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/builder/submit",
        json={"pending_id": "pid-9", "signature": "0xdeadbeef"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 200
    body = await resp.json()
    assert fake_app.submit_calls == [(777, "pid-9", "0xdeadbeef")]
    assert body["paused"] is True
    assert body["account_id"] == "tg-777"


async def test_malformed_json_body_is_400(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/builder/prepare",
        data="not-json",
        headers={**_auth_header(init_data), "Content-Type": "application/json"},
    )
    assert resp.status == 400
