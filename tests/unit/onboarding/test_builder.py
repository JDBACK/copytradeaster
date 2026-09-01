"""Use-case tests for Builder (Aster Code) onboarding safety gates."""

from __future__ import annotations

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.models import AgentDTO, BalanceDTO, PositionDTO
from copytradeaster.aster.signing import recover_registration
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.onboarding.builder import EPHEMERAL_PREFIX, BuilderOnboardingService
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderPrepareRequest,
    BuilderSubmitRequest,
    OnboardingCommit,
)
from copytradeaster.storage.repositories import BuilderOnboardingAttempt
from tests.unit.aster.conftest import KEY

SIGNER = Account.from_key(KEY).address
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
BASE = "https://fapi.asterdex-testnet.com"
NOW_MS = 1_700_000_000_000
USER_WALLET = Account.create()
USER_ADDRESS = USER_WALLET.address
OTHER_WALLET = Account.create()


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


def _balance() -> BalanceDTO:
    return BalanceDTO.model_validate(
        {
            "asset": "USDT",
            "balance": "1000",
            "crossWalletBalance": "1000",
            "crossUnPnl": "0",
            "availableBalance": "1000",
            "maxWithdrawAmount": "0",
        }
    )


def _position() -> PositionDTO:
    return PositionDTO.model_validate(
        {
            "symbol": "BTCUSDT",
            "positionAmt": "0",
            "entryPrice": "0",
            "markPrice": "65000",
            "leverage": 3,
            "liquidationPrice": "0",
            "marginType": "CROSSED",
            "positionSide": "BOTH",
            "unRealizedProfit": "0",
        }
    )


class FakeResponse:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {"msg": "success"}
        self.text = "raw-body"

    def json(self) -> object:
        return self._body


class FakeHTTP:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.error = error
        self.posted_urls: list[str] = []

    async def post(self, url: str, content: bytes = b""):
        self.posted_urls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


class FakeLimiter:
    async def acquire_request(self, weight: int) -> None: ...

    async def acquire_order(self, account_id: str) -> None: ...


class FakeStore:
    def __init__(self, conflict: str | None = None, *, commit_error: bool = False) -> None:
        self.conflict = conflict
        self.commit_error = commit_error
        self.commits: list[OnboardingCommit] = []
        self.attempts: dict[int, BuilderOnboardingAttempt] = {}

    async def find_identity_conflict(self, telegram_user_id: int, user_address: str):
        return self.conflict

    async def commit_onboarding(self, payload: OnboardingCommit) -> None:
        if self.commit_error:
            raise RuntimeError("store down")
        self.commits.append(payload)

    async def get_builder_attempt(self, telegram_user_id: int) -> BuilderOnboardingAttempt | None:
        return self.attempts.get(telegram_user_id)

    async def get_builder_attempt_by_pending_id(
        self, pending_id: str
    ) -> BuilderOnboardingAttempt | None:
        for att in self.attempts.values():
            if att.pending_id == pending_id:
                return att
        return None

    async def save_builder_attempt(self, attempt: BuilderOnboardingAttempt) -> None:
        self.attempts[attempt.telegram_user_id] = attempt

    async def update_builder_attempt_state(
        self, pending_id: str, state: str, *, last_error_code: str | None = None
    ) -> bool:
        att = await self.get_builder_attempt_by_pending_id(pending_id)
        if att is None:
            return False
        att.state = state
        att.last_error_code = last_error_code
        return True

    async def delete_builder_attempt(self, telegram_user_id: int) -> bool:
        return self.attempts.pop(telegram_user_id, None) is not None

    async def delete_builder_attempt_by_pending_id(self, pending_id: str) -> bool:
        att = await self.get_builder_attempt_by_pending_id(pending_id)
        if att is not None:
            self.attempts.pop(att.telegram_user_id, None)
            return True
        return False

    async def cleanup_expired_prepared_attempts(self, now_ms: int) -> int:
        to_del = [
            uid
            for uid, att in self.attempts.items()
            if att.state == "PREPARED" and att.approval_deadline_ms < now_ms
        ]
        for uid in to_del:
            del self.attempts[uid]
        return len(to_del)


