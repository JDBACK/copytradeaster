"""P2.6 same-signer reauthorization regression tests.

The reauthorization path repairs the current Builder identity — it never
rotates the signer (no orphan Agent stays authorized remotely), binds the
payload to the server-side account/user/signer/builder, rejects foreign
wallets and stale attempts, and leaves the account paused.  Uses the same
fake style as the onboarding use-case tests; no new test framework.
"""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.models import AgentDTO, BuilderDTO
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.onboarding.builder import REAUTH_PENDING_PREFIX, BuilderOnboardingService
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderSubmitRequest,
    ReauthorizeContext,
)
from copytradeaster.storage.repositories import BuilderOnboardingAttempt
from tests.unit.aster.conftest import KEY

SIGNER = Account.from_key(KEY).address
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
BASE = "https://fapi.asterdex-testnet.com"
NOW_MS = 1_700_000_000_000
USER_WALLET = Account.create()
USER_ADDRESS = USER_WALLET.address
FOREIGN_WALLET = Account.create()
ACCOUNT_ID = "tg-7"


def _config(**changes: object) -> BuilderOnboardingConfig:
    values: dict[str, object] = {
        "base_url": BASE,
        "builder_address": BUILDER,
        "builder_name": "copytradeaster",
        "max_fee_rate": "0.0001",
        "agent_expiry_days": 30,
        "approval_ttl_s": 600,
        "expected_egress_ips": ("203.0.113.7",),
    }
    values.update(changes)
    return BuilderOnboardingConfig(**values)


def _agent(**changes: object) -> AgentDTO:
    values: dict[str, object] = {
        "agentAddress": SIGNER,
        "agentName": "copytradeaster",
        "expired": NOW_MS + 29 * 24 * 60 * 60 * 1000,
        "ipWhitelist": "203.0.113.7/32",
        "canRead": True,
        "canSpotTrade": False,
        "canPerpTrade": True,
        "canWithdraw": False,
    }
    values.update(changes)
    return AgentDTO.model_validate(values)


def _builder_record(**changes: object) -> BuilderDTO:
    values: dict[str, object] = {
        "userAddress": USER_ADDRESS,
        "builderAddress": BUILDER,
        "builderName": "copytradeaster",
        "maxFeeRate": "0.0001",
    }
    values.update(changes)
    return BuilderDTO.model_validate(values)


class FakeResponse:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {"msg": "success"}
        self.text = "raw-body"

    def json(self) -> object:
        return self._body


class FakeHTTP:
    def __init__(self) -> None:
        self.posted_urls: list[str] = []

    async def post(self, url: str, content: bytes = b""):
        self.posted_urls.append(url)
        return FakeResponse()


class FakeLimiter:
    async def acquire_request(self, weight: int) -> None: ...

    async def acquire_order(self, account_id: str) -> None: ...


class FakeReauthStore:
    """Durable store double with a fixed current builder identity."""

    def __init__(self) -> None:
        self.attempts: dict[int, BuilderOnboardingAttempt] = {}
        self.ctx = ReauthorizeContext(
            account_id=ACCOUNT_ID,
            user_address=USER_ADDRESS,
            signer_address=SIGNER,
        )
        self.version = "v1"
        self.commits: list[dict] = []

    async def get_reauthorize_context(self, telegram_user_id: int):
        del telegram_user_id
        return self.ctx

    async def account_version(self, account_id: str):
        del account_id
        return self.version

    async def commit_reauthorization(self, **kwargs) -> None:
        if kwargs.get("expected_version") != self.version:
            raise ValueError("stale version")
        self.commits.append(kwargs)

    async def find_identity_conflict(self, telegram_user_id: int, user_address: str):
        return None

    async def commit_onboarding(self, payload) -> None:
        raise AssertionError("reauthorization must not rotate credentials")

    async def get_builder_attempt(self, telegram_user_id: int):
        return self.attempts.get(telegram_user_id)

    async def get_builder_attempt_by_pending_id(self, pending_id: str):
        for attempt in self.attempts.values():
            if attempt.pending_id == pending_id:
                return attempt
        return None

    async def save_builder_attempt(self, attempt: BuilderOnboardingAttempt) -> None:
        self.attempts[attempt.telegram_user_id] = attempt

    async def update_builder_attempt_state(
        self, pending_id: str, state: str, *, last_error_code: str | None = None
    ) -> bool:
        attempt = await self.get_builder_attempt_by_pending_id(pending_id)
        if attempt is None:
            return False
        attempt.state = state
        attempt.last_error_code = last_error_code
        return True

    async def delete_builder_attempt(self, telegram_user_id: int) -> bool:
        return self.attempts.pop(telegram_user_id, None) is not None

    async def delete_builder_attempt_by_pending_id(self, pending_id: str) -> bool:
        attempt = await self.get_builder_attempt_by_pending_id(pending_id)
        if attempt is not None:
            self.attempts.pop(attempt.telegram_user_id, None)
            return True
        return False

    async def cleanup_expired_prepared_attempts(self, now_ms: int) -> int:
        return 0


