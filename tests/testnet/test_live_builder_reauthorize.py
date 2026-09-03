"""Bounded live same-signer Builder reauthorization gate (Aster Testnet).

Covers the exchange-writing business function added by P2.6 corrective
review: after an authoritative EXPIRING verdict, the service pauses +
drains through the real runtime, the main wallet signs a real
same-signer reauthorization EIP-712 payload, ``/approveAgent`` passes,
and signed reads confirm the same signer with renewed expiry.

Two phases on one funded throwaway wallet (``wallet.txt`` section
``builder-gate``, fresh Telegram slot, scratch DB — production untouched):

1. short-lived approval (``agent_expiry_days=5`` < 7-day warning) →
   EXPIRING → service prepare → submit → proof renewed, still EXPIRING,
   same signer, still paused;
2. normal approval (30d) → service prepare → submit → VERIFIED,
   both trust rails true, same signer, still paused.

No orders are placed (P2.7 is out of scope).  If the exchange ever
rejects a same-signer re-approval, this gate fails loudly with live
evidence instead of falling back to signer rotation.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.app import App
from copytradeaster.application.adapters import AppStage2RuntimeAdapter, SqliteOnboardingStore
from copytradeaster.application.models import (
    AccountBuilderState,
    OnboardingApplicationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError
from copytradeaster.config import Settings
from copytradeaster.domain.models import RiskPolicy, TradingIdentity
from copytradeaster.onboarding.builder import BuilderOnboardingService
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderPrepareRequest,
    BuilderSubmitRequest,
    stable_account_id,
)
from copytradeaster.onboarding.providers import (
    EphemeralIdentityProvider,
    EphemeralKeyProvider,
)
from copytradeaster.storage.repositories import (
    NonceStore,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.testnet.support.builder_live import load_wallet_section

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

FRESH_TG_ID = 777000006
EXPIRY_SHORT_DAYS = 5
EXPIRY_NORMAL_DAYS = 30
EXPIRY_WARNING_MS = 7 * 24 * 60 * 60 * 1000


@pytest.fixture
async def live_reauth_stack(tmp_path: Path):
    db_path = tmp_path / "live-builder-reauth.sqlite3"
    settings = Settings(db_path=db_path)
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")

    app = App(settings)
    await app.db.connect()
    await app.db.migrate()
    await app.credentials.load()

    settings_repo = SettingsRepo(app.db)
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.set_flag("ready", "true")
    import time

    await settings_repo.set_flag("heartbeat_at", str(int(time.time() * 1000)))
    await app.leverage.refresh_catalog()

    await TelegramUserRepo(app.db).upsert(FRESH_TG_ID, FRESH_TG_ID)

    limiter = RateLimits(
        settings.weight_limit_per_min,
        settings.orders_limit_per_min,
        settings.orders_limit_per_10s,
    )

    def client_factory(identities, keys):
        return AsterClient(
            http=app.http,
            base_url=settings.rest_base_url,
            limiter=limiter,
            identities=identities,
            keys=keys,
            nonces=NonceStore(app.db),
            dry_run=False,
        )

    store = SqliteOnboardingStore(app.db, app.credentials, TelegramUserRepo(app.db))

    def make_onboarding(agent_expiry_days: int) -> BuilderOnboardingService:
        config = BuilderOnboardingConfig(
            base_url=settings.rest_base_url,
            trading_chain_id=714,
            registration_chain_id=settings.registration_chain_id,
            builder_address=settings.builder_address,
            builder_name=settings.builder_name,
            max_fee_rate=settings.max_fee_rate,
            agent_expiry_days=agent_expiry_days,
            approval_ttl_s=settings.builder_approval_ttl_s,
            expected_egress_ips=(),
            require_agent_ip_whitelist=False,
            expected_position_mode=settings.position_mode,
        )

        def _runtime_client(account_id: str):
            """Live signed-read client for the current identity (production wiring)."""
            runtime = app.follower_registry.get(account_id)
            if runtime is None:
                raise KeyError(f"follower runtime {account_id!r} not found")
            return runtime.client

        return BuilderOnboardingService(
            app.http,
            limiter,
            client_factory,
            store,
            config=config,
            runtime_client_factory=_runtime_client,
        )

    def make_service(onboarding: BuilderOnboardingService) -> UserApplicationService:
        return UserApplicationService(
            settings=settings_repo,
            runtime=AppStage2RuntimeAdapter(app),
            admin_ids=(FRESH_TG_ID,),
            builder_onboarding=onboarding,
        )

    yield {
        "app": app,
        "settings": settings,
        "settings_repo": settings_repo,
        "store": store,
        "limiter": limiter,
        "client_factory": client_factory,
        "make_onboarding": make_onboarding,
        "make_service": make_service,
    }

    await app.http.aclose()
    await app.db.close()


def _sign_main_wallet(eip712: dict, main_key: str) -> str:
    typed = encode_typed_data(
        domain_data=eip712["domain"],
        message_types={"ApproveAgent": eip712["types"]["ApproveAgent"]},
        message_data=eip712["message"],
    )
    return Account.sign_message(typed, main_key).signature.hex()


async def _submit_onboarding_with_retries(
    onboarding, store, app, pending_id: str, signature: str
) -> object:
    """Resume-style submit loop (established Builder e2e pattern)."""
    result = None
    mode_normalized = False
    for attempt in range(12):
        try:
            result = await onboarding.submit(
                BuilderSubmitRequest(
                    telegram_user_id=FRESH_TG_ID,
                    pending_id=pending_id,
                    signature="" if attempt else signature,
                )
            )
            break
        except OnboardingError as exc:
            if exc.code is OnboardingErrorCode.POSITION_MODE_MISMATCH and not mode_normalized:
                live_attempt = await store.get_builder_attempt(FRESH_TG_ID)
                assert live_attempt is not None
                assert live_attempt.state == "APPROVED"
                agent_identity = TradingIdentity(
                    account_id="gate-reauth-modeset",
                    user=live_attempt.user_address,
                    signer=live_attempt.signer_address,
                    chain_id=714,
                )
                agent_client = AsterClient(
                    http=app.http,
                    base_url=app.cfg.rest_base_url,
                    limiter=RateLimits(10**9, 10**9, 10**9),
                    identities=EphemeralIdentityProvider("gate-reauth-modeset", agent_identity),
                    keys=EphemeralKeyProvider(
                        "gate-reauth-modeset", live_attempt.signer_private_key
                    ),
                    nonces=NonceStore(app.db),
                    dry_run=False,
                )
                try:
                    await agent_client.set_position_mode("gate-reauth-modeset", True)
                except AsterError as mode_exc:
                    if mode_exc.code not in (-4046, -4045):
                        raise
                mode_normalized = True
                continue
            if exc.code not in {
                OnboardingErrorCode.AGENT_NOT_CONFIRMED,
                OnboardingErrorCode.SIGNED_READ_FAILED,
            }:
                raise
            if attempt == 11:
                raise
            await asyncio.sleep(1.5)
    assert result is not None
    return result


async def _submit_reauth_with_retries(service, pending_id: str, signature: str) -> object:
    """Service-level submit with the same eventual-consistency retries.

    The application boundary maps onboarding failures to
    ``OnboardingApplicationError`` carrying the same typed code.  Retries
    always resend the original signature: the service layer rejects blank
    signatures upfront, and a duplicate submit is safe by design (the
    onboarding layer resumes from the stored attempt state).
    """
    retryable = {
        OnboardingErrorCode.AGENT_NOT_CONFIRMED,
        OnboardingErrorCode.SIGNED_READ_FAILED,
    }
    result = None
    for attempt in range(12):
        try:
            result = await service.builder_reauthorize_submit(FRESH_TG_ID, pending_id, signature)
            break
        except OnboardingApplicationError as exc:
            if exc.code not in retryable:
                raise
            if attempt == 11:
                raise
            await asyncio.sleep(1.5)
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_live_same_signer_builder_reauthorization(live_reauth_stack):
    app: App = live_reauth_stack["app"]
    settings: Settings = live_reauth_stack["settings"]
    settings_repo: SettingsRepo = live_reauth_stack["settings_repo"]
    store = live_reauth_stack["store"]
    make_onboarding = live_reauth_stack["make_onboarding"]
    make_service = live_reauth_stack["make_service"]

    fresh_user, fresh_main_key = load_wallet_section("builder-gate")
    stable_id = stable_account_id(FRESH_TG_ID)

    # Phase 0: short-lived Builder onboarding (5d < 7d warning) → EXPIRING.
    onboarding_short = make_onboarding(EXPIRY_SHORT_DAYS)
    service_short = make_service(onboarding_short)

    prepared = await onboarding_short.prepare(BuilderPrepareRequest(FRESH_TG_ID, fresh_user))
    assert prepared.pending_id is not None
    onboarded = await _submit_onboarding_with_retries(
        onboarding_short,
        store,
        app,
        prepared.pending_id,
        _sign_main_wallet(prepared.eip712, fresh_main_key),
    )
    assert onboarded.paused is True

    await app.register_follower(stable_id)
    await settings_repo.upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=20,
            max_entry_deviation_pct=D("1"),
        ),
        stable_id,
    )

    dto = await service_short.account_builder(FRESH_TG_ID, refresh=True)
    print(f"LIVE reauth phase0 source={dto.identity_source} state={dto.state.value}")
    assert dto.identity_source == "builder"
    assert dto.state is AccountBuilderState.EXPIRING
    assert dto.max_fee_rate == str(settings.max_fee_rate)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert dto.agent_expires_at_ms is not None
    assert 0 < dto.agent_expires_at_ms - now_ms <= EXPIRY_WARNING_MS
    signer_before = dto.signer_masked
    verified_before = dto.verified_at
    assert signer_before and verified_before

    # Phase 1: service-level same-signer reauthorization (short expiry).
    # Proves C2 eligibility + C3 real pause/drain + same signer, still paused.
    prep = await service_short.builder_reauthorize_prepare(FRESH_TG_ID)
    assert prep.pending_id is not None
    assert str(prep.pending_id).startswith("reauth-")
    policy = await settings_repo.get_policy(stable_id)
    assert policy is not None
    assert policy.allow_new_entries is False

    signature = _sign_main_wallet(prep.eip712, fresh_main_key)
    reauthorized = await _submit_reauth_with_retries(service_short, prep.pending_id, signature)
    assert reauthorized.paused is True
    assert str(reauthorized.account_id) == stable_id
    assert reauthorized.signer_masked == signer_before

    dto2 = await service_short.account_builder(FRESH_TG_ID, refresh=True)
    print(f"LIVE reauth phase1 state={dto2.state.value}")
    assert dto2.identity_source == "builder"
    assert dto2.state is AccountBuilderState.EXPIRING
    assert dto2.signer_masked == signer_before
    assert dto2.verified_at is not None and dto2.verified_at > verified_before
    policy = await settings_repo.get_policy(stable_id)
    assert policy is not None
    assert policy.allow_new_entries is False

    # Signed exchange witnesses: same agent, same builder, same fee cap.
    follower_runtime = app.follower_registry.get(stable_id)
    assert follower_runtime is not None
    agent = await follower_runtime.client.agent(stable_id)
    builders = await follower_runtime.client.builder(stable_id)
    own = [
        record
        for record in builders
        if isinstance(getattr(record, "user_address", None), str)
        and record.user_address.lower() == fresh_user.lower()
    ]
    assert own, "reauthorized Builder proof missing on the exchange"
    assert agent.agent_address and agent.agent_address.lower() != fresh_user.lower()
    assert dto2.max_fee_rate == str(own[0].max_fee_rate)
    assert dto2.max_fee_rate == str(settings.max_fee_rate)
    assert own[0].builder_address is not None
    assert own[0].builder_address.lower() == str(settings.builder_address).lower()
    assert agent.can_read is True
    assert agent.can_perp_trade is True
    assert agent.can_withdraw is False

    # Phase 2: normal-expiry reauthorization → VERIFIED + both rails true.
    onboarding_normal = make_onboarding(EXPIRY_NORMAL_DAYS)
    service_normal = make_service(onboarding_normal)
    prep2 = await service_normal.builder_reauthorize_prepare(FRESH_TG_ID)
    assert prep2.pending_id is not None
    assert str(prep2.pending_id).startswith("reauth-")
    signature2 = _sign_main_wallet(prep2.eip712, fresh_main_key)
    reauthorized2 = await _submit_reauth_with_retries(service_normal, prep2.pending_id, signature2)
    assert reauthorized2.paused is True
    assert reauthorized2.signer_masked == signer_before

    dto3 = await service_normal.account_builder(FRESH_TG_ID, refresh=True)
    print(f"LIVE reauth phase2 state={dto3.state.value}")
    assert dto3.identity_source == "builder"
    assert dto3.state is AccountBuilderState.VERIFIED
    assert dto3.remediation is None
    assert dto3.signer_masked == signer_before
    assert dto3.verified_at is not None and dto3.verified_at > dto2.verified_at
    assert dto3.agent_expires_at_ms is not None
    assert dto3.agent_expires_at_ms - now_ms > EXPIRY_WARNING_MS

    dashboard = await service_normal.dashboard(FRESH_TG_ID, refresh=True)
    assert dashboard.builder_verified is True
    status = await service_normal.status(FRESH_TG_ID, refresh=True)
    assert status.builder_verified is True
    policy = await settings_repo.get_policy(stable_id)
    assert policy is not None
    assert policy.allow_new_entries is False

    agent2 = await follower_runtime.client.agent(stable_id)
    assert agent2.agent_address is not None
    assert agent2.agent_address.lower() == agent.agent_address.lower()

    orders = await follower_runtime.client.open_orders(stable_id)
    assert orders == []
    print(f"LIVE reauthorized same-signer paused state={dto3.state.value} rails=true orders=0")