class FakeClient:
    def __init__(self) -> None:
        self.agent_record: AgentDTO | Exception = _agent()
        self.positions: list[PositionDTO] = [_position()]
        self.open_orders_result: list = []
        self.dual_side_position = False
        self.seen_ids: list[str] = []

    async def agent(self, account_id: str) -> AgentDTO:
        self.seen_ids.append(account_id)
        assert account_id.startswith(EPHEMERAL_PREFIX)
        if isinstance(self.agent_record, Exception):
            raise self.agent_record
        return self.agent_record

    async def balance(self, account_id: str) -> list[BalanceDTO]:
        return [_balance()]

    async def position_risk(self, account_id: str) -> list[PositionDTO]:
        return self.positions

    async def open_orders(self, account_id: str) -> list:
        return self.open_orders_result

    async def get_position_mode(self, account_id: str) -> bool:
        return self.dual_side_position


class _Factory:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def __call__(self, identities, keys):
        return self._client


def _service(
    *,
    config: BuilderOnboardingConfig | None = None,
    store: FakeStore | None = None,
    client: FakeClient | None = None,
    http: FakeHTTP | None = None,
    clock: dict | None = None,
    activation=None,
) -> tuple[BuilderOnboardingService, FakeStore, FakeClient, FakeHTTP, dict]:
    store = store or FakeStore()
    client = client or FakeClient()
    http = http or FakeHTTP()
    clock = clock or {"ms": NOW_MS}
    service = BuilderOnboardingService(
        http,
        FakeLimiter(),
        _Factory(client),
        store,
        config=config or _config(),
        activation=activation,
        clock_ms=lambda: clock["ms"],
        signer_factory=lambda: (SIGNER, KEY),
    )
    return service, store, client, http, clock


def _sign_payload(result, account: Account) -> str:
    typed = encode_typed_data(
        domain_data=result.eip712["domain"],
        message_types={"ApproveAgent": result.eip712["types"]["ApproveAgent"]},
        message_data=result.eip712["message"],
    )
    return Account.sign_message(typed, account.key).signature.hex()


async def test_eip712_json_payload_matches_backend_recovery():
    params = {
        "agentName": "copytradeaster",
        "agentAddress": SIGNER,
        "expired": 1867945395040,
        "canSpotTrade": False,
        "canPerpTrade": True,
        "canWithdraw": False,
        "builder": BUILDER,
        "maxFeeRate": "0.0001",
        "builderName": "copytradeaster",
        "asterChain": "Testnet",
        "user": USER_ADDRESS,
        "nonce": 1756460000000000,
    }
    payload = __import__(
        "copytradeaster.aster.signing", fromlist=["eip712_json_payload"]
    ).eip712_json_payload(params, "ApproveAgent", 56)
    signature = _sign_payload(type("R", (), {"eip712": payload})(), USER_WALLET)
    recovered = recover_registration(params, signature, "ApproveAgent", chain_id=56)
    assert recovered.lower() == USER_ADDRESS.lower()


async def test_prepare_returns_review_payload_and_masks_secrets():
    service, _store, _client, _http, _clock = _service()

    result = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))

    assert result.pending_id
    assert result.masked_user_address == f"{USER_ADDRESS[:6]}…{USER_ADDRESS[-4:]}"
    assert result.masked_signer_address == f"{SIGNER[:6]}…{SIGNER[-4:]}"
    assert result.builder_masked == f"{BUILDER[:6]}…{BUILDER[-4:]}"
    assert result.builder_name == "copytradeaster"
    assert result.max_fee_rate == "0.0001"
    assert result.signature_plan == "combined_approve_agent"
    assert result.can_perp_trade is True
    assert result.can_withdraw is False
    assert result.agent_expires_at_ms == NOW_MS + 30 * 86_400_000
    assert result.approval_expires_at_ms == NOW_MS + 600_000
    message = result.eip712["message"]
    assert result.eip712["primaryType"] == "ApproveAgent"
    assert result.eip712["domain"]["chainId"] == 56
    assert message["AgentAddress"] == SIGNER
    assert message["Builder"] == BUILDER
    assert message["CanPerpTrade"] is True
    assert message["CanWithdraw"] is False
    assert message["User"] == USER_ADDRESS
    assert "IpWhitelist" not in message