class FakeRuntimeClient:
    def __init__(self) -> None:
        self.agent_record: AgentDTO | Exception = _agent()
        self.builder_records: list[BuilderDTO] | Exception = [_builder_record()]

    async def agent(self, account_id: str) -> AgentDTO:
        if isinstance(self.agent_record, Exception):
            raise self.agent_record
        return self.agent_record

    async def builder(self, account_id: str) -> list[BuilderDTO]:
        if isinstance(self.builder_records, Exception):
            raise self.builder_records
        return self.builder_records


def _service(
    store: FakeReauthStore,
    client: FakeRuntimeClient,
    clock: dict,
    signer_calls: list,
) -> BuilderOnboardingService:
    def _signer_factory():
        signer_calls.append(1)
        return SIGNER, KEY

    return BuilderOnboardingService(
        FakeHTTP(),
        FakeLimiter(),
        lambda identities, keys: client,
        store,  # type: ignore[arg-type]
        config=_config(),
        clock_ms=lambda: clock["ms"],
        signer_factory=_signer_factory,
        runtime_client_factory=lambda account_id: client,
    )


def _env() -> tuple[BuilderOnboardingService, FakeReauthStore, FakeRuntimeClient, dict, list]:
    store = FakeReauthStore()
    client = FakeRuntimeClient()
    clock = {"ms": NOW_MS}
    signer_calls: list = []
    return _service(store, client, clock, signer_calls), store, client, clock, signer_calls


def _sign(eip712: dict, wallet: Account) -> str:
    typed = encode_typed_data(
        domain_data=eip712["domain"],
        message_types={"ApproveAgent": eip712["types"]["ApproveAgent"]},
        message_data=eip712["message"],
    )
    return Account.sign_message(typed, wallet.key).signature.hex()


async def _prepared(env) -> tuple[object, FakeReauthStore]:
    service, store, _, _, _ = env
    result = await service.reauthorize_prepare(7)
    return result, store


async def test_prepare_unavailable_without_context():
    service, store, _, _, _ = _env()
    store.ctx = None  # type: ignore[assignment]

    try:
        await service.reauthorize_prepare(7)
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.REAUTHORIZE_UNAVAILABLE
    else:
        raise AssertionError("expected REAUTHORIZE_UNAVAILABLE")


async def test_prepare_reuses_current_signer_without_rotation():
    env = _env()
    service, _, _, _, signer_calls = env

    result = await service.reauthorize_prepare(7)

    assert result.pending_id.startswith(REAUTH_PENDING_PREFIX)
    assert result.eip712["message"]["AgentAddress"] == SIGNER
    assert result.eip712["message"]["User"] == USER_ADDRESS
    assert result.eip712["message"]["Builder"] == BUILDER
    # No new signer is generated for a re-approval.
    assert signer_calls == []


async def test_submit_success_repairs_proof_paused():
    env = _env()
    service, store, _, _, _ = env
    prepared, _ = await _prepared(env)

    result = await service.reauthorize_submit(
        BuilderSubmitRequest(
            telegram_user_id=7,
            pending_id=prepared.pending_id,
            signature=_sign(prepared.eip712, USER_WALLET),
        )
    )

    assert result.account_id == ACCOUNT_ID
    assert result.paused is True
    assert result.max_fee_rate == "0.0001"
    assert len(store.commits) == 1
    commit = store.commits[0]
    assert commit["account_id"] == ACCOUNT_ID
    assert commit["validation"].signer_address == SIGNER
    assert commit["builder_address"].lower() == BUILDER.lower()


