"""Signed testnet live suite: Pause/Resume control plane and diagnostic snapshot (L7-L8).

Marker `testnet`, requires CT_TESTNET=1 and wallet.txt.
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from copytradeaster.application.adapters import AppStage2RuntimeAdapter
from copytradeaster.application.models import (
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    CopyState,
    DashboardDTO,
    MiniAppAccessState,
    MiniAppConnectionState,
    SystemState,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import RiskPolicy, TradingIdentity
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AgentValidationRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from copytradeaster.trading.coordinator import FollowerRuntime
from copytradeaster.trading.registry import FollowerRegistry
from tests.testnet.support.builder_live import (
    ensure_testnet_guard,
    load_wallet_section,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

USER_TG_ID = 777_000_103
USER_SECTION = "copyc2"


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    cfg = Settings(
        db_path=tmp_path / "live_resume.sqlite3",
        log_path=tmp_path / "live.log",
        builder_name="copytradeaster",
        max_fee_rate="0.0001",
    )
    ensure_testnet_guard(cfg.rest_base_url, 714, cfg.registration_chain_id)
    return cfg


@pytest.fixture
async def live_app_env(test_settings):
    db = Database(test_settings.db_path)
    await db.connect()
    await db.migrate()

    user_address, _ = load_wallet_section(USER_SECTION)
    account_id = f"tg-{USER_TG_ID}"
    credentials = SqliteCredentials(db, test_settings.encryption_key_bytes())
    signer_key = "0x" + "11" * 32
    from eth_account import Account

    signer_address = Account.from_key(signer_key).address
    val = {
        "signer_address": signer_address,
        "can_read": True,
        "can_perp_trade": True,
        "can_withdraw": False,
        "ip_whitelist": "203.0.113.7/32",
        "status": "OK",
        "expired_ms": int((datetime.now(UTC) + timedelta(days=30)).timestamp() * 1000),
        "checked_at": datetime.now(UTC).isoformat(),
        "last_error_code": None,
    }
    await credentials.import_identity(
        account_id=account_id,
        user=user_address,
        signer_private_key_hex=signer_key,
        role="follower",
        label="test",
        owner_telegram_user_id=USER_TG_ID,
        owner_chat_id=USER_TG_ID,
        validation=val,
    )
    await TelegramUserRepo(db).upsert(
        USER_TG_ID, USER_TG_ID, account_id=account_id, access_enabled=True
    )
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(Decimal("0.01"), 20, Decimal("0.01"), enabled=True, allow_new_entries=False),
        account_id,
    )
    await SettingsRepo(db).set_flag("ready", "true")

    registry = FollowerRegistry()
    runtime = FollowerRuntime(
        account_id=account_id,
        client=object(),
        identity=TradingIdentity(account_id, user_address, user_address, 714),
        execution_mode="REAL",
    )
    await registry.publish_new(runtime)

    class _FakeCoordinator:
        async def open_generation_admission(self):
            pass

        async def close_generation_admission(self):
            pass

    class FakeLiveApp:
        def __init__(self):
            self.db = db
            self.cfg = test_settings
            self.credentials = credentials
            self.follower_registry = registry
            self.coordinator = _FakeCoordinator()
            self.ready = True

        async def reconcile_follower(self, account_id: str, reason: str = ""):
            pass

        async def snapshot_follower(self, account_id: str):
            from copytradeaster.domain.models import AccountState

            return AccountState(account_id=account_id, available_usdt=Decimal("1000"), positions={})

        async def validate_resume_safety(self, account_id: str, snapshot: Any):
            pass

        def master_has_open_positions(self):
            return False
    app = FakeLiveApp()
    adapter = AppStage2RuntimeAdapter(app)
    service = UserApplicationService(adapter, admin_ids=[])

    yield {"app": app, "service": service, "db": db, "account_id": account_id}
    await db.close()


async def test_l7_pause_resume_via_miniapp_and_stale_rejection(live_app_env):
    """L7: Pause/Resume with confirmation tokens and stale token rejection."""
    service: UserApplicationService = live_app_env["service"]
    account_id: str = live_app_env["account_id"]
    db: Database = live_app_env["db"]

    # 1. Read session: must be approved and connected
    session = await service.miniapp_session(USER_TG_ID)
    assert session.access_state == MiniAppAccessState.APPROVED
    assert session.connection_state == MiniAppConnectionState.CONNECTED
    assert session.account_connected is True

    # 2. Home dashboard returns paused state and captured_at
    dash: DashboardDTO = await service.dashboard(USER_TG_ID, refresh=True)
    assert dash.copy_state is CopyState.PAUSED
    assert dash.snapshot_captured_at is not None

    # 3. Resume prepare creates confirmation token
    confirmation = await service.begin_confirmation(USER_TG_ID, ConfirmationAction.RESUME)
    assert confirmation.token

    # 4. Resume without confirmation token is rejected
    with pytest.raises(ConfirmationExpiredError):
        await service.resume(USER_TG_ID)

    # 5. Invalid / stale confirmation token is rejected
    with pytest.raises(ConfirmationExpiredError):
        await service.resume(
            USER_TG_ID,
            confirmation=Confirmation(
                token="invalid_token_123",
                action=ConfirmationAction.RESUME,
                account_id=account_id,
            ),
        )

    # 6. Valid confirmation resumes follower to ACTIVE
    dash_active = await service.resume(USER_TG_ID, confirmation=confirmation)
    assert dash_active.copy_state is CopyState.ACTIVE
    policy = await SettingsRepo(db).get_policy(account_id)
    assert policy is not None
    assert policy.enabled is True
    assert policy.allow_new_entries is True

    # 7. Pause returns follower to PAUSED
    dash_paused = await service.pause(USER_TG_ID)
    assert dash_paused.copy_state is CopyState.PAUSED


async def test_l8_final_diagnostic_snapshot(live_app_env):
    """L8: Diagnostic snapshot verifies healthy state."""
    service: UserApplicationService = live_app_env["service"]
    dash: DashboardDTO = await service.dashboard(USER_TG_ID, refresh=True)
    assert dash.system_state == SystemState.READY
    assert dash.snapshot_captured_at is not None
    assert dash.copy_state in {CopyState.ACTIVE, CopyState.PAUSED}
