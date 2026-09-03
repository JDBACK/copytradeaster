"""Signed Live Acceptance Gate for Stage 3 Part 2 (P2.5 — System Status).

Read-only gate through the real production composition (no FakeAster, no
mock transport, no testnet orders):

1. Real connected follower Testnet account (``copyc1`` from wallet.txt).
2. ``status(refresh=True)`` performs a real signed Aster snapshot read and a
   real signed Agent permission read.
3. The fresh/ready verdict, evidence timestamps, and absence of secrets/raw
   responses are verified on real data, including the HTTP serialization of
   ``POST /miniapp/api/status``.

The gate never opens, closes, or cancels orders and never touches the
production database: it boots the production ``App`` (credentials, registry,
leverage catalog) against a scratch SQLite copy for the settings/user reads.
Mainnet is never touched (testnet base URL is enforced).
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.app import App
from copytradeaster.application.adapters import AppStage2RuntimeAdapter
from copytradeaster.application.models import StatusState
from copytradeaster.application.service import UserApplicationService
from copytradeaster.config import Settings
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.storage.repositories import (
    AccountRepo,
    SettingsRepo,
    TelegramUserRepo,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

TEST_USER_ID = 900000001
FOLLOWER_ACCOUNT_ID = "copyc1"
PROD_DB = Path("data/copytrade.sqlite3")


def _backup_sqlite(src: Path, dst: Path) -> None:
    """Point-in-time copy via the SQLite backup API (production untouched)."""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


@pytest.fixture
async def live_status_stack(tmp_path):
    if not PROD_DB.exists():
        pytest.skip("need data/copytrade.sqlite3 with imported testnet credentials")
    db_path = tmp_path / "live-status.sqlite3"
    _backup_sqlite(PROD_DB, db_path)
    settings = Settings(
        db_path=db_path,
        miniapp_dev_unsigned=True,
        miniapp_dev_user_id=TEST_USER_ID,
    )
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")

    app = App(settings)
    await app.db.connect()
    await app.db.migrate()
    await app.credentials.load()
    if FOLLOWER_ACCOUNT_ID not in app.credentials.account_ids():
        await app.db.close()
        pytest.skip(f"need {FOLLOWER_ACCOUNT_ID} imported in wallet.txt")

    identity = app.credentials.identity(FOLLOWER_ACCOUNT_ID)
    await app.register_follower(FOLLOWER_ACCOUNT_ID)
    await app.leverage.refresh_catalog()

    settings_repo = SettingsRepo(app.db)
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.set_flag("ready", "true")
    await settings_repo.set_flag("heartbeat_at", str(int(time.time() * 1000)))
    await settings_repo.upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
        FOLLOWER_ACCOUNT_ID,
    )
    await AccountRepo(app.db).upsert(
        FOLLOWER_ACCOUNT_ID, "follower", FOLLOWER_ACCOUNT_ID, identity.user
    )
    await TelegramUserRepo(app.db).upsert(
        telegram_user_id=TEST_USER_ID,
        chat_id=TEST_USER_ID,
        account_id=FOLLOWER_ACCOUNT_ID,
        role="ADMIN",
        access_enabled=True,
    )

    runtime_adapter = AppStage2RuntimeAdapter(app)
    app_service = UserApplicationService(
        settings=settings_repo,
        runtime=runtime_adapter,
        admin_ids=(TEST_USER_ID,),
    )
    aio_app = create_miniapp_app(settings, app_service)
    test_client = TestClient(TestServer(aio_app))
    await test_client.start_server()

    yield {
        "app": app,
        "service": app_service,
        "http_client": test_client,
        "user_address": identity.user,
        "signer_address": identity.signer,
    }

    await test_client.close()
    await app.http.aclose()
    await app.db.close()


@pytest.mark.asyncio
async def test_live_production_status_acceptance_gate(live_status_stack):
    service: UserApplicationService = live_status_stack["service"]
    http_client: TestClient = live_status_stack["http_client"]
    user_address: str = live_status_stack["user_address"]
    signer_address: str = live_status_stack["signer_address"]
    auth_headers = {"X-Dev-User-Id": str(TEST_USER_ID), "Content-Type": "application/json"}

    # Phase 0: real signed reads through the production composition.
    dto = await service.status(TEST_USER_ID, refresh=True)

    assert dto.state is StatusState.READY
    assert dto.system_state.value == "ready"
    assert dto.copy_state.value in {"active", "paused"}
    assert dto.aster_state.value == "ok"
    assert dto.permissions_state.value == "valid"
    assert dto.reason.value == "none"
    assert dto.remediation is None
    assert isinstance(dto.positions_count, int)

    # Evidence timestamps come from the real exchange reads, not the call time.
    assert dto.snapshot_captured_at is not None
    assert dto.last_check_at is not None
    age = (datetime.now(UTC) - dto.last_check_at).total_seconds()
    assert 0 <= age < 180
    assert dto.last_check_at <= dto.snapshot_captured_at

    # Only masked identities travel outward — never raw addresses.
    assert dto.account_masked and user_address not in dto.account_masked
    assert dto.signer_masked and signer_address not in dto.signer_masked

    # Phase 1: the HTTP boundary serializes the exact safe contract.
    resp = await http_client.post(
        "/miniapp/api/status", headers=auth_headers, json={"refresh": True}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["state"] == "ready"
    assert body["reason"] == "none"
    assert body["remediation"] is None
    assert body["positions_count"] == dto.positions_count
    assert body["account_masked"] == dto.account_masked
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
    fingerprint = str(body)
    assert user_address not in fingerprint
    assert signer_address not in fingerprint

    # Phase 2: strict refresh boundary still holds against the live server.
    bad = await http_client.post(
        "/miniapp/api/status", headers=auth_headers, json={"refresh": "true"}
    )
    assert bad.status == 400
    assert (await bad.json())["error"] == "invalid_input"