async def test_prepare_requires_builder_address():
    service, _store, _client, _http, _clock = _service(config=_config(builder_address=""))
    try:
        await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.BUILDER_CONFIG_MISSING
    else:
        raise AssertionError("expected BUILDER_CONFIG_MISSING")


async def test_prepare_refuses_non_testnet_url():
    service, _store, _client, _http, _clock = _service(
        config=_config(base_url="https://fapi.asterdex.com")
    )
    try:
        await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.TESTNET_REQUIRED
    else:
        raise AssertionError("expected TESTNET_REQUIRED")


async def test_prepare_blocks_already_connected_owner():
    service, _store, _client, _http, _clock = _service(store=FakeStore(conflict="owner"))
    try:
        await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.OWNER_CONFLICT
    else:
        raise AssertionError("expected OWNER_CONFLICT")


async def test_prepare_rejects_master_address():
    service, _store, _client, _http, _clock = _service(
        config=_config(master_user_address=USER_ADDRESS)
    )
    try:
        await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.MASTER_ADDRESS
    else:
        raise AssertionError("expected MASTER_ADDRESS")


async def test_prepare_resumes_unexpired_pending():
    service, _store, _client, _http, _clock = _service()

    first = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    second = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    assert first.pending_id == second.pending_id
    assert first.eip712 == second.eip712


async def test_new_user_address_invalidates_previous_pending():
    service, _store, _client, _http, _clock = _service()

    first = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    second = await service.prepare(BuilderPrepareRequest(101, OTHER_WALLET.address))
    assert first.pending_id != second.pending_id

    signature = _sign_payload(second, OTHER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, first.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.APPROVAL_UNKNOWN
    else:
        raise AssertionError("expected APPROVAL_UNKNOWN")


async def test_submit_end_to_end_commits_paused():
    activated: list[str] = []
    service, store, client, http, _clock = _service(activation=activated.append)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    result = await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))

    assert result.account_id == "tg-101"
    assert result.paused is True
    assert result.masked_user_address == prepared.masked_user_address
    assert result.masked_signer_address == SIGNER[:6] + "…" + SIGNER[-4:]
    assert result.builder_name == "copytradeaster"
    assert result.position_mode == "ONE_WAY"
    assert result.available_usdt == 1000
    assert activated == ["tg-101"]

    assert len(store.commits) == 1
    commit = store.commits[0]
    assert commit.account_id == "tg-101"
    assert commit.allow_new_entries is False
    assert commit.validation.can_perp_trade is True
    assert commit.validation.can_withdraw is False

    assert len(http.posted_urls) == 1
    url = http.posted_urls[0]
    assert url.startswith(f"{BASE}/fapi/v3/approveAgent?")
    assert f"user={USER_ADDRESS}" in url
    assert f"builder={BUILDER}" in url
    assert "maxFeeRate=0.0001" in url
    assert "signatureChainId=56" in url
    assert "signature=" in url
    # The generated signer key must never leak into the submitted URL.
    assert KEY not in url
    assert client.seen_ids and client.seen_ids[0].startswith(EPHEMERAL_PREFIX)


async def test_submit_rejects_signature_from_other_wallet():
    service, store, _client, http, _clock = _service()

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, OTHER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.SIGNATURE_MISMATCH
    else:
        raise AssertionError("expected SIGNATURE_MISMATCH")
    assert store.commits == []
    assert http.posted_urls == []


