"""Transport tests for the P2.4 Risk on Stop Mini App endpoints."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import (
    RISK_BASE,
    RISK_MAX,
    RISK_MIN,
    RISK_PRESETS,
    RISK_STEP,
    RiskDTO,
    RiskUpdateInput,
    ValidationError,
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


def _risk_dto(risk_percent: str = "0.01") -> RiskDTO:
    return RiskDTO(
        risk_percent=Decimal(risk_percent),
        min=RISK_MIN,
        max=RISK_MAX,
        step=RISK_STEP,
        presets=RISK_PRESETS,
        risk_base=RISK_BASE,
    )


@dataclass
class FakeRiskApplication:
    stored: RiskDTO = field(default_factory=_risk_dto)
    fail_with: Exception | None = None
    update_calls: list = field(default_factory=list)

    async def risk(self, user_id: int) -> RiskDTO:
        del user_id
        if self.fail_with:
            raise self.fail_with
        return self.stored

    async def update_risk(self, user_id: int, values: RiskUpdateInput) -> RiskDTO:
        del user_id
        self.update_calls.append(values)
        if self.fail_with:
            raise self.fail_with
        if not isinstance(values, RiskUpdateInput) or values.risk_percent is None:
            raise ValidationError("risk values are invalid")
        self.stored = _risk_dto(str(values.risk_percent))
        return self.stored


def _settings() -> Settings:
    return Settings(encryption_key="00" * 32, telegram_bot_token=BOT_TOKEN, miniapp_enabled=True)


@pytest.fixture
def fake_app() -> FakeRiskApplication:
    return FakeRiskApplication()


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


async def test_risk_read_requires_auth(client):
    resp = await client.post("/miniapp/api/risk", json={})
    assert resp.status == 401
    resp = await client.post("/miniapp/api/risk/percent", json={"risk_percent": "0.02"})
    assert resp.status == 401


async def test_risk_read_returns_backend_owned_contract(client):
    resp = await client.post("/miniapp/api/risk", json={}, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "risk_percent": "0.01",
        "min": "0.005",
        "max": "0.05",
        "step": "0.005",
        "presets": ["0.01", "0.02", "0.03", "0.05"],
        "risk_base": "available_balance",
    }
    for forbidden in ("max_leverage", "max_entry_deviation_pct", "account_id", "enabled"):
        assert forbidden not in body


async def test_risk_update_round_trip_returns_authoritative_dto(client, fake_app):
    resp = await client.post(
        "/miniapp/api/risk/percent", json={"risk_percent": "0.02"}, headers=_auth()
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["risk_percent"] == "0.02"
    assert body["min"] == "0.005"
    assert body["presets"] == ["0.01", "0.02", "0.03", "0.05"]
    assert len(fake_app.update_calls) == 1
    assert fake_app.update_calls[0].risk_percent == Decimal("0.02")
    for forbidden in ("max_leverage", "max_entry_deviation_pct", "account_id"):
        assert forbidden not in body


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"risk_percent": "0.02", "max_leverage": 10},
        {"risk_percent": "0.02", "max_entry_deviation_pct": "0.01"},
        {"risk_percent": "0.02", "account_id": "tg-777"},
        {"risk_percent": "0.02", "extra": 1},
        {"max_leverage": 10},
        {"risk_percent": True},
        {"risk_percent": False},
        {"risk_percent": None},
        {"risk_percent": 0.02},
        {"risk_percent": 2},
        {"risk_percent": ["0.02"]},
        {"risk_percent": {"v": "0.02"}},
        {"risk_percent": ""},
        {"risk_percent": "   "},
        {"risk_percent": "abc"},
        {"risk_percent": "NaN"},
        {"risk_percent": "Infinity"},
        {"risk_percent": "-Infinity"},
    ],
)
async def test_risk_update_rejects_forged_and_malformed_bodies(client, fake_app, payload):
    resp = await client.post("/miniapp/api/risk/percent", json=payload, headers=_auth())
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_input"
    assert fake_app.update_calls == []


async def test_risk_update_rejects_non_object_body(client, fake_app):
    resp = await client.post("/miniapp/api/risk/percent", json=[1, 2], headers=_auth())
    assert resp.status == 400
    assert fake_app.update_calls == []


async def test_risk_update_maps_application_validation_error_to_safe_400(client, fake_app):
    fake_app.fail_with = ValidationError("boom-secret-must-not-leak")
    resp = await client.post(
        "/miniapp/api/risk/percent", json={"risk_percent": "0.02"}, headers=_auth()
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_input"
    assert "boom-secret-must-not-leak" not in str(body)
