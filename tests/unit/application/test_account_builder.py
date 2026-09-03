"""P2.6 Account + Builder application regression tests.

Covers the authoritative ``AccountBuilderDTO`` over
``UserApplicationService`` with real SQLite repositories and a small
controllable runtime fake: verified/expiring/stale/unavailable/blocked/
not-applicable verdicts, exact remediation, masked-only output, the
old-proof-after-replacement race gate, and the shared trust-rail verdict.
No new test framework.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from copytradeaster.application.models import (
    AccountBuilderReason,
    AccountBuilderRemediation,
    AccountBuilderState,
    AuthorizationError,
    OnboardingApplicationError,
    ServiceUnavailableError,
    ValidationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.models import BuilderDTO
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import BuilderReauthorizeResult
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    SettingsRepo,
    TelegramUserRepo,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
USER_ADDRESS = "0x1111111111111111111111111111111111111111"
SIGNER_ADDRESS = "0x2222222222222222222222222222222222222222"
NEW_SIGNER = "0x3333333333333333333333333333333333333333"
BUILDER_ADDRESS = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
FAR_FUTURE_MS = 2_000_000_000_000


def _valid_record(signer: str = SIGNER_ADDRESS, expired_ms: int = FAR_FUTURE_MS) -> AgentValidation:
    return AgentValidation(
        signer_address=signer,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=expired_ms,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at=NOW.isoformat(),
    )


def _builder_dto() -> BuilderDTO:
    return BuilderDTO.model_validate(
        {
            "userAddress": USER_ADDRESS,
            "builderAddress": BUILDER_ADDRESS,
            "builderName": "copytradeaster",
            "maxFeeRate": "0.0001",
        }
    )


class FakeBuilderRuntime:
    """Controllable signed-read evidence for attestation tests."""

    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.cfg = SimpleNamespace(
            builder_address=BUILDER_ADDRESS,
            builder_name="copytradeaster",
            max_fee_rate="0.0001",
        )
        self.credentials = self
        self.signer = SIGNER_ADDRESS
        self.version = "v1"
        self.versions: list[str] = []
        self.permission_record: AgentValidation | None = _valid_record()
        self.permission_error: Exception | None = None
        self.builder_records: list[BuilderDTO] | None = [_builder_dto()]
        self.builder_error: Exception | None = None
        self.rotate_on_read = False
        self.pause_calls: list[str] = []
        self.pause_error: Exception | None = None

    def identity(self, account_id: str):
        del account_id
        return SimpleNamespace(signer=self.signer)

    async def account_version(self, account_id: str) -> str:
        del account_id
        self.versions.append(self.version)
        return self.version

    async def refresh_agent_permission(self, account_id: str):
        del account_id
        if self.permission_error is not None:
            raise self.permission_error
        assert self.permission_record is not None
        return self.permission_record

    async def refresh_builder_record(self, account_id: str):
        del account_id
        if self.rotate_on_read:
            # A credential replacement lands mid-read: new signer/version.
            self.signer = NEW_SIGNER
            self.version = "v2"
        if self.builder_error is not None:
            raise self.builder_error
        assert self.builder_records is not None
        return self.builder_records

    async def is_account_registered(self, account_id: str) -> bool:
        del account_id
        return True

    async def pause_account(self, account_id: str) -> None:
        """Real pause/drain seam double: logs order, injectable failure."""
        self.pause_calls.append(account_id)
        if self.pause_error is not None:
            raise self.pause_error


@pytest.fixture
async def builder_env(tmp_path: Path):
    db = Database(tmp_path / "builder.sqlite3")
    try:
        await db.connect()
        await db.migrate()
        await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER_ADDRESS)
        await TelegramUserRepo(db).upsert(7, 700, account_id="tg-7")
        runtime = FakeBuilderRuntime(db)
        service = UserApplicationService(
            runtime,  # type: ignore[arg-type]
            admin_ids=[100],
            clock=lambda: NOW,
        )
        yield db, runtime, service
    finally:
        await db.close()


async def _seed_builder_account(db: Database, verified_at: datetime) -> None:
    await AccountRepo(db).set_builder_snapshot(
        "tg-7",
        builder_address=BUILDER_ADDRESS,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=FAR_FUTURE_MS,
        builder_verified_at=verified_at.isoformat(),
    )
    await AgentValidationRepo(db).upsert("tg-7", validation=_valid_record())


async def test_direct_account_is_not_applicable(builder_env):
    _, _, service = builder_env

    dto = await service.account_builder(7, refresh=False)

    assert dto.identity_source == "direct"
    assert dto.state is AccountBuilderState.NOT_APPLICABLE
    assert dto.reason is AccountBuilderReason.DIRECT_ACCOUNT
    assert dto.remediation is None
    assert dto.builder_address_masked is None
    assert dto.max_fee_rate is None


async def test_refresh_true_on_verified_builder_stores_proof(builder_env):
    db, runtime, service = builder_env
    del runtime
    await _seed_builder_account(db, NOW)

    dto = await service.account_builder(7, refresh=True)

    assert dto.identity_source == "builder"
    assert dto.state is AccountBuilderState.VERIFIED
    assert dto.reason is AccountBuilderReason.NONE
    assert dto.remediation is None
    assert dto.builder_address_masked == "0x2576…6EF8"
    assert dto.builder_name == "copytradeaster"
    assert dto.max_fee_rate == "0.0001"
    assert dto.verified_at == NOW
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["identity_source"] == "builder"
    assert snapshot["builder_address"] == BUILDER_ADDRESS
    assert snapshot["builder_verified_at"] == NOW.isoformat()


async def test_dto_carries_no_secrets_or_full_addresses(builder_env):
    _, _, service = builder_env

    dto = await service.account_builder(7, refresh=True)

    assert USER_ADDRESS not in dto.account_masked
    assert SIGNER_ADDRESS not in (dto.signer_masked or "")
    assert BUILDER_ADDRESS not in (dto.builder_address_masked or "")
    assert dto.signer_masked == "0x2222…2222"


async def test_expiring_agent_requires_reauthorize(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    now_ms = int(NOW.timestamp() * 1000)
    runtime.permission_record = _valid_record(expired_ms=now_ms + 3 * 24 * 60 * 60 * 1000)

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.EXPIRING
    assert dto.reason is AccountBuilderReason.AGENT_EXPIRING
    assert dto.remediation is AccountBuilderRemediation.REAUTHORIZE
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["identity_source"] == "builder"


async def test_transient_failure_keeps_bound_snapshot_as_stale(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.permission_error = RuntimeError("SECRET-BOOM-aster-is-down")

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.STALE
    assert dto.reason is AccountBuilderReason.TRANSIENT_UNAVAILABLE
    assert dto.remediation is AccountBuilderRemediation.REFRESH
    # The last-known evidence stays visible with its age.
    assert dto.builder_address_masked == "0x2576…6EF8"
    assert dto.max_fee_rate == "0.0001"
    assert dto.verified_at == NOW


async def test_transient_failure_without_snapshot_is_unavailable(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    # Source stays builder but no usable proof exists.
    await AccountRepo(db).clear_builder_snapshot("tg-7")
    runtime.permission_error = RuntimeError("aster-is-down")

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.UNAVAILABLE
    assert dto.remediation is AccountBuilderRemediation.REFRESH


async def test_builder_mismatch_blocks_and_invalidates_proof(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_records = [
        BuilderDTO.model_validate(
            {
                "userAddress": USER_ADDRESS,
                "builderAddress": "0x9999999999999999999999999999999999999999",
                "builderName": "copytradeaster",
                "maxFeeRate": "0.0001",
            }
        )
    ]

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.BUILDER_MISMATCH
    assert dto.remediation is AccountBuilderRemediation.REAUTHORIZE
    # The old proof must not resurrect after restart.
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["builder_address"] is None
    assert snapshot["builder_verified_at"] is None


async def test_fee_mismatch_blocks_with_typed_reason(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_records = [
        BuilderDTO.model_validate(
            {
                "userAddress": USER_ADDRESS,
                "builderAddress": BUILDER_ADDRESS,
                "builderName": "copytradeaster",
                "maxFeeRate": "0.0002",
            }
        )
    ]

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.FEE_MISMATCH
    assert dto.remediation is AccountBuilderRemediation.REAUTHORIZE


async def test_revoked_agent_blocks_and_kills_proof(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.permission_error = AsterError(-2015, "Invalid API-key, IP, or permissions")

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.AGENT_INVALID
    assert dto.remediation is AccountBuilderRemediation.REAUTHORIZE
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["builder_address"] is None


async def test_identity_race_never_stores_old_proof(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.rotate_on_read = True

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.STALE
    assert dto.reason is AccountBuilderReason.IDENTITY_CHANGED
    assert dto.remediation is AccountBuilderRemediation.REFRESH
    # The old signer's proof must not be stored for the new signer.
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["builder_address"] is None
    assert snapshot["builder_verified_at"] is None
    assert runtime.versions[0] != runtime.versions[-1]


async def test_disabled_account_is_blocked_disconnected(builder_env):
    db, _, service = builder_env
    await AccountRepo(db).set_identity_direct("tg-7")
    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-7",))

    dto = await service.account_builder(7, refresh=False)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.ACCOUNT_DISCONNECTED


async def test_trust_verdict_true_only_for_verified(builder_env):
    db, _, service = builder_env

    assert await service._builder_verified("tg-7") is False

    await _seed_builder_account(db, NOW)

    assert await service._builder_verified("tg-7") is True

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.VERIFIED
    assert await service._builder_verified("tg-7") is True


async def test_cached_read_never_serves_stale_as_verified(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    # Poison the validation triple so durable evidence disproves the proof.
    await AgentValidationRepo(db).upsert("tg-7", validation=_valid_record(signer=NEW_SIGNER))
    runtime.signer = SIGNER_ADDRESS

    dto = await service.account_builder(7, refresh=False)

    assert dto.state is not AccountBuilderState.VERIFIED


class FakeBuilderOnboarding:
    """Same-signer re-approval double for the application boundary."""

    def __init__(self) -> None:
        self.prepare_calls: list[int] = []
        self.submit_calls: list = []

    async def reauthorize_prepare(self, telegram_user_id: int):
        self.prepare_calls.append(telegram_user_id)
        return SimpleNamespace(
            pending_id="reauth-test-1",
            masked_user_address="0x1111…1111",
            masked_signer_address="0x2222…2222",
            builder_masked="0x2576…6EF8",
            builder_name="copytradeaster",
            max_fee_rate="0.0001",
            signature_plan="combined_approve_agent",
            can_perp_trade=True,
            can_withdraw=False,
            agent_expires_at_ms=FAR_FUTURE_MS,
            approval_expires_at_ms=int(NOW.timestamp() * 1000) + 600_000,
            eip712={"domain": {}, "message": {}, "types": {}},
        )

    async def reauthorize_submit(self, request):
        self.submit_calls.append(request)
        return BuilderReauthorizeResult(
            account_id="tg-7",
            masked_user_address="0x1111…1111",
            masked_signer_address="0x2222…2222",
            builder_name="copytradeaster",
            max_fee_rate="0.0001",
            agent_expires_at_ms=FAR_FUTURE_MS,
            paused=True,
        )


def _service_with_reauth(runtime, onboarding) -> UserApplicationService:
    return UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        builder_onboarding=onboarding,
        testnet_check=True,
    )


async def test_reauthorize_prepare_rejected_for_direct_account(builder_env):
    db, runtime, _ = builder_env
    service = _service_with_reauth(runtime, FakeBuilderOnboarding())

    with pytest.raises(ValidationError):
        await service.builder_reauthorize_prepare(7)


async def test_reauthorize_prepare_rejected_when_not_required(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    service = _service_with_reauth(runtime, FakeBuilderOnboarding())

    dto = await service.account_builder(7, refresh=False)
    assert dto.state is AccountBuilderState.VERIFIED
    with pytest.raises(ValidationError):
        await service.builder_reauthorize_prepare(7)


async def test_reauthorize_prepare_allowed_from_expiring(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    now_ms = int(NOW.timestamp() * 1000)
    runtime.permission_record = _valid_record(expired_ms=now_ms + 3 * 24 * 60 * 60 * 1000)
    # Poison the cached validation so the cached verdict is expiring.
    await AgentValidationRepo(db).upsert("tg-7", validation=runtime.permission_record)
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    result = await service.builder_reauthorize_prepare(7)

    assert result.pending_id == "reauth-test-1"
    assert onboarding.prepare_calls == [7]


async def test_reauthorize_submit_returns_paused_result(builder_env):
    db, runtime, _ = builder_env
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    result = await service.builder_reauthorize_submit(7, "reauth-test-1", "0x" + "ab" * 65)

    assert result.account_id == "tg-7"
    assert result.paused is True
    assert result.max_fee_rate == "0.0001"
    assert len(onboarding.submit_calls) == 1
    assert onboarding.submit_calls[0].telegram_user_id == 7
    assert SIGNER_ADDRESS not in (result.signer_masked or "")


async def test_unowned_user_gets_authorization_error(builder_env):
    _, _, service = builder_env

    with pytest.raises(AuthorizationError):
        await service.account_builder(8, refresh=False)


async def test_unexpected_failure_maps_to_unavailable(builder_env):
    _, _, service = builder_env

    async def _boom(*args, **kwargs):
        raise RuntimeError("SECRET-boom")

    service._owned_account = _boom  # type: ignore[method-assign]

    with pytest.raises(ServiceUnavailableError):
        await service.account_builder(7, refresh=True)


async def test_garbage_identity_source_reads_as_direct(builder_env):
    from copytradeaster.application.models import UserContext

    _, _, service = builder_env
    # The CHECK constraint already rejects garbage at the DB level; this
    # covers the defense-in-depth normalization for non-SQLite row shapes.
    context = UserContext(user_id=7, chat_id=700, account_id="tg-7", allowed=True)
    row = {"user_address": USER_ADDRESS, "identity_source": "builder-legacy", "enabled": 1}

    dto = await service._account_builder_for(context, row, "tg-7", refresh=False)

    assert dto.identity_source == "direct"
    assert dto.state is AccountBuilderState.NOT_APPLICABLE


async def test_verified_beyond_ttl_reads_as_stale(builder_env):
    from datetime import timedelta

    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW - timedelta(seconds=3600))

    dto = await service.account_builder(7, refresh=False)

    assert dto.state is AccountBuilderState.STALE
    assert dto.remediation is AccountBuilderRemediation.REFRESH
    assert dto.builder_address_masked == "0x2576…6EF8"


async def test_empty_builder_records_block_as_not_confirmed(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_records = []

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.BUILDER_NOT_CONFIRMED
    assert dto.remediation is AccountBuilderRemediation.REAUTHORIZE


async def test_version_check_failure_raises_unavailable(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)

    async def _no_version(account_id: str) -> str:
        raise ServiceUnavailableError("version gone")

    runtime.account_version = _no_version  # type: ignore[method-assign]

    with pytest.raises(ServiceUnavailableError):
        await service.account_builder(7, refresh=True)


async def test_missing_builder_config_raises_unavailable(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.cfg.builder_address = ""

    with pytest.raises(ServiceUnavailableError):
        await service.account_builder(7, refresh=True)


async def test_builder_transport_error_keeps_bound_snapshot_stale(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_error = ServiceUnavailableError("builder read down")

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.STALE
    assert dto.builder_address_masked == "0x2576…6EF8"


async def test_builder_exchange_error_keeps_bound_snapshot_stale(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_error = AsterError(429, "rate limited")

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.STALE
    assert dto.remediation is AccountBuilderRemediation.REFRESH


async def test_disconnect_mid_read_blocks_without_proof(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    calls = {"n": 0}

    async def _vanishing_version(account_id: str) -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise ServiceUnavailableError("credentials gone")
        return "v1"

    runtime.account_version = _vanishing_version  # type: ignore[method-assign]

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.ACCOUNT_DISCONNECTED
    assert dto.remediation is None


async def test_prepare_without_onboarding_is_unavailable(builder_env):
    _, _, service = builder_env

    with pytest.raises(ServiceUnavailableError):
        await service.builder_reauthorize_prepare(7)


async def test_prepare_disabled_builder_account_rejected(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-7",))
    service = _service_with_reauth(runtime, FakeBuilderOnboarding())

    with pytest.raises(ValidationError):
        await service.builder_reauthorize_prepare(7)


async def test_prepare_maps_onboarding_error_to_application_error(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    now_ms = int(NOW.timestamp() * 1000)
    runtime.permission_record = _valid_record(expired_ms=now_ms + 3 * 24 * 60 * 60 * 1000)
    await AgentValidationRepo(db).upsert("tg-7", validation=runtime.permission_record)

    class _FailingOnboarding(FakeBuilderOnboarding):
        async def reauthorize_prepare(self, telegram_user_id: int):
            raise OnboardingError(OnboardingErrorCode.APPROVAL_EXPIRED)

    service = _service_with_reauth(runtime, _FailingOnboarding())

    with pytest.raises(OnboardingApplicationError):
        await service.builder_reauthorize_prepare(7)


async def test_submit_rejects_blank_fields(builder_env):
    _, runtime, _ = builder_env
    service = _service_with_reauth(runtime, FakeBuilderOnboarding())

    with pytest.raises(ValidationError):
        await service.builder_reauthorize_submit(7, "  ", "0x" + "ab" * 65)
    with pytest.raises(ValidationError):
        await service.builder_reauthorize_submit(7, "reauth-1", "  ")


async def test_submit_without_onboarding_is_unavailable(builder_env):
    _, _, service = builder_env

    with pytest.raises(ServiceUnavailableError):
        await service.builder_reauthorize_submit(7, "reauth-1", "0x" + "ab" * 65)


async def test_submit_maps_onboarding_error_to_application_error(builder_env):
    _, runtime, _ = builder_env

    class _FailingOnboarding(FakeBuilderOnboarding):
        async def reauthorize_submit(self, request):
            raise OnboardingError(OnboardingErrorCode.SIGNATURE_MISMATCH)

    service = _service_with_reauth(runtime, _FailingOnboarding())

    with pytest.raises(OnboardingApplicationError):
        await service.builder_reauthorize_submit(7, "reauth-1", "0x" + "ab" * 65)


async def test_submit_rejects_mismatched_account_result(builder_env):
    _, runtime, _ = builder_env

    class _WrongAccountOnboarding(FakeBuilderOnboarding):
        async def reauthorize_submit(self, request):
            result = await super().reauthorize_submit(request)
            return BuilderReauthorizeResult(
                account_id="tg-999",
                masked_user_address=result.masked_user_address,
                masked_signer_address=result.masked_signer_address,
                builder_name=result.builder_name,
                max_fee_rate=result.max_fee_rate,
                agent_expires_at_ms=result.agent_expires_at_ms,
                paused=True,
            )

    service = _service_with_reauth(runtime, _WrongAccountOnboarding())

    with pytest.raises(ValidationError):
        await service.builder_reauthorize_submit(7, "reauth-1", "0x" + "ab" * 65)


# --- Corrective 1: the shared trust verdict is fresh, TTL- and config-bound.


def _service_at(runtime, moment):
    return UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: moment,
    )


async def test_trust_verdict_matches_verified_account(builder_env):
    db, _, service = builder_env
    await _seed_builder_account(db, NOW)

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.VERIFIED
    # Same truth feeds DashboardDTO/StatusDTO.builder_verified.
    assert await service._builder_verified("tg-7") is True


async def test_trust_verdict_false_for_stale_proof(builder_env):
    from datetime import timedelta

    db, _, service = builder_env
    await _seed_builder_account(db, NOW - timedelta(seconds=3600))

    dto = await service.account_builder(7, refresh=False)

    assert dto.state is AccountBuilderState.STALE
    assert await service._builder_verified("tg-7") is False


async def test_trust_verdict_false_for_expired_agent(builder_env):
    db, _, service = builder_env
    await _seed_builder_account(db, NOW)
    await AgentValidationRepo(db).upsert(
        "tg-7", validation=_valid_record(expired_ms=int(NOW.timestamp() * 1000) - 1)
    )

    assert await service._builder_verified("tg-7") is False


async def test_trust_verdict_false_for_changed_builder_address(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.cfg.builder_address = "0x9999999999999999999999999999999999999999"

    assert await service._builder_verified("tg-7") is False


async def test_trust_verdict_false_for_changed_fee_cap(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.cfg.max_fee_rate = "0.0002"

    assert await service._builder_verified("tg-7") is False


async def test_trust_verdict_accepts_decimal_equivalent_fee_cap(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    # Same cap, different representation: Decimal-equivalent, never float.
    runtime.cfg.max_fee_rate = "0.00010"

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.VERIFIED
    assert await service._builder_verified("tg-7") is True


async def test_trust_verdict_false_after_signer_replacement(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    await AgentValidationRepo(db).upsert("tg-7", validation=_valid_record(signer=NEW_SIGNER))
    runtime.signer = SIGNER_ADDRESS

    assert await service._builder_verified("tg-7") is False
    dto = await service.account_builder(7, refresh=False)
    assert dto.state is AccountBuilderState.BLOCKED


# --- Corrective 5: evidence timestamps are never refreshed without evidence.


async def test_cached_read_does_not_refresh_evidence_time(builder_env):
    from datetime import timedelta

    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    service = _service_at(runtime, NOW + timedelta(seconds=30))

    dto = await service.account_builder(7, refresh=False)

    assert dto.state is AccountBuilderState.VERIFIED
    assert dto.verified_at == NOW
    assert dto.last_check_at == NOW
    assert dto.age_seconds == 30


async def test_stale_after_ttl_preserves_original_timestamp(builder_env):
    from datetime import timedelta

    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW - timedelta(seconds=3600))
    service = _service_at(runtime, NOW)

    dto = await service.account_builder(7, refresh=False)

    assert dto.state is AccountBuilderState.STALE
    assert dto.last_check_at == NOW - timedelta(seconds=3600)
    assert dto.age_seconds == 3600


async def test_transient_failure_preserves_evidence_time(builder_env):
    from datetime import timedelta

    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    runtime.permission_error = RuntimeError("aster-is-down")
    service = _service_at(runtime, NOW + timedelta(seconds=30))

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.STALE
    assert dto.verified_at == NOW
    assert dto.last_check_at == NOW


async def test_successful_refresh_updates_evidence_time(builder_env):
    from datetime import timedelta

    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    service = _service_at(runtime, NOW + timedelta(seconds=30))

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.VERIFIED
    assert dto.verified_at == NOW + timedelta(seconds=30)
    assert dto.last_check_at == NOW + timedelta(seconds=30)
    assert dto.age_seconds == 0


async def test_unavailable_without_snapshot_has_no_evidence_time(builder_env):
    from datetime import timedelta

    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    await AccountRepo(db).clear_builder_snapshot("tg-7")
    runtime.permission_error = RuntimeError("aster-is-down")
    service = _service_at(runtime, NOW + timedelta(seconds=30))

    dto = await service.account_builder(7, refresh=True)
    assert dto.state is AccountBuilderState.UNAVAILABLE
    assert dto.last_check_at is None
    assert dto.age_seconds == 0

    cached = await service.account_builder(7, refresh=False)
    assert cached.state is AccountBuilderState.UNAVAILABLE
    assert cached.last_check_at is None


async def test_blocked_mismatch_has_no_evidence_time(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_records = [
        BuilderDTO.model_validate(
            {
                "userAddress": USER_ADDRESS,
                "builderAddress": "0x9999999999999999999999999999999999999999",
                "builderName": "copytradeaster",
                "maxFeeRate": "0.0001",
            }
        )
    ]

    dto = await service.account_builder(7, refresh=True)

    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.last_check_at is None


# --- Corrective 2: BLOCKED invalidates, then reauthorize_prepare still opens.


def _mismatch_records():
    return [
        BuilderDTO.model_validate(
            {
                "userAddress": USER_ADDRESS,
                "builderAddress": "0x9999999999999999999999999999999999999999",
                "builderName": "copytradeaster",
                "maxFeeRate": "0.0001",
            }
        )
    ]


async def test_blocked_mismatch_allows_reauthorize_prepare(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    runtime.builder_records = _mismatch_records()
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    dto = await service.account_builder(7, refresh=True)
    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.BUILDER_MISMATCH
    assert dto.remediation is AccountBuilderRemediation.REAUTHORIZE
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["builder_address"] is None

    result = await service.builder_reauthorize_prepare(7)

    assert result.pending_id == "reauth-test-1"
    assert onboarding.prepare_calls == [7]
    assert runtime.pause_calls == ["tg-7"]


async def test_revoked_agent_blocked_allows_reauthorize_prepare(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    runtime.permission_error = AsterError(-2015, "Invalid API-key, IP, or permissions")
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    dto = await service.account_builder(7, refresh=True)
    assert dto.state is AccountBuilderState.BLOCKED
    assert dto.reason is AccountBuilderReason.AGENT_INVALID
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["builder_address"] is None

    result = await service.builder_reauthorize_prepare(7)

    assert result.pending_id == "reauth-test-1"
    assert onboarding.prepare_calls == [7]


async def test_transient_stale_does_not_open_reauthorize(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    runtime.permission_error = RuntimeError("aster-is-down")
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    dto = await service.account_builder(7, refresh=True)
    assert dto.state is AccountBuilderState.STALE

    with pytest.raises(ValidationError):
        await service.builder_reauthorize_prepare(7)
    assert onboarding.prepare_calls == []


async def test_unavailable_without_snapshot_does_not_open_reauthorize(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    await AccountRepo(db).clear_builder_snapshot("tg-7")
    runtime.permission_error = RuntimeError("aster-is-down")
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    dto = await service.account_builder(7, refresh=True)
    assert dto.state is AccountBuilderState.UNAVAILABLE

    with pytest.raises(ValidationError):
        await service.builder_reauthorize_prepare(7)
    assert onboarding.prepare_calls == []


# --- Corrective 3: real runtime pause/drain before any wallet payload.


def _expiring_env(db, runtime):
    now_ms = int(NOW.timestamp() * 1000)
    runtime.permission_record = _valid_record(expired_ms=now_ms + 3 * 24 * 60 * 60 * 1000)
    return AgentValidationRepo(db).upsert("tg-7", validation=runtime.permission_record)


async def _seed_open_policy(db: Database) -> None:
    """Production always has a policy row; patch_policy never upserts."""
    from decimal import Decimal

    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=Decimal("0.01"),
            max_leverage=20,
            max_entry_deviation_pct=Decimal("1"),
        ),
        "tg-7",
    )


class _PauseOrderOnboarding(FakeBuilderOnboarding):
    """Fails unless the runtime drain landed before the wallet payload."""

    def __init__(self, runtime) -> None:
        super().__init__()
        self.runtime = runtime

    async def reauthorize_prepare(self, telegram_user_id: int):
        assert self.runtime.pause_calls == ["tg-7"]
        return await super().reauthorize_prepare(telegram_user_id)


async def test_reauthorize_prepare_pauses_before_payload(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    await _seed_open_policy(db)
    await _expiring_env(db, runtime)
    onboarding = _PauseOrderOnboarding(runtime)
    service = _service_with_reauth(runtime, onboarding)

    result = await service.builder_reauthorize_prepare(7)

    assert result.pending_id == "reauth-test-1"
    assert runtime.pause_calls == ["tg-7"]
    assert onboarding.prepare_calls == [7]
    assert service._settings is not None
    policy = await service._settings.get_policy("tg-7")
    assert policy is not None
    assert policy.allow_new_entries is False


async def test_reauthorize_prepare_fails_closed_when_pause_fails(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    await _seed_open_policy(db)
    await _expiring_env(db, runtime)
    runtime.pause_error = RuntimeError("drain failed")
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    with pytest.raises(ServiceUnavailableError):
        await service.builder_reauthorize_prepare(7)
    # No wallet payload after a failed pause ...
    assert onboarding.prepare_calls == []
    # ... and new entries stay forbidden.
    assert service._settings is not None
    policy = await service._settings.get_policy("tg-7")
    assert policy is not None
    assert policy.allow_new_entries is False


async def test_reauthorize_submit_keeps_account_paused(builder_env):
    db, runtime, _ = builder_env
    await _seed_builder_account(db, NOW)
    await _seed_open_policy(db)
    await _expiring_env(db, runtime)
    onboarding = FakeBuilderOnboarding()
    service = _service_with_reauth(runtime, onboarding)

    await service.builder_reauthorize_prepare(7)
    result = await service.builder_reauthorize_submit(7, "reauth-test-1", "0x" + "ab" * 65)

    assert result.paused is True
    assert service._settings is not None
    policy = await service._settings.get_policy("tg-7")
    assert policy is not None
    assert policy.allow_new_entries is False


# --- Corrective 4: a pre-lock row can never promote direct back to builder.


async def test_pre_lock_direct_replacement_cannot_be_repromoted(builder_env):
    db, runtime, service = builder_env
    await _seed_builder_account(db, NOW)
    real_account_row = service._account_row
    calls = {"n": 0}

    async def _row_with_midflight_replacement(account_id: str):
        calls["n"] += 1
        if calls["n"] == 2:
            # A direct replacement commits between the pre-lock read and
            # the locked refresh: new signer, rewritten credentials, proof
            # cleared.  The version marker moves too, exactly like the
            # production race where version_before is read post-replacement.
            runtime.signer = NEW_SIGNER
            runtime.version = "v2"
            await AccountRepo(db).set_identity_direct("tg-7")
            await AgentValidationRepo(db).upsert(
                "tg-7", validation=_valid_record(signer=NEW_SIGNER)
            )
        return await real_account_row(account_id)

    service._account_row = _row_with_midflight_replacement  # type: ignore[method-assign]

    dto = await service.account_builder(7, refresh=True)

    assert calls["n"] >= 2
    # Fail closed on the fresh state: direct, never re-promoted, even though
    # the new identity's Aster Builder record would pass the validator.
    assert dto.identity_source == "direct"
    assert dto.state is AccountBuilderState.NOT_APPLICABLE
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["identity_source"] == "direct"
    assert snapshot["builder_address"] is None
    assert snapshot["builder_verified_at"] is None
