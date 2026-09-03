"""Transport tests for the P2.6 Account + Builder Mini App endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import (
    AccountBuilderDTO,
    AccountBuilderReason,
    AccountBuilderState,
    ApplicationError,
    AuthorizationError,
    BuilderPrepareDTO,
    BuilderReauthorizedDTO,
    PermissionSummary,
    ServiceUnavailableError,
)
from copytradeaster.config import Settings
from copytradeaster.miniapp.server import create_miniapp_app

BOT_TOKEN = "123456:TEST-TOKEN"


def _make_init_data(token: str, user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF" + "0" * 10,
        "user": f'{{"id":{user_id},"first_name":"T"}}',
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


def _builder_dto() -> AccountBuilderDTO:
    return AccountBuilderDTO(
        identity_source="builder",
        state=AccountBuilderState.VERIFIED,
        reason=AccountBuilderReason.NONE,
        remediation=None,
        account_masked="0x1111…1111",
        signer_masked="0x2222…2222",
        builder_address_masked="0x2576…6EF8",
        builder_name="copytradeaster",
        max_fee_rate="0.0001",
        permission=PermissionSummary(
            can_read=True,
            can_perp_trade=True,
            can_withdraw=False,
            whitelist_ok=True,
            checked_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        ),
        agent_expires_at_ms=2_000_000_000_000,
        verified_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        last_check_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        age_seconds=0,
    )


@dataclass
class FakeBuilderApplication:
    dto: AccountBuilderDTO = field(default_factory=_builder_dto)
    fail_with: Exception | None = None
    builder_calls: list = field(default_factory=list)
    prepare_calls: list = field(default_factory=list)
    submit_calls: list = field(default_factory=list)

    async def account_builder(self, user_id: int, refresh: bool = False) -> AccountBuilderDTO:
        self.builder_calls.append((user_id, refresh))
        if self.fail_with is not None:
            raise self.fail_with
        return self.dto

    async def builder_reauthorize_prepare(self, user_id: int) -> BuilderPrepareDTO:
        self.prepare_calls.append(user_id)
        if self.fail_with is not None:
            raise self.fail_with
        return BuilderPrepareDTO(pending_id="reauth-test-1", builder_name="copytradeaster")

    async def builder_reauthorize_submit(
        self, user_id: int, pending_id: str, signature: str
    ) -> BuilderReauthorizedDTO:
        self.submit_calls.append((user_id, pending_id, signature))
        if self.fail_with is not None:
            raise self.fail_with
        return BuilderReauthorizedDTO(
            account_id="tg-777",
            account_masked="0x1111…1111",
            signer_masked="0x2222…2222",
            builder_name="copytradeaster",
            max_fee_rate="0.0001",
            agent_expires_at_ms=2_000_000_000_000,
            paused=True,
        )


def _settings() -> Settings:
    return Settings(encryption_key="00" * 32, telegram_bot_token=BOT_TOKEN, miniapp_enabled=True)


@pytest.fixture
def fake_app() -> FakeBuilderApplication:
    return FakeBuilderApplication()


@pytest.fixture
async def client(fake_app):
    app = create_miniapp_app(_settings(), fake_app)  # type: ignore[arg-type]
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


def _auth(user_id: int = 777) -> dict:
    return {"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, user_id)}


async def test_account_builder_requires_auth(client):
    resp = await client.post("/miniapp/api/account-builder", json={})
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_account_builder_rejects_forged_signature(client, fake_app):
    bad = _make_init_data("123456:WRONG-TOKEN", 777)
    resp = await client.post(
        "/miniapp/api/account-builder", json={}, headers={"X-Telegram-Init-Data": bad}
    )
    assert resp.status == 401
    assert fake_app.builder_calls == []


async def test_account_builder_empty_body_is_refresh_false(client, fake_app):
    resp = await client.post("/miniapp/api/account-builder", json={}, headers=_auth())
    assert resp.status == 200
    assert fake_app.builder_calls == [(777, False)]


async def test_account_builder_exact_booleans_route_to_service(client, fake_app):
    resp = await client.post(
        "/miniapp/api/account-builder", json={"refresh": True}, headers=_auth()
    )
    assert resp.status == 200
    resp = await client.post(
        "/miniapp/api/account-builder", json={"refresh": False}, headers=_auth()
    )
    assert resp.status == 200
    assert fake_app.builder_calls == [(777, True), (777, False)]


@pytest.mark.parametrize(
    "payload",
    [
        {"refresh": "true"},
        {"refresh": 1},
        {"refresh": None},
        {"refresh": [True]},
        {"refresh": True, "account_id": "tg-777"},
        {"refresh": True, "force": True},
        {"refresh": True, "reauthorize": True},
        {"account_id": "tg-777"},
        {"extra": 1},
    ],
)
async def test_account_builder_rejects_non_boolean_and_forged_bodies(client, fake_app, payload):
    resp = await client.post("/miniapp/api/account-builder", json=payload, headers=_auth())
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_input"
    assert fake_app.builder_calls == []


async def test_account_builder_returns_exact_safe_dto(client):
    resp = await client.post(
        "/miniapp/api/account-builder", json={"refresh": True}, headers=_auth()
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "identity_source": "builder",
        "state": "verified",
        "reason": "none",
        "remediation": None,
        "account_masked": "0x1111…1111",
        "signer_masked": "0x2222…2222",
        "builder_address_masked": "0x2576…6EF8",
        "builder_name": "copytradeaster",
        "max_fee_rate": "0.0001",
        "permission": {
            "can_read": True,
            "can_perp_trade": True,
            "can_withdraw": False,
            "whitelist_ok": True,
            "checked_at": "2026-09-03T12:00:00+00:00",
            "warning": None,
        },
        "agent_expires_at_ms": 2_000_000_000_000,
        "verified_at": "2026-09-03T12:00:00+00:00",
        "last_check_at": "2026-09-03T12:00:00+00:00",
        "age_seconds": 0,
    }
    for forbidden in (
        "account_id",
        "user_address",
        "signer_address",
        "builder_address",
        "private_key",
        "signature",
        "nonce",
        "payload",
        "eip712",
        "traceback",
    ):
        assert forbidden not in body
    assert "0x1111111111111111111111111111111111111111" not in json.dumps(body)


async def test_account_builder_maps_application_error_to_safe_code(client, fake_app):
    fake_app.fail_with = ApplicationError("boom-secret-must-not-leak")
    resp = await client.post("/miniapp/api/account-builder", json={}, headers=_auth())
    assert resp.status == 400
    body = await resp.json()
    assert "boom-secret-must-not-leak" not in json.dumps(body)


async def test_account_builder_maps_authorization_and_unavailable(client, fake_app):
    fake_app.fail_with = AuthorizationError("not yours")
    resp = await client.post("/miniapp/api/account-builder", json={}, headers=_auth())
    assert resp.status == 403

    fake_app.fail_with = ServiceUnavailableError("engine down")
    resp = await client.post("/miniapp/api/account-builder", json={}, headers=_auth())
    assert resp.status == 503
    assert "engine down" not in json.dumps(await resp.json())


async def test_account_builder_uses_telegram_identity(client, fake_app):
    resp = await client.post("/miniapp/api/account-builder", json={}, headers=_auth(778))
    assert resp.status == 200
    assert fake_app.builder_calls == [(778, False)]


async def test_reauthorize_prepare_requires_auth(client, fake_app):
    resp = await client.post("/miniapp/api/builder/reauthorize-prepare", json={})
    assert resp.status == 401
    assert fake_app.prepare_calls == []


async def test_reauthorize_prepare_ignores_client_fields(client, fake_app):
    # The body is never read: account/user/signer/builder come from the
    # Telegram identity, so there is no force flag to forge.
    resp = await client.post(
        "/miniapp/api/builder/reauthorize-prepare",
        json={"force": True, "builder": "0xevil", "max_fee_rate": "0.9999"},
        headers=_auth(),
    )
    assert resp.status == 200
    assert fake_app.prepare_calls == [777]
    body = await resp.json()
    assert body["pending_id"] == "reauth-test-1"
    assert body["builder_name"] == "copytradeaster"
    assert "0xevil" not in json.dumps(body)
    assert "0.9999" not in json.dumps(body)


async def test_reauthorize_submit_requires_strict_body(client, fake_app):
    for payload in (
        {"pending_id": "reauth-1"},
        {"signature": "0xabc"},
        {"pending_id": "reauth-1", "signature": "0xabc", "builder": "0xevil"},
        {"pending_id": "", "signature": "0xabc"},
        {"pending_id": "reauth-1", "signature": ""},
        {"pending_id": 1, "signature": "0xabc"},
    ):
        resp = await client.post(
            "/miniapp/api/builder/reauthorize-submit", json=payload, headers=_auth()
        )
        assert resp.status == 400
    assert fake_app.submit_calls == []


async def test_reauthorize_submit_routes_exact_fields(client, fake_app):
    resp = await client.post(
        "/miniapp/api/builder/reauthorize-submit",
        json={"pending_id": "reauth-test-1", "signature": "0x" + "ab" * 65},
        headers=_auth(),
    )
    assert resp.status == 200
    assert fake_app.submit_calls == [(777, "reauth-test-1", "0x" + "ab" * 65)]
    body = await resp.json()
    assert body["paused"] is True
    assert body["account_id"] == "tg-777"
    for forbidden in ("private_key", "signature", "nonce", "traceback"):
        assert forbidden not in body


async def test_reauthorize_prepare_maps_safe_failures(client, fake_app):
    fake_app.fail_with = ServiceUnavailableError("engine down")
    resp = await client.post("/miniapp/api/builder/reauthorize-prepare", json={}, headers=_auth())
    assert resp.status == 503
    assert "engine down" not in json.dumps(await resp.json())

    fake_app.fail_with = ApplicationError("boom-secret-must-not-leak")
    resp = await client.post("/miniapp/api/builder/reauthorize-prepare", json={}, headers=_auth())
    assert resp.status == 400
    assert "boom-secret-must-not-leak" not in json.dumps(await resp.json())


async def test_reauthorize_submit_maps_safe_failures(client, fake_app):
    fake_app.fail_with = AuthorizationError("not yours")
    resp = await client.post(
        "/miniapp/api/builder/reauthorize-submit",
        json={"pending_id": "reauth-test-1", "signature": "0x" + "ab" * 65},
        headers=_auth(),
    )
    assert resp.status == 403

    fake_app.fail_with = ServiceUnavailableError("engine down")
    resp = await client.post(
        "/miniapp/api/builder/reauthorize-submit",
        json={"pending_id": "reauth-test-1", "signature": "0x" + "ab" * 65},
        headers=_auth(),
    )
    assert resp.status == 503
    assert "engine down" not in json.dumps(await resp.json())
