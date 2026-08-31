"""Transport tests for the Mini App aiohttp server (Stage 3 part 1)."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import (
    AuthorizationError,
    BuilderOnboardedDTO,
    BuilderPrepareDTO,
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    CopyState,
    DashboardDTO,
    MiniAppAccessState,
    MiniAppSessionDTO,
    OnboardingApplicationError,
    SystemState,
)
from copytradeaster.config import Settings
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.onboarding.errors import OnboardingErrorCode

BOT_TOKEN = "123456:TEST-TOKEN"


def _make_init_data(token: str, user_id: int, auth_date: int | None = None) -> str:
    """Build initData exactly the way Telegram signs it (for tests only)."""
    pairs = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAF" + "0" * 10,
        "user": f'{{"id":{user_id},"first_name":"T"}}',
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


def _dashboard_dto() -> DashboardDTO:
    return DashboardDTO(
        copy_state=CopyState.PAUSED,
        system_state=SystemState.READY,
        account_id="tg-777",
        account_masked="0xABC…DEF",
        signer_masked="0x111…222",
        risk_percent=Decimal("1.5"),
        max_leverage=75,
        open_positions=2,
        updated_at=datetime(2026, 8, 31, 12, 41, tzinfo=UTC),
        is_admin=False,
    )


@dataclass
class FakeApplication:
    """Records calls and returns canned DTOs; mimics ApplicationProtocol."""

    user_id_seen: int | None = None
    fail_with: Exception | None = None
    submit_fail_with: Exception | None = None
    resume_fail_with: Exception | None = None
    session_dto: MiniAppSessionDTO = field(
        default_factory=lambda: MiniAppSessionDTO(
            user_id=777,
            access_state=MiniAppAccessState.APPROVED,
            environment="testnet",
            account_connected=False,
        )
    )
    dashboard_dto: DashboardDTO = field(default_factory=_dashboard_dto)
    prepare_calls: list = field(default_factory=list)
    submit_calls: list = field(default_factory=list)
    dashboard_calls: list = field(default_factory=list)
    pause_calls: list = field(default_factory=list)
    resume_calls: list = field(default_factory=list)
    begin_calls: list = field(default_factory=list)
    confirm_calls: list = field(default_factory=list)

    async def miniapp_session(self, user_id: int) -> MiniAppSessionDTO:
        self.user_id_seen = user_id
        if self.fail_with:
            raise self.fail_with
        return self.session_dto

    async def dashboard(self, user_id: int) -> DashboardDTO:
        self.user_id_seen = user_id
        self.dashboard_calls.append(user_id)
        if self.fail_with:
            raise self.fail_with
        return self.dashboard_dto

    async def builder_prepare(self, user_id: int, user_address: str) -> BuilderPrepareDTO:
        self.prepare_calls.append((user_id, user_address))
        if self.fail_with:
            raise self.fail_with
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

    async def pause(self, user_id: int) -> DashboardDTO:
        self.pause_calls.append(user_id)
        return self.dashboard_dto

    async def resume(self, user_id: int, confirmation: Confirmation | None = None) -> DashboardDTO:
        self.resume_calls.append((user_id, confirmation))
        if self.resume_fail_with and confirmation is None:
            raise self.resume_fail_with
        return self.dashboard_dto

    async def begin_confirmation(
        self, user_id: int, action: ConfirmationAction, account_id: str | None = None
    ) -> Confirmation:
        if action in {
            ConfirmationAction.EMERGENCY,
            ConfirmationAction.GLOBAL_PAUSE,
            ConfirmationAction.GLOBAL_RESUME,
            ConfirmationAction.GLOBAL_EMERGENCY,
        }:
            # Mimic the real boundary: follower/global actions are rejected.
            raise AuthorizationError("action is not allowed for this role")
        self.begin_calls.append((user_id, action))
        return Confirmation(
            token="tok-1",
            action=action,
            account_id=account_id,
            expires_at=datetime(2026, 8, 31, 13, 0, tzinfo=UTC),
        )

    async def confirm_action(self, user_id: int, confirmation: Confirmation) -> DashboardDTO:
        self.confirm_calls.append((user_id, confirmation))
        return self.dashboard_dto


def _settings(**overrides) -> Settings:
    return Settings(
        encryption_key="00" * 32,
        telegram_bot_token=BOT_TOKEN,
        miniapp_enabled=True,
        **overrides,
    )


@pytest.fixture
def settings() -> Settings:
    return _settings()


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


# --- session & initData integrity -------------------------------------------


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


async def test_stale_auth_date_is_rejected(client, fake_app):
    stale = int(time.time()) - 2 * 86_400
    init_data = _make_init_data(BOT_TOKEN, 777, auth_date=stale)
    resp = await client.post("/miniapp/api/session", headers=_auth_header(init_data))
    assert resp.status == 401
    assert fake_app.user_id_seen is None


async def test_future_auth_date_is_rejected(client, fake_app):
    future = int(time.time()) + 3600
    init_data = _make_init_data(BOT_TOKEN, 777, auth_date=future)
    resp = await client.post("/miniapp/api/session", headers=_auth_header(init_data))
    assert resp.status == 401


# --- dev unsigned session (fail-closed) --------------------------------------


async def test_dev_header_ignored_when_flag_off(client, fake_app):
    resp = await client.post("/miniapp/api/session", headers={"X-Dev-User-Id": "777"})
    assert resp.status == 401
    assert fake_app.user_id_seen is None


async def test_dev_session_accepts_declared_user_on_testnet(fake_app):
    settings = _settings(miniapp_dev_unsigned=True, miniapp_dev_user_id=777)
    app = create_miniapp_app(settings, fake_app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/miniapp/api/session", headers={"X-Dev-User-Id": "777"})
        assert resp.status == 200
        assert fake_app.user_id_seen == 777
    finally:
        await client.close()


async def test_dev_session_rejects_other_user_and_mainnet_base(fake_app):
    settings = _settings(miniapp_dev_unsigned=True, miniapp_dev_user_id=777)
    app = create_miniapp_app(settings, fake_app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/miniapp/api/session", headers={"X-Dev-User-Id": "666"})
        assert resp.status == 401
    finally:
        await client.close()

    mainnet = _settings(
        rest_base_url="https://fapi.asterdex.com",
        ws_user_base="wss://fstream.asterdex.com/ws",
        allow_mainnet=True,
        testnet_ip_whitelist_override=False,
        miniapp_dev_unsigned=True,
        miniapp_dev_user_id=777,
    )
    app = create_miniapp_app(mainnet, fake_app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/miniapp/api/session", headers={"X-Dev-User-Id": "777"})
        assert resp.status == 401
        assert fake_app.user_id_seen is None
    finally:
        await client.close()


# --- builder flow ------------------------------------------------------------


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


async def test_builder_prepare_maps_typed_onboarding_code(client, fake_app):
    fake_app.fail_with = OnboardingApplicationError(OnboardingErrorCode.OWNER_CONFLICT)
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/builder/prepare",
        json={"user_address": "0xABC"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "owner_conflict"
    assert "уже есть подключённый" in body["message"] or body["message"]


async def test_builder_submit_unclassified_error_is_503(client, fake_app):
    fake_app.submit_fail_with = RuntimeError("boom")
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/builder/submit",
        json={"pending_id": "p", "signature": "0xsig"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 503
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


# --- home surface: dashboard / pause / resume / confirmation ------------------


async def test_dashboard_serializes_decimal_datetime_and_enum(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post("/miniapp/api/dashboard", headers=_auth_header(init_data))
    assert resp.status == 200
    body = await resp.json()
    assert fake_app.dashboard_calls == [777]
    assert body["risk_percent"] == "1.5"
    assert body["copy_state"] == "paused"
    assert body["system_state"] == "ready"
    assert body["updated_at"].startswith("2026-08-31T12:41")


async def test_dashboard_requires_auth(client, fake_app):
    resp = await client.post("/miniapp/api/dashboard")
    assert resp.status == 401
    assert fake_app.dashboard_calls == []


async def test_pause_returns_dashboard(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post("/miniapp/api/pause", headers=_auth_header(init_data))
    assert resp.status == 200
    body = await resp.json()
    assert fake_app.pause_calls == [777]
    assert body["copy_state"] == "paused"


async def test_resume_without_confirmation_maps_confirmation_expired(client, fake_app):
    fake_app.resume_fail_with = ConfirmationExpiredError("confirmation required")
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post("/miniapp/api/resume", json={}, headers=_auth_header(init_data))
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "confirmation_expired"


async def test_resume_with_confirmation_returns_dashboard(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    confirmation = {
        "token": "tok-9",
        "action": "resume",
        "account_id": "tg-777",
        "expires_at": "2026-08-31T13:00:00+00:00",
    }
    resp = await client.post(
        "/miniapp/api/resume",
        json={"confirmation": confirmation},
        headers=_auth_header(init_data),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["account_id"] == "tg-777"
    assert len(fake_app.resume_calls) == 1
    assert fake_app.resume_calls[0][1] is not None
    assert fake_app.resume_calls[0][1].token == "tok-9"


async def test_resume_with_malformed_confirmation_is_400(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/resume",
        json={"confirmation": {"token": ""}},
        headers=_auth_header(init_data),
    )
    assert resp.status == 400
    assert fake_app.resume_calls == []


async def test_confirmation_begin_and_confirm_roundtrip(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/confirmation/begin",
        json={"action": "resume"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 200
    body = await resp.json()
    assert fake_app.begin_calls == [(777, ConfirmationAction.RESUME)]
    assert body["token"] == "tok-1"
    assert body["action"] == "resume"
    assert body["expires_at"].startswith("2026-08-31T13:00")

    resp = await client.post(
        "/miniapp/api/confirmation/confirm",
        json={"confirmation": body},
        headers=_auth_header(init_data),
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["copy_state"] == "paused"
    assert len(fake_app.confirm_calls) == 1


async def test_confirmation_begin_rejects_forbidden_action(client, fake_app):
    init_data = _make_init_data(BOT_TOKEN, 777)
    resp = await client.post(
        "/miniapp/api/confirmation/begin",
        json={"action": "global_emergency"},
        headers=_auth_header(init_data),
    )
    assert resp.status == 403
    assert fake_app.begin_calls == []


# --- static bundle serving ----------------------------------------------------


async def test_static_index_is_served_before_the_static_mount(tmp_path):
    (tmp_path / "index.html").write_text("<html>aster-copy</html>", encoding="utf-8")
    settings = _settings(miniapp_static_dir=tmp_path)
    app = create_miniapp_app(settings, fake_app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        for path in ("/miniapp/", "/miniapp"):
            resp = await client.get(path)
            assert resp.status == 200
            assert "aster-copy" in await resp.text()
        # Unknown paths stay inside the static mount (SPA assets resolve there).
        missing = await client.get("/miniapp/assets/none.js")
        assert missing.status == 404
    finally:
        await client.close()
