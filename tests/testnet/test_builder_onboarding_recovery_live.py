"""Signed testnet live suite: Builder onboarding lifecycle & recovery (L1-L4).

Marker `testnet`, requires CT_TESTNET=1, wallet.txt (copyc2 and builder).
All requests execute against the live Aster testnet (fapi.asterdex-testnet.com).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.application.adapters import SqliteOnboardingStore
from copytradeaster.application.models import (
    MiniAppAccessState,
    MiniAppConnectionState,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.onboarding.builder import BuilderOnboardingService
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderPrepareRequest,
    BuilderSubmitRequest,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import NonceStore, SettingsRepo, TelegramUserRepo
from copytradeaster.trading.registry import FollowerRegistry
from tests.testnet.support.builder_live import (
    ensure_testnet_guard,
    get_live_account_state,
    load_wallet_section,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

USER_TG_ID = 777_000_102
USER_SECTION = "copyc2"
BUILDER_SECTION = "builder"


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    cfg = Settings(
        db_path=tmp_path / "live_recovery.sqlite3",
        log_path=tmp_path / "live.log",
        builder_name="copytradeaster",
        max_fee_rate="0.0001",
    )
    ensure_testnet_guard(cfg.rest_base_url, 714, cfg.registration_chain_id)
    return cfg


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        yield client


async def test_l1_signed_read_account_state(test_settings, http_client):
    """L1: Signed read of current account state (Agent, mode, balance, positions)."""
    user_address, user_key = load_wallet_section(USER_SECTION)
    limiter = RateLimits(2400, 1200, 300)
    db = Database(test_settings.db_path)
    await db.connect()
    await db.migrate()
    nonce_store = NonceStore(db)

    identity = TradingIdentity(
        account_id="tg-live-read",
        user=user_address,
        signer=user_address,
        chain_id=714,
    )
    state = await get_live_account_state(
        http_client,
        test_settings.rest_base_url,
        identity,
        user_key,
        nonce_store,
        limiter,
    )
    await db.close()

    assert "balances" in state
    assert "positions" in state
    assert "open_orders" in state
    assert isinstance(state["dual_side_position"], bool)


async def test_l2_fresh_builder_onboarding_happy_path(test_settings, http_client):
    """L2: Fresh Builder onboarding: EIP-712 signature -> approveAgent -> paused commit."""
    user_address, user_key = load_wallet_section(USER_SECTION)
    builder_address, _ = load_wallet_section(BUILDER_SECTION)

    db = Database(test_settings.db_path)
    await db.connect()
    await db.migrate()
    await TelegramUserRepo(db).upsert(USER_TG_ID, USER_TG_ID)

    credentials = SqliteCredentials(db, test_settings.encryption_key_bytes())
    store = SqliteOnboardingStore(db, credentials, permission_override=True)
    limiter = RateLimits(2400, 1200, 300)

    config = BuilderOnboardingConfig(
        base_url=test_settings.rest_base_url,
        trading_chain_id=714,
        registration_chain_id=56,
        builder_address=builder_address,
        builder_name=test_settings.builder_name,
        max_fee_rate=test_settings.max_fee_rate,
        agent_expiry_days=30,
        approval_ttl_s=600,
        require_agent_ip_whitelist=False,
    )

    def client_factory(identities, keys):
        return AsterClient(
            http=http_client,
            base_url=test_settings.rest_base_url,
            limiter=limiter,
            identities=identities,
            keys=keys,
            nonces=NonceStore(db),
        )

    activated_accounts: list[str] = []
    service = BuilderOnboardingService(
        http_client,
        limiter,
        client_factory,
        store,
        config=config,
        activation=activated_accounts.append,
    )

    prepared = await service.prepare(BuilderPrepareRequest(USER_TG_ID, user_address))
    assert prepared.pending_id

    typed = encode_typed_data(
        domain_data=prepared.eip712["domain"],
        message_types={"ApproveAgent": prepared.eip712["types"]["ApproveAgent"]},
        message_data=prepared.eip712["message"],
    )
    signature = Account.sign_message(typed, user_key).signature.hex()

    result = await service.submit(BuilderSubmitRequest(USER_TG_ID, prepared.pending_id, signature))
    assert result.paused is True
    assert result.account_id == f"tg-{USER_TG_ID}"
    assert activated_accounts == [f"tg-{USER_TG_ID}"]

    # Audit assertions: attempt row removed after commit, credentials encrypted
    assert await store.get_builder_attempt(USER_TG_ID) is None
    cred_row = await db.fetchone(
        "SELECT encrypted FROM credentials WHERE account_id=?", (f"tg-{USER_TG_ID}",)
    )
    assert cred_row is not None
    assert user_key.encode() not in bytes(cred_row["encrypted"])
    assert signature.encode() not in bytes(cred_row["encrypted"])

    await db.close()


async def test_l3_recovery_after_real_approve_agent_and_local_failure(test_settings, http_client):
    """L3: Recovery after remote approval: local failure before commit recovers with same signer."""
    user_address, user_key = load_wallet_section(USER_SECTION)
    builder_address, _ = load_wallet_section(BUILDER_SECTION)

    db = Database(test_settings.db_path)
    await db.connect()
    await db.migrate()
    tg_id = USER_TG_ID + 10
    await TelegramUserRepo(db).upsert(tg_id, tg_id)

    credentials = SqliteCredentials(db, test_settings.encryption_key_bytes())
    store = SqliteOnboardingStore(db, credentials, permission_override=True)
    limiter = RateLimits(2400, 1200, 300)

    config = BuilderOnboardingConfig(
        base_url=test_settings.rest_base_url,
        trading_chain_id=714,
        registration_chain_id=56,
        builder_address=builder_address,
        builder_name=test_settings.builder_name,
        max_fee_rate=test_settings.max_fee_rate,
        agent_expiry_days=30,
        approval_ttl_s=600,
        require_agent_ip_whitelist=False,
    )

    def client_factory(identities, keys):
        return AsterClient(
            http=http_client,
            base_url=test_settings.rest_base_url,
            limiter=limiter,
            identities=identities,
            keys=keys,
            nonces=NonceStore(db),
        )

    service = BuilderOnboardingService(
        http_client,
        limiter,
        client_factory,
        store,
        config=config,
    )

    prepared = await service.prepare(BuilderPrepareRequest(tg_id, user_address))
    typed = encode_typed_data(
        domain_data=prepared.eip712["domain"],
        message_types={"ApproveAgent": prepared.eip712["types"]["ApproveAgent"]},
        message_data=prepared.eip712["message"],
    )
    signature = Account.sign_message(typed, user_key).signature.hex()

    # 1. Simulate in-flight state where remote approval was interrupted
    await store.update_builder_attempt_state(prepared.pending_id, "APPROVAL_SENT")

    # 2. Re-create service (simulating crash / restart)
    service_after_restart = BuilderOnboardingService(
        http_client,
        limiter,
        client_factory,
        store,
        config=config,
    )

    # 3. First submit after restart checks GET /agent -> not found -> reverts to PREPARED
    with pytest.raises(OnboardingError) as exc_info:
        await service_after_restart.submit(
            BuilderSubmitRequest(tg_id, prepared.pending_id, signature)
        )
    assert exc_info.value.code is OnboardingErrorCode.AGENT_NOT_CONFIRMED

    # 4. Check state reverted to PREPARED
    att = await store.get_builder_attempt(tg_id)
    assert att is not None
    assert att.state == "PREPARED"

    # 5. Retry submit posts approval, verifies agent, and completes paused commit
    result = await service_after_restart.submit(
        BuilderSubmitRequest(tg_id, prepared.pending_id, signature)
    )
    assert result.paused is True
    assert await store.get_builder_attempt(tg_id) is None

    await db.close()


async def test_l4_recovery_after_commit_and_runtime_activation_failure(test_settings, http_client):
    """L4: Committed account with pending runtime recovers via miniapp_recover_runtime."""
    user_address, user_key = load_wallet_section(USER_SECTION)
    builder_address, _ = load_wallet_section(BUILDER_SECTION)

    db = Database(test_settings.db_path)
    await db.connect()
    await db.migrate()
    tg_id = USER_TG_ID + 20
    await TelegramUserRepo(db).upsert(tg_id, tg_id)

    credentials = SqliteCredentials(db, test_settings.encryption_key_bytes())
    store = SqliteOnboardingStore(db, credentials, permission_override=True)
    limiter = RateLimits(2400, 1200, 300)

    config = BuilderOnboardingConfig(
        base_url=test_settings.rest_base_url,
        trading_chain_id=714,
        registration_chain_id=56,
        builder_address=builder_address,
        builder_name=test_settings.builder_name,
        max_fee_rate=test_settings.max_fee_rate,
        agent_expiry_days=30,
        approval_ttl_s=600,
        require_agent_ip_whitelist=False,
    )

    def client_factory(identities, keys):
        return AsterClient(
            http=http_client,
            base_url=test_settings.rest_base_url,
            limiter=limiter,
            identities=identities,
            keys=keys,
            nonces=NonceStore(db),
        )

    # 1. Onboard without runtime activation callback (simulating broken activation)
    service = BuilderOnboardingService(
        http_client,
        limiter,
        client_factory,
        store,
        config=config,
    )

    prepared = await service.prepare(BuilderPrepareRequest(tg_id, user_address))
    typed = encode_typed_data(
        domain_data=prepared.eip712["domain"],
        message_types={"ApproveAgent": prepared.eip712["types"]["ApproveAgent"]},
        message_data=prepared.eip712["message"],
    )
    signature = Account.sign_message(typed, user_key).signature.hex()

    onboarded = await service.submit(BuilderSubmitRequest(tg_id, prepared.pending_id, signature))
    assert onboarded.paused is True

    # 2. Setup UserApplicationService without pre-registered runtime
    from copytradeaster.application.adapters import (
        AppStage2RuntimeAdapter,
        BuilderOnboardingAdapter,
    )

    # Create dummy app wrapper for adapter
    class FakeApp:
        def __init__(self):
            self.db = db
            self.cfg = test_settings
            self.credentials = credentials
            self.ready = True
            self.http = http_client
            self.limiter = limiter
            self.nonces = NonceStore(db)
            self.settings = SettingsRepo(db)
            self.follower_registry = FollowerRegistry()

        async def register_follower(self, account_id: str):
            from copytradeaster.trading.coordinator import FollowerRuntime

            runtime = FollowerRuntime(
                account_id=account_id,
                client=object(),
                identity=None,
                execution_mode="REAL",
            )
            await self.follower_registry.publish_new(runtime)

    fake_app = FakeApp()
    runtime_adapter = AppStage2RuntimeAdapter(fake_app)
    builder_adapter = BuilderOnboardingAdapter(fake_app, store)

    app_service = UserApplicationService(
        runtime_adapter,
        admin_ids=[],
        builder_onboarding=builder_adapter,
    )

    # Session reports recovery_required
    sess = await app_service.miniapp_session(tg_id)
    assert sess.access_state == MiniAppAccessState.APPROVED
    assert sess.connection_state == MiniAppConnectionState.RECOVERY_REQUIRED
    assert sess.account_connected is False

    # Recover runtime
    recovered_sess = await app_service.miniapp_recover_runtime(tg_id)
    assert recovered_sess.connection_state == MiniAppConnectionState.CONNECTED
    assert recovered_sess.account_connected is True
    assert fake_app.follower_registry.get(f"tg-{tg_id}") is not None

    await db.close()