async def test_submit_expired_pending_is_rejected():
    service, store, _client, _http, clock = _service()

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    clock["ms"] += 601_000
    signature = _sign_payload(prepared, USER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.APPROVAL_EXPIRED
    else:
        raise AssertionError("expected APPROVAL_EXPIRED")
    assert store.commits == []


async def test_submit_exchange_failure_maps_to_typed_error():
    http = FakeHTTP(FakeResponse(status=400, body={"code": -1102, "msg": "nonce"}))
    service, store, _client, _http, _clock = _service(http=http)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.AGENT_SUBMIT_FAILED
        assert exc.exchange_code == -1102
    else:
        raise AssertionError("expected AGENT_SUBMIT_FAILED")
    assert store.commits == []


async def test_submit_agent_not_confirmed_maps_typed_error():
    client = FakeClient()
    client.agent_record = AsterError(-2015, "agent not found")
    service, _store, _client, http, _clock = _service(client=client)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.AGENT_NOT_CONFIRMED
        assert exc.exchange_code == -2015
    else:
        raise AssertionError("expected AGENT_NOT_CONFIRMED")
    # The registration POST itself succeeded before verification failed.
    assert len(http.posted_urls) == 1


async def test_submit_non_flat_account_is_rejected():
    client = FakeClient()
    client.positions = [
        PositionDTO.model_validate(
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.5",
                "entryPrice": "65000",
                "markPrice": "65000",
                "leverage": 3,
                "liquidationPrice": "0",
                "marginType": "CROSSED",
                "positionSide": "BOTH",
                "unRealizedProfit": "0",
            }
        )
    ]
    service, _store, _client, _http, _clock = _service(client=client)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.ACCOUNT_NOT_FLAT
    else:
        raise AssertionError("expected ACCOUNT_NOT_FLAT")


async def test_commit_failure_maps_to_typed_error():
    service, _store, _client, _http, _clock = _service(store=FakeStore(commit_error=True))

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.COMMIT_FAILED
    else:
        raise AssertionError("expected COMMIT_FAILED")


async def test_activation_failure_keeps_durable_commit():
    async def _broken_activation(account_id: str) -> None:
        raise RuntimeError("runtime down")

    service, store, _client, _http, _clock = _service(activation=_broken_activation)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    try:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    except OnboardingError as exc:
        assert exc.code is OnboardingErrorCode.ACTIVATION_FAILED
    else:
        raise AssertionError("expected ACTIVATION_FAILED")
    # The commit succeeded; only the runtime publication failed.
    assert len(store.commits) == 1
    attempt = await store.get_builder_attempt(101)
    assert attempt is not None
    assert attempt.state == "COMMITTED"


async def test_pending_repr_and_submit_request_never_leak_key_material():
    service, _store, _client, _http, _clock = _service()

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    submit = BuilderSubmitRequest(101, prepared.pending_id, "0xdeadbeef" * 8)
    text = f"{prepared!r} {submit!r}"
    assert KEY not in text
    assert "<redacted>" in f"{submit!r}"


async def test_submit_preflight_block_keeps_approved_for_retry():
    client = FakeClient()
    client.positions = [
        PositionDTO.model_validate(
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.5",
                "entryPrice": "65000",
                "markPrice": "65000",
                "leverage": 3,
                "liquidationPrice": "0",
                "marginType": "CROSSED",
                "positionSide": "BOTH",
                "unRealizedProfit": "0",
            }
        )
    ]
    service, store, _client, http, _clock = _service(client=client)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    with pytest.raises(OnboardingError) as exc_info:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    assert exc_info.value.code is OnboardingErrorCode.ACCOUNT_NOT_FLAT

    attempt = await store.get_builder_attempt(101)
    assert attempt is not None
    assert attempt.state == "APPROVED"
    assert attempt.last_error_code == "account_not_flat"

    # Post was done once
    assert len(http.posted_urls) == 1

    # User clears positions on exchange -> retries submit without needing new signature
    client.positions = [_position()]
    result = await service.submit(BuilderSubmitRequest(101, prepared.pending_id, ""))
    assert result.account_id == "tg-101"
    assert result.paused is True
    assert await store.get_builder_attempt(101) is None