async def test_submit_foreign_wallet_signature_rejected():
    env = _env()
    service, _, _, _, _ = env
    prepared, _ = await _prepared(env)

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7,
                pending_id=prepared.pending_id,
                signature=_sign(prepared.eip712, FOREIGN_WALLET),
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.SIGNATURE_MISMATCH
    else:
        raise AssertionError("expected SIGNATURE_MISMATCH")


async def test_submit_expired_attempt_rejected():
    env = _env()
    service, _, _, clock, _ = env
    prepared, _ = await _prepared(env)
    clock["ms"] = NOW_MS + 601_000

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7,
                pending_id=prepared.pending_id,
                signature=_sign(prepared.eip712, USER_WALLET),
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.APPROVAL_EXPIRED
    else:
        raise AssertionError("expected APPROVAL_EXPIRED")


async def test_submit_initial_onboarding_pending_id_rejected():
    env = _env()
    service, _, _, _, _ = env

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7, pending_id="pend-123", signature="0x" + "ab" * 65
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.APPROVAL_UNKNOWN
    else:
        raise AssertionError("expected APPROVAL_UNKNOWN")


async def test_submit_after_identity_change_aborts_without_commit():
    env = _env()
    service, store, _, _, _ = env
    prepared, _ = await _prepared(env)
    store.ctx = ReauthorizeContext(
        account_id=ACCOUNT_ID,
        user_address=USER_ADDRESS,
        signer_address="0x" + "44" * 20,
    )

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7,
                pending_id=prepared.pending_id,
                signature=_sign(prepared.eip712, USER_WALLET),
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.IDENTITY_CHANGED
    else:
        raise AssertionError("expected IDENTITY_CHANGED")
    assert store.commits == []


async def test_submit_version_change_aborts_without_commit():
    env = _env()
    service, store, client, _, _ = env
    prepared, _ = await _prepared(env)
    original_builder = client.builder

    async def _rotating_builder(account_id: str):
        store.version = "v2"
        return await original_builder(account_id)

    client.builder = _rotating_builder  # type: ignore[method-assign]

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7,
                pending_id=prepared.pending_id,
                signature=_sign(prepared.eip712, USER_WALLET),
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.IDENTITY_CHANGED
    else:
        raise AssertionError("expected IDENTITY_CHANGED")
    assert store.commits == []


async def test_prepare_same_target_returns_existing_attempt():
    env = _env()
    service, store, _, _, _ = env
    first = await service.reauthorize_prepare(7)

    second = await service.reauthorize_prepare(7)

    # Duplicate prepare is safe: the same pending payload is returned.
    assert second.pending_id == first.pending_id
    assert len(store.attempts) == 1


async def test_prepare_after_identity_change_replaces_attempt():
    env = _env()
    service, store, _, _, _ = env
    first = await service.reauthorize_prepare(7)
    store.ctx = ReauthorizeContext(
        account_id=ACCOUNT_ID,
        user_address=USER_ADDRESS,
        signer_address="0x" + "44" * 20,
    )

    second = await service.reauthorize_prepare(7)

    assert second.pending_id != first.pending_id
    assert second.eip712["message"]["AgentAddress"] == "0x" + "44" * 20


async def test_submit_agent_gone_aborts_without_commit():
    env = _env()
    service, store, client, _, _ = env
    prepared, _ = await _prepared(env)
    client.agent_record = AsterError(-2015, "Invalid API-key, IP, or permissions")

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7,
                pending_id=prepared.pending_id,
                signature=_sign(prepared.eip712, USER_WALLET),
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.AGENT_NOT_CONFIRMED
    else:
        raise AssertionError("expected AGENT_NOT_CONFIRMED")
    assert store.commits == []


async def test_submit_builder_read_failure_aborts_without_commit():
    env = _env()
    service, store, client, _, _ = env
    prepared, _ = await _prepared(env)
    client.builder_records = AsterError(429, "rate limited")

    try:
        await service.reauthorize_submit(
            BuilderSubmitRequest(
                telegram_user_id=7,
                pending_id=prepared.pending_id,
                signature=_sign(prepared.eip712, USER_WALLET),
            )
        )
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.SIGNED_READ_FAILED
    else:
        raise AssertionError("expected SIGNED_READ_FAILED")
    assert store.commits == []
