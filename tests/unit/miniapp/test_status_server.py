"""Transport tests for the P2.5 System Status Mini App endpoint."""

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
    ApplicationError,
    AsterState,
    AuthorizationError,
    CopyState,
    PermissionsState,
    PermissionSummary,
    ServiceUnavailableError,
    StatusDTO,
    StatusReason,
    StatusState,
    SystemState,
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


def _status_dto() -> StatusDTO:
    return StatusDTO(
        state=StatusState.READY,
        system_state=SystemState.READY,
        copy_state=CopyState.PAUSED,
        aster_state=AsterState.OK,
        permissions_state=PermissionsState.VALID,
        account_masked="0xABC…DEF",
        signer_masked="0x111…222",
        positions_count=0,
        permissions=PermissionSummary(
            can_read=True,
            can_perp_trade=True,
            can_withdraw=False,
            whitelist_ok=True,
            checked_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        ),
        snapshot_captured_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        last_check_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        age_seconds=5,
        reason=StatusReason.NONE,
        remediation=None,
    )


@dataclass
class FakeStatusApplication:
    dto: StatusDTO = field(default_factory=_status_dto)
    fail_with: Exception | None = None
    status_calls: list = field(default_factory=list)

    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO:
        self.status_calls.append((user_id, refresh))
        if self.fail_with is not None:
            raise self.fail_with
        return self.dto


def _settings() -> Settings:
    return Settings(encryption_key="00" * 32, telegram_bot_token=BOT_TOKEN, miniapp_enabled=True)


@pytest.fixture
def fake_app() -> FakeStatusApplication:
    return FakeStatusApplication()


@pytest.fixture
async def client(fake_app):
    app = create_miniapp_app(_settings(), fake_app)  # type: ignore[arg-type]
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


def _auth() -> dict:
    return {"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, 777)}


async def test_status_requires_auth(client):
    resp = await client.post("/miniapp/api/status", json={})
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_status_empty_body_is_refresh_false(client, fake_app):
    resp = await client.post("/miniapp/api/status", json={}, headers=_auth())
    assert resp.status == 200
    assert fake_app.status_calls == [(777, False)]


async def test_status_exact_booleans_route_to_service(client, fake_app):
    resp = await client.post("/miniapp/api/status", json={"refresh": True}, headers=_auth())
    assert resp.status == 200
    resp = await client.post("/miniapp/api/status", json={"refresh": False}, headers=_auth())
    assert resp.status == 200
    assert fake_app.status_calls == [(777, True), (777, False)]


@pytest.mark.parametrize(
    "payload",
    [
        {"refresh": "true"},
        {"refresh": "false"},
        {"refresh": 1},
        {"refresh": 0},
        {"refresh": None},
        {"refresh": [True]},
        {"refresh": {"value": True}},
        {"refresh": True, "account_id": "tg-777"},
        {"refresh": True, "user_id": 777},
        {"account_id": "tg-777"},
        {"extra": 1},
    ],
)
async def test_status_rejects_non_boolean_and_forged_bodies(client, fake_app, payload):
    resp = await client.post("/miniapp/api/status", json=payload, headers=_auth())
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_input"
    assert fake_app.status_calls == []


async def test_status_rejects_non_object_body(client, fake_app):
    resp = await client.post("/miniapp/api/status", json=[1, 2], headers=_auth())
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_input"
    assert fake_app.status_calls == []


async def test_status_rejects_malformed_json(client, fake_app):
    resp = await client.post(
        "/miniapp/api/status",
        data="{not-json",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_input"
    assert fake_app.status_calls == []


async def test_status_returns_exact_safe_dto(client):
    resp = await client.post("/miniapp/api/status", json={"refresh": True}, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "state": "ready",
        "system_state": "ready",
        "copy_state": "paused",
        "aster_state": "ok",
        "permissions_state": "valid",
        "account_masked": "0xABC…DEF",
        "signer_masked": "0x111…222",
        "positions_count": 0,
        "permissions": {
            "can_read": True,
            "can_perp_trade": True,
            "can_withdraw": False,
            "whitelist_ok": True,
            "checked_at": "2026-09-03T12:00:00+00:00",
            "warning": None,
        },
        "snapshot_captured_at": "2026-09-03T12:00:00+00:00",
        "last_check_at": "2026-09-03T12:00:00+00:00",
        "age_seconds": 5,
        "reason": "none",
        "remediation": None,
    }
    for forbidden in (
        "message",
        "aster_connected",
        "last_refresh_at",
        "account_id",
        "command_key",
        "client_order_id",
        "private_key",
        "signature",
        "nonce",
        "payload",
        "traceback",
    ):
        assert forbidden not in body


async def test_status_maps_application_error_to_safe_code(client, fake_app):
    fake_app.fail_with = ApplicationError("boom-secret-must-not-leak")
    resp = await client.post("/miniapp/api/status", json={}, headers=_auth())
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "application_error"
    assert "boom-secret-must-not-leak" not in json.dumps(body)


async def test_status_maps_authorization_and_unavailable(client, fake_app):
    fake_app.fail_with = AuthorizationError("not yours")
    resp = await client.post("/miniapp/api/status", json={}, headers=_auth())
    assert resp.status == 403

    fake_app.fail_with = ServiceUnavailableError("engine down")
    resp = await client.post("/miniapp/api/status", json={}, headers=_auth())
    assert resp.status == 503
    assert "engine down" not in json.dumps(await resp.json())
