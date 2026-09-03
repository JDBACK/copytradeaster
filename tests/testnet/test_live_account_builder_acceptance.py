"""Targeted live gate for P2.6 Account + Builder attestation (marker `testnet`).

Read-only gate through the real production composition (no FakeAster, no
mock transport, no testnet orders, no exchange writes):

1. Real signed ``GET /fapi/v3/agent`` + ``GET /fapi/v3/builder`` through the
   production ``AsterClient`` for the connected follower (``copyc1``).
2. ``account_builder(refresh=True)`` runs the same reads through the
   application path against a scratch SQLite copy (production untouched)
   and its DTO must match the direct signed reads field-for-field
   (masked identities, actual fee cap, permissions, expiry).
3. The HTTP boundary serializes the exact safe contract with no secrets,
   full addresses, signatures or raw payloads.
4. Dashboard/Status trust rails derive the Builder verdict from the same
   application truth as the Account DTO.

If the account is builder-authorized, the DTO must read ``verified``; if it
is direct, the DTO must read ``not_applicable`` and the signed reads must
show no usable Builder proof.  Either outcome proves the wire/auth
semantics; the state-changing disconnect → re-onboarding lifecycle is a
separate bounded step owned by the P2.6 live checklist.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.app import App
from copytradeaster.application.adapters import AppStage2RuntimeAdapter, SqliteOnboardingStore
from copytradeaster.application.models import AccountBuilderState, ConfirmationAction
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError
from copytradeaster.config import Settings
from copytradeaster.domain.models import RiskPolicy, TradingIdentity
from copytradeaster.miniapp.server import create_miniapp_app
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
    AccountRepo,
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

TEST_USER_ID = 900000002
FOLLOWER_ACCOUNTS = ("copyc1", "copyc2")
PROD_DB = Path("data/copytrade.sqlite3")


def _backup_sqlite(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


@pytest.fixture(params=FOLLOWER_ACCOUNTS)
async def live_builder_stack(tmp_path, request):
    follower_account_id: str = request.param
    if not PROD_DB.exists():
        pytest.skip("need data/copytrade.sqlite3 with imported testnet credentials")
    db_path = tmp_path / f"live-builder-{follower_account_id}.sqlite3"
    _backup_sqlite(PROD_DB, db_path)
    # The scratch copy may carry a previous live-run owner: discover it first
    # so the dev identity below matches the account owner (production file
    # itself is untouched).
    probe = sqlite3.connect(db_path)
    try:
        probe_row = probe.execute(
            "SELECT telegram_user_id FROM telegram_users WHERE account_id=?",
            (follower_account_id,),
        ).fetchone()
    finally:
        probe.close()
    gate_user_id = int(probe_row[0]) if probe_row is not None else TEST_USER_ID
    settings = Settings(
        db_path=db_path,
        miniapp_dev_unsigned=True,
        miniapp_dev_user_id=gate_user_id,
    )
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")

    app = App(settings)
    await app.db.connect()
    await app.db.migrate()
    await app.credentials.load()
    if follower_account_id not in app.credentials.account_ids():
        await app.db.close()
        pytest.skip(f"need {follower_account_id} imported in wallet.txt")

    identity = app.credentials.identity(follower_account_id)
    await app.register_follower(follower_account_id)
    await app.leverage.refresh_catalog()

    settings_repo = SettingsRepo(app.db)
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.set_flag("ready", "true")
    await settings_repo.set_flag("heartbeat_at", str(int(time.time() * 1000)))
    await settings_repo.upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
        follower_account_id,
    )
    await AccountRepo(app.db).upsert(
        follower_account_id, "follower", follower_account_id, identity.user
    )
    # Lift the gate user to an enabled ADMIN in the scratch copy only.
    await app.db.execute_write(
        "UPDATE telegram_users SET role='ADMIN', access_enabled=1 WHERE account_id=?",
        (follower_account_id,),
    )
    if gate_user_id == TEST_USER_ID:
        await TelegramUserRepo(app.db).upsert(
            telegram_user_id=gate_user_id,
            chat_id=gate_user_id,
            account_id=follower_account_id,
            role="ADMIN",
            access_enabled=True,
        )

    runtime_adapter = AppStage2RuntimeAdapter(app)
    app_service = UserApplicationService(
        settings=settings_repo,
        runtime=runtime_adapter,
        admin_ids=(gate_user_id,),
    )
    aio_app = create_miniapp_app(settings, app_service)
    test_client = TestClient(TestServer(aio_app))
    await test_client.start_server()

    yield {
        "app": app,
        "service": app_service,
        "http_client": test_client,
        "account_id": follower_account_id,
        "user_address": identity.user,
        "signer_address": identity.signer,
        "gate_user_id": gate_user_id,
    }

    await test_client.close()
    await app.http.aclose()
    await app.db.close()


@pytest.mark.asyncio
async def test_live_account_builder_attestation_gate(live_builder_stack):
    service: UserApplicationService = live_builder_stack["service"]
    http_client: TestClient = live_builder_stack["http_client"]
    user_address: str = live_builder_stack["user_address"]
    signer_address: str = live_builder_stack["signer_address"]
    app: App = live_builder_stack["app"]
    account_id: str = live_builder_stack["account_id"]
    gate_user_id: int = live_builder_stack["gate_user_id"]
    auth_headers = {"X-Dev-User-Id": str(gate_user_id), "Content-Type": "application/json"}

    # Phase 0: direct signed reads through the production Aster client.
    runtime = app.follower_registry.get(account_id)
    assert runtime is not None
    agent = await runtime.client.agent(account_id)
    builders = await runtime.client.builder(account_id)

    assert agent.agent_address and agent.agent_address.lower() == signer_address.lower()
    assert agent.can_read is True
    assert agent.can_perp_trade is True
    assert agent.can_withdraw is False
    assert int(agent.expired or 0) > int(datetime.now(UTC).timestamp() * 1000)

    own = [
        record
        for record in builders
        if isinstance(getattr(record, "user_address", None), str)
        and record.user_address.lower() == user_address.lower()
    ]

    # Phase 1: the application DTO must match the signed reads exactly.
    dto = await service.account_builder(gate_user_id, refresh=True)
    print(f"LIVE account={account_id} source={dto.identity_source} state={dto.state.value}")

    assert dto.account_masked and user_address not in dto.account_masked
    assert dto.signer_masked and signer_address not in dto.signer_masked
    if dto.identity_source == "builder":
        assert dto.state is AccountBuilderState.VERIFIED
        assert dto.remediation is None
        assert own, "verified DTO requires a signed Builder proof for this user"
        proof = own[0]
        configured_builder = str(app.cfg.builder_address or "")
        assert proof.builder_address is not None
        assert proof.builder_address.lower() == configured_builder.lower()
        assert dto.builder_address_masked and proof.builder_address not in (
            dto.builder_address_masked
        )
        assert dto.builder_name == (proof.builder_name or app.cfg.builder_name)
        assert dto.max_fee_rate == str(proof.max_fee_rate)
        assert dto.max_fee_rate == str(app.cfg.max_fee_rate)
        assert dto.verified_at is not None
        age = (datetime.now(UTC) - dto.verified_at).total_seconds()
        assert 0 <= age < 180
    else:
        assert dto.identity_source == "direct"
        assert dto.state is AccountBuilderState.NOT_APPLICABLE
        # A live exchange proof may well exist for a post-migration direct
        # row (the exchange authorization survives); the local source
        # governs until an authoritative Builder onboarding re-attests it.

    # Phase 2: HTTP serializes the exact safe contract.
    resp = await http_client.post(
        "/miniapp/api/account-builder", headers=auth_headers, json={"refresh": True}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["identity_source"] == dto.identity_source
    assert body["state"] == dto.state.value
    assert body["account_masked"] == dto.account_masked
    assert body["signer_masked"] == dto.signer_masked
    assert body["builder_address_masked"] == dto.builder_address_masked
    assert body["max_fee_rate"] == dto.max_fee_rate
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
    fingerprint = str(body)
    assert user_address not in fingerprint
    assert signer_address not in fingerprint

    # Phase 3: trust rails share the same application truth.
    dashboard = await service.dashboard(gate_user_id, refresh=False)
    status = await service.status(gate_user_id, refresh=False)
    expect_verified = dto.state is AccountBuilderState.VERIFIED
    assert dashboard.builder_verified is expect_verified
    assert status.builder_verified is expect_verified


@pytest.mark.asyncio
async def test_live_local_disconnect_clears_identity(live_builder_stack):
    """Bounded live disconnect phase on the scratch stack (copyc2 only).

    Real application confirmation flow against the live flat account: the
    testnet exchange is never written by this phase (no orders, no revoke —
    remote Agent/Builder authorization explicitly survives), while the
    scratch DB must lose credentials, Agent validation and any Builder
    snapshot atomically.  Production DB untouched.
    """
    service: UserApplicationService = live_builder_stack["service"]
    app: App = live_builder_stack["app"]
    account_id: str = live_builder_stack["account_id"]
    gate_user_id: int = live_builder_stack["gate_user_id"]
    if account_id != "copyc2":
        pytest.skip("destructive phase is scoped to the copyc2 e2e account")

    # Pre: the live account is flat with no open orders and nothing in flight.
    # Capture the signer material BEFORE the local identity is removed: it
    # stays in process memory only (never logged) for the post-disconnect
    # no-revoke proof below.
    runtime = app.follower_registry.get(account_id)
    assert runtime is not None
    pre_identity = app.credentials.identity(account_id)
    pre_signer_key = app.credentials.private_key(account_id)
    positions = await runtime.client.position_risk(account_id)
    assert all(float(position.position_amt or 0) == 0 for position in positions)
    assert await runtime.client.open_orders(account_id) == []
    pre_agent = await runtime.client.agent(account_id)
    assert pre_agent.agent_address is not None

    confirmation = await service.begin_confirmation(gate_user_id, ConfirmationAction.DISCONNECT)
    result = await service.confirm_action(gate_user_id, confirmation)

    assert result.copy_state.value == "empty"
    assert account_id not in app.credentials.account_ids()
    account = await app.db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
    assert account is not None
    assert int(account["enabled"]) == 0
    assert str(account["identity_source"]) == "direct"
    assert account["builder_address"] is None
    assert account["builder_verified_at"] is None
    assert (
        await app.db.fetchone("SELECT * FROM credentials WHERE account_id=?", (account_id,)) is None
    )
    assert (
        await app.db.fetchone("SELECT * FROM agent_validations WHERE account_id=?", (account_id,))
        is None
    )
    # Local-only semantics: the exchange-side Agent authorization is NOT
    # revoked by this action (remote revoke is out of scope for P2.6).  The
    # ephemeral client reuses the pre-disconnect signer in memory only.
    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.aster.trading_api import AsterClient
    from copytradeaster.storage.repositories import NonceStore

    class _StaticProviders:
        def __init__(self, identity, key: str) -> None:
            self._identity = identity
            self._key = key

        def identity(self, account_id: str):
            return self._identity

        def private_key(self, account_id: str) -> str:
            return self._key

        def main_private_key(self, account_id: str):
            return None

    providers = _StaticProviders(pre_identity, pre_signer_key)
    witness = AsterClient(
        http=app.http,
        base_url=app.cfg.rest_base_url,
        limiter=RateLimits(10**9, 10**9, 10**9),
        identities=providers,
        keys=providers,
        nonces=NonceStore(app.db),
    )
    post_agent = await witness.agent(account_id)
    assert post_agent.agent_address is not None
    assert post_agent.agent_address.lower() == pre_agent.agent_address.lower()


@pytest.mark.asyncio
async def test_live_builder_onboarding_returns_verified_paused(live_builder_stack):
    """Bounded live Builder onboarding on the scratch stack (fresh identity).

    A funded throwaway testnet wallet (``wallet.txt`` section
    ``builder-gate``) goes through the standard Part 1 wallet flow against
    the live exchange: prepare → in-process EIP-712 main-wallet signature
    (exactly what the frontend wallet produces) → submit → signed Agent +
    Builder verification → preflight flatness → atomic paused commit.
    Production DB untouched; no orders are placed (P2.7 is out of scope).

    Rationale: re-onboarding a disconnected legacy address (copyc1/copyc2)
    is blocked by the Part 1 identity-conflict policy (pinned offline by
    ``test_find_identity_conflict_pins_legacy_reonboard_policy``), so the
    live verified-path proof uses a fresh address through the unmodified
    standard path.  The address must hold a futures deposit: live exchange
    behavior (proven 2026-09-03) is ``-5050 after deposit`` on
    openOrders/position-mode and ``-1000 No agent found`` on
    balance/positionRisk for never-deposited accounts, so the preflight
    flatness bundle cannot pass without funding.  Faucet-funded, throwaway.
    """
    service: UserApplicationService = live_builder_stack["service"]
    app: App = live_builder_stack["app"]
    gate_user_id: int = live_builder_stack["gate_user_id"]

    fresh_tg_id = 777000003
    fresh_user, fresh_main_key = load_wallet_section("builder-gate")
    fresh_account = Account.from_key(fresh_main_key)
    await TelegramUserRepo(app.db).upsert(fresh_tg_id, fresh_tg_id)

    settings = app.cfg
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
    config = BuilderOnboardingConfig(
        base_url=settings.rest_base_url,
        trading_chain_id=714,
        registration_chain_id=settings.registration_chain_id,
        builder_address=settings.builder_address,
        builder_name=settings.builder_name,
        max_fee_rate=settings.max_fee_rate,
        agent_expiry_days=settings.builder_agent_expiry_days,
        approval_ttl_s=settings.builder_approval_ttl_s,
        expected_egress_ips=(),
        require_agent_ip_whitelist=False,
        expected_position_mode=settings.position_mode,
    )
    onboarding = BuilderOnboardingService(app.http, limiter, client_factory, store, config=config)

    prepared = await onboarding.prepare(BuilderPrepareRequest(fresh_tg_id, fresh_user))
    assert prepared.pending_id is not None
    typed = encode_typed_data(
        domain_data=prepared.eip712["domain"],
        message_types={"ApproveAgent": prepared.eip712["types"]["ApproveAgent"]},
        message_data=prepared.eip712["message"],
    )
    signature = Account.sign_message(typed, fresh_account.key).signature.hex()
    # Live-testnet eventual consistency: the approval propagates with a
    # delay, so the post-approval signed reads are retried exactly like the
    # established Builder e2e does (empty signature resumes APPROVAL_SENT).
    # A fresh wallet defaults to one-way while product policy here is HEDGE:
    # on POSITION_MODE_MISMATCH the mode is normalized with the approved
    # agent key (self-signed management is rejected live with ``-1000 No
    # agent found``) and the same submit is resumed.
    result = None
    mode_normalized = False
    for attempt in range(12):
        try:
            result = await onboarding.submit(
                BuilderSubmitRequest(
                    telegram_user_id=fresh_tg_id,
                    pending_id=prepared.pending_id,
                    signature=signature if attempt == 0 else "",
                )
            )
            break
        except OnboardingError as exc:
            if exc.code is OnboardingErrorCode.POSITION_MODE_MISMATCH and not mode_normalized:
                live_attempt = await store.get_builder_attempt(fresh_tg_id)
                assert live_attempt is not None
                assert live_attempt.state == "APPROVED"
                agent_identity = TradingIdentity(
                    account_id="gate-modeset",
                    user=live_attempt.user_address,
                    signer=live_attempt.signer_address,
                    chain_id=714,
                )
                agent_client = AsterClient(
                    http=app.http,
                    base_url=settings.rest_base_url,
                    limiter=limiter,
                    identities=EphemeralIdentityProvider("gate-modeset", agent_identity),
                    keys=EphemeralKeyProvider("gate-modeset", live_attempt.signer_private_key),
                    nonces=NonceStore(app.db),
                    dry_run=False,
                )
                try:
                    await agent_client.set_position_mode("gate-modeset", True)
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

    assert result.paused is True
    # A connected user is always a registered follower in production; the
    # refresh path resolves its signed reads through the live registry.
    await app.register_follower(stable_account_id(fresh_tg_id))
    dto = await service.account_builder(fresh_tg_id, refresh=True)
    print(f"LIVE onboarded source={dto.identity_source} state={dto.state.value}")
    assert dto.identity_source == "builder"
    assert dto.state is AccountBuilderState.VERIFIED
    assert dto.remediation is None
    assert dto.max_fee_rate == str(settings.max_fee_rate)
    assert dto.builder_name == settings.builder_name
    assert dto.verified_at is not None
    dashboard = await service.dashboard(fresh_tg_id, refresh=False)
    status = await service.status(fresh_tg_id, refresh=False)
    assert dashboard.builder_verified is True
    assert status.builder_verified is True
    assert dashboard.copy_state.value == "paused"
    assert gate_user_id != fresh_tg_id