async def test_submit_recovery_after_remote_post_timeout():
    client = FakeClient()
    http = FakeHTTP(error=httpx.ReadTimeout("lost approveAgent response"))
    service, store, _client, http, clock = _service(client=client, http=http)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    signature = _sign_payload(prepared, USER_WALLET)
    with pytest.raises(OnboardingError) as exc_info:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, signature))
    assert exc_info.value.code is OnboardingErrorCode.APPROVAL_STATUS_UNKNOWN

    attempt = await store.get_builder_attempt(101)
    assert attempt is not None and attempt.state == "APPROVAL_SENT"
    assert attempt.last_error_code == "-1006"

    # Simulate a process restart after the exchange accepted the mutation but
    # the local HTTP response was lost. Recovery must not POST again.
    http.error = None
    service, _store, _client, _http, _clock = _service(
        client=client,
        store=store,
        http=http,
        clock=clock,
    )

    # Next submit finds Agent via GET /agent -> state becomes APPROVED -> preflight -> commit
    result = await service.submit(BuilderSubmitRequest(101, prepared.pending_id, ""))
    assert result.account_id == "tg-101"
    assert len(http.posted_urls) == 1
    assert len(store.commits) == 1
    assert await store.get_builder_attempt(101) is None


async def test_unknown_approval_log_omits_transport_payload(caplog):
    secret = "signature-or-response-secret"
    http = FakeHTTP(error=httpx.ReadTimeout(secret))
    service, _store, _client, _http, _clock = _service(http=http)
    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))

    with caplog.at_level("WARNING"), pytest.raises(OnboardingError):
        await service.submit(
            BuilderSubmitRequest(101, prepared.pending_id, _sign_payload(prepared, USER_WALLET))
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "outcome unknown" in messages
    assert secret not in messages


async def test_submit_unknown_agent_read_keeps_approval_sent():
    client = FakeClient()
    client.agent_record = AsterError(-1000, "unknown exchange outcome")
    service, store, _client, _http, _clock = _service(client=client)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    attempt = await store.get_builder_attempt(101)
    assert attempt is not None
    await store.update_builder_attempt_state(prepared.pending_id, "APPROVAL_SENT")

    with pytest.raises(OnboardingError) as exc_info:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, ""))
    assert exc_info.value.code is OnboardingErrorCode.SIGNED_READ_FAILED
    assert exc_info.value.exchange_code == -1000

    reloaded = await store.get_builder_attempt(101)
    assert reloaded is not None
    assert reloaded.state == "APPROVAL_SENT"


async def test_submit_recovery_waits_until_deadline_before_allowing_resign():
    client = FakeClient()
    client.agent_record = AsterError(-2015, "agent not found")
    service, store, _client, http, clock = _service(client=client)

    prepared = await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    attempt = await store.get_builder_attempt(101)
    assert attempt is not None
    attempt.state = "APPROVAL_SENT"

    with pytest.raises(OnboardingError) as exc_info:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, ""))
    assert exc_info.value.code is OnboardingErrorCode.AGENT_NOT_CONFIRMED

    # A single not-found response may be exchange propagation delay. Never
    # enable a blind re-submit while the original approval window is active.
    reloaded = await store.get_builder_attempt(101)
    assert reloaded is not None
    assert reloaded.state == "APPROVAL_SENT"

    clock["ms"] += 601_000
    with pytest.raises(OnboardingError) as expired_info:
        await service.submit(BuilderSubmitRequest(101, prepared.pending_id, ""))
    assert expired_info.value.code is OnboardingErrorCode.AGENT_NOT_CONFIRMED

    reloaded = await store.get_builder_attempt(101)
    assert reloaded is not None
    assert reloaded.state == "PREPARED"


async def test_prepare_lazy_cleanups_expired_attempts():
    service, store, _client, http, clock = _service()
    await service.prepare(BuilderPrepareRequest(101, USER_ADDRESS))
    attempt = await store.get_builder_attempt(101)
    assert attempt is not None

    # Advance time past deadline
    clock["ms"] += 700_000

    await service.prepare(BuilderPrepareRequest(102, USER_ADDRESS))
    # User 101's expired attempt is cleaned up
    assert await store.get_builder_attempt(101) is None
    assert await store.get_builder_attempt(102) is not None
