"""Use-case tests for direct Aster onboarding safety gates."""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal

import pytest
from eth_account import Account

from copytradeaster.aster.models import AgentDTO, BalanceDTO, PositionDTO
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.onboarding.direct import (
    EPHEMERAL_ACCOUNT_ID,
    DirectOnboardingService,
    validate_agent_permissions,
)
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    DirectOnboardingConfig,
    DirectOnboardingRequest,
    OnboardingCommit,
)
from copytradeaster.onboarding.providers import EphemeralIdentityProvider, EphemeralKeyProvider
from tests.unit.aster.conftest import KEY, USER

SIGNER = Account.from_key(KEY).address
BASE = "https://fapi.asterdex-testnet.com"
NOW_MS = 1_700_000_000_000
EXPIRY_MS = NOW_MS + 24 * 60 * 60 * 1000


def _request(**changes: object) -> DirectOnboardingRequest:
    values: dict[str, object] = {
        "telegram_user_id": 101,
        "user_address": USER,
        "declared_signer_address": SIGNER,
        "signer_private_key": KEY,
    }
    values.update(changes)
    return DirectOnboardingRequest(**values)


def _agent(**changes: object) -> AgentDTO:
    values: dict[str, object] = {
        "agentAddress": SIGNER,
        "agentName": "copytradeaster",
        "expired": EXPIRY_MS,
        "ipWhitelist": "203.0.113.7/32",
        "canRead": True,
        "canSpotTrade": False,
        "canPerpTrade": True,
        "canWithdraw": False,
    }
    values.update(changes)
    return AgentDTO.model_validate(values)


def test_public_agent_validator_matches_onboarding_policy_without_bundle_reads():
    config = DirectOnboardingConfig(
        base_url=BASE,
        expected_egress_ips=("203.0.113.7",),
    )

    snapshot = validate_agent_permissions(
        _agent(),
        SIGNER,
        config,
        checked_at_ms=NOW_MS,
    )

    assert snapshot.signer_address == SIGNER
    assert snapshot.can_read is True
    assert snapshot.can_perp_trade is True
    assert snapshot.can_withdraw is False
    assert snapshot.checked_at_ms == NOW_MS

    with pytest.raises(OnboardingError) as exc_info:
        validate_agent_permissions(
            _agent(canWithdraw=True),
            SIGNER,
            config,
            checked_at_ms=NOW_MS,
        )
    assert exc_info.value.code is OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION


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


def _position(**changes: object) -> PositionDTO:
    values: dict[str, object] = {
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
    values.update(changes)
    return PositionDTO.model_validate(values)


class FakeClient:
    def __init__(self) -> None:
        self.agent_record = _agent()
        self.positions = [_position()]
        self.open_orders_result: list = []
        self.dual_side_position = False
        self.bound: tuple[EphemeralIdentityProvider, EphemeralKeyProvider] | None = None

    def bind(self, identities: EphemeralIdentityProvider, keys: EphemeralKeyProvider) -> None:
        assert identities.identity(EPHEMERAL_ACCOUNT_ID).signer == SIGNER
        assert keys.private_key(EPHEMERAL_ACCOUNT_ID) == KEY
        self.bound = (identities, keys)

    async def agent(self, account_id: str) -> AgentDTO:
        assert account_id == EPHEMERAL_ACCOUNT_ID
        return self.agent_record

    async def balance(self, account_id: str) -> list[BalanceDTO]:
        assert account_id == EPHEMERAL_ACCOUNT_ID
        return [_balance()]

    async def position_risk(self, account_id: str) -> list[PositionDTO]:
        assert account_id == EPHEMERAL_ACCOUNT_ID
        return self.positions

    async def open_orders(self, account_id: str) -> list:
        assert account_id == EPHEMERAL_ACCOUNT_ID
        return self.open_orders_result

    async def get_position_mode(self, account_id: str) -> bool:
        assert account_id == EPHEMERAL_ACCOUNT_ID
        return self.dual_side_position


class FakeStore:
    def __init__(self, conflict: str | None = None, account_id: str | None = None) -> None:
        self.conflict = conflict
        self.account_id = account_id
        self.payload: OnboardingCommit | None = None
        self.calls: list[str] = []

    async def find_identity_conflict(self, telegram_user_id: int, user_address: str) -> str | None:
        self.calls.append("conflict")
        return self.conflict

    async def commit_onboarding(self, payload: OnboardingCommit) -> None:
        self.calls.append("commit")
        self.payload = payload
        if self.account_id is not None and self.account_id != payload.account_id:
            raise AssertionError("store must receive the stable account id")


@pytest.fixture
def service_factory():
    def make(
        *,
        store: FakeStore | None = None,
        config: DirectOnboardingConfig | None = None,
        activation: Callable[[str], object] | None = None,
    ) -> tuple[DirectOnboardingService, FakeStore, FakeClient]:
        store = store or FakeStore()
        client = FakeClient()

        def factory(identities: EphemeralIdentityProvider, keys: EphemeralKeyProvider):
            client.bind(identities, keys)
            return client

        config = config or DirectOnboardingConfig(
            base_url=BASE,
            expected_egress_ips=("203.0.113.7",),
        )
        service = DirectOnboardingService(
            factory,
            store,
            config,
            activation=activation,
            clock_ms=lambda: NOW_MS,
        )
        return service, store, client

    return make


async def test_valid_preflight_commit_and_activation_are_ordered(service_factory):
    events: list[str] = []

    async def activate(account_id: str) -> None:
        events.append(f"activate:{account_id}")

    service, store, _ = service_factory(activation=activate)
    result = await service.onboard(_request())

    assert result.account_id == "tg-101"
    assert result.paused is True
    assert result.position_mode == "ONE_WAY"
    assert result.position_count == 0
    assert result.available_usdt == Decimal("1000")
    assert result.masked_signer_address != KEY
    assert store.calls == ["conflict", "commit"]
    assert store.payload is not None
    assert store.payload.allow_new_entries is False
    assert store.payload.signer_private_key == KEY
    assert events == ["activate:tg-101"]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("canRead", False, OnboardingErrorCode.AGENT_READ_PERMISSION),
        ("canRead", None, OnboardingErrorCode.AGENT_READ_PERMISSION),
        ("canPerpTrade", False, OnboardingErrorCode.AGENT_PERP_PERMISSION),
        ("canPerpTrade", None, OnboardingErrorCode.AGENT_PERP_PERMISSION),
        ("canWithdraw", True, OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION),
        ("canWithdraw", None, OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION),
        ("expired", NOW_MS + 1_000, OnboardingErrorCode.AGENT_EXPIRED),
        ("ipWhitelist", "198.51.100.0/24", OnboardingErrorCode.AGENT_IP_WHITELIST),
    ],
)
async def test_agent_policy_is_fail_closed(service_factory, field, value, expected):
    service, store, client = service_factory()
    client.agent_record = _agent(**{field: value})

    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())

    assert exc_info.value.code == expected
    assert store.payload is None


async def test_nonflat_or_open_orders_never_reach_store(service_factory):
    service, store, client = service_factory()
    client.positions = [_position(positionAmt="0.01")]

    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code == OnboardingErrorCode.ACCOUNT_NOT_FLAT

    client.positions = [_position()]
    client.open_orders_result = [object()]
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code == OnboardingErrorCode.OPEN_ORDERS_PRESENT
    assert store.payload is None


async def test_position_mode_policy_is_checked(service_factory):
    service, store, client = service_factory(
        config=DirectOnboardingConfig(
            base_url=BASE,
            expected_egress_ips=("203.0.113.7",),
            expected_position_mode="HEDGE",
        )
    )

    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())

    assert exc_info.value.code == OnboardingErrorCode.POSITION_MODE_MISMATCH
    assert store.payload is None
    client.dual_side_position = True
    result = await service.preflight(_request())
    assert result.position_mode == "HEDGE"


async def test_agent_signer_mismatch_is_rejected(service_factory):
    service, store, client = service_factory()
    client.agent_record = _agent(agentAddress="0x" + "11" * 20)

    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())

    assert exc_info.value.code == OnboardingErrorCode.AGENT_SIGNER_MISMATCH
    assert store.payload is None


async def test_ip_whitelist_accepts_list_and_cidr_coverage(service_factory):
    service, _, client = service_factory(
        config=DirectOnboardingConfig(
            base_url=BASE,
            expected_egress_ips=("203.0.113.7", "2001:db8::7"),
        )
    )
    client.agent_record = _agent(ipWhitelist=["203.0.113.0/24", "2001:db8::/64"])

    result = await service.preflight(_request())

    assert result.validation.ip_whitelist == "203.0.113.0/24 2001:db8::/64"


async def test_whitelist_override_is_allowed_only_on_exact_testnet(service_factory):
    service, _, client = service_factory(
        config=DirectOnboardingConfig(
            base_url=BASE,
            require_agent_ip_whitelist=False,
        )
    )
    client.agent_record = _agent(ipWhitelist=None)

    result = await service.preflight(_request())

    assert result.validation.ip_whitelist == ""


@pytest.mark.parametrize(
    "base_url",
    [
        "https://fapi.asterdex.com",
        "https://fapi.asterdex-testnet.com.evil.example",
        "http://fapi.asterdex-testnet.com",
    ],
)
async def test_whitelist_override_rejects_mainnet_lookalike_and_http(service_factory, base_url):
    service, store, _ = service_factory(
        config=DirectOnboardingConfig(
            base_url=base_url,
            require_agent_ip_whitelist=False,
        )
    )

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.TESTNET_REQUIRED
    assert store.calls == []


async def test_chain_override_cannot_disable_testnet_guard(service_factory):
    service, store, _ = service_factory(
        config=DirectOnboardingConfig(
            base_url=BASE,
            trading_chain_id=1666,
            require_agent_ip_whitelist=False,
        )
    )

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.TESTNET_REQUIRED
    assert store.calls == []


def test_direct_config_has_no_testnet_disable_switch():
    with pytest.raises(TypeError):
        DirectOnboardingConfig(testnet_only=False)  # type: ignore[call-arg]


async def test_bool_telegram_id_is_rejected(service_factory):
    service, store, _ = service_factory()

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request(telegram_user_id=True))

    assert exc_info.value.code == OnboardingErrorCode.INVALID_INPUT
    assert store.calls == []


def test_request_does_not_accept_user_label():
    with pytest.raises(TypeError):
        DirectOnboardingRequest(
            telegram_user_id=101,
            user_address=USER,
            declared_signer_address=SIGNER,
            signer_private_key=KEY,
            label="follower",
        )


async def test_multi_user_ids_produce_distinct_stable_account_ids(service_factory):
    service, store, _ = service_factory()

    first = await service.onboard(_request(telegram_user_id=101))
    second = await service.onboard(_request(telegram_user_id=202))

    assert first.account_id == "tg-101"
    assert second.account_id == "tg-202"
    assert store.payload is not None
    assert store.payload.account_id == "tg-202"
    assert store.payload.label == "tg-202"


async def test_activation_failure_happens_after_paused_commit(service_factory):
    async def fail_activation(account_id: str) -> None:
        raise RuntimeError("registry unavailable")

    service, store, _ = service_factory(activation=fail_activation)

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.ACTIVATION_FAILED
    assert store.payload is not None
    assert store.payload.account_id == "tg-101"
    assert store.payload.allow_new_entries is False


async def test_local_invalid_key_and_signer_do_not_construct_client(service_factory):
    service, store, client = service_factory()
    request = _request(signer_private_key="0x" + "55" * 32)

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(request)

    assert exc_info.value.code == OnboardingErrorCode.INVALID_INPUT
    assert store.calls == []
    assert client.bound is None


async def test_mainnet_guard_is_hard_reject(service_factory):
    service, store, _ = service_factory(
        config=DirectOnboardingConfig(base_url="https://fapi.asterdex.com")
    )

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.TESTNET_REQUIRED
    assert store.calls == []


async def test_lookalike_testnet_host_is_rejected(service_factory):
    service, store, _ = service_factory(
        config=DirectOnboardingConfig(base_url="https://fapi.asterdex-testnet.com.evil.example")
    )

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.TESTNET_REQUIRED
    assert store.calls == []


async def test_duplicate_owner_is_rejected_before_remote_preflight(service_factory):
    store = FakeStore(conflict="owner")
    service, store, client = service_factory(store=store)

    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.OWNER_CONFLICT
    assert store.calls == ["conflict"]
    assert client.bound is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-2015, OnboardingErrorCode.AGENT_NOT_FOUND),
        (-1000, OnboardingErrorCode.SIGNED_READ_FAILED),
    ],
)
async def test_agent_aster_errors_are_normalized_without_raw_messages(
    service_factory, code, expected
):
    service, store, client = service_factory()

    async def fail_agent(account_id: str):
        del account_id
        raise AsterError(code, f"secret-like {KEY}")

    client.agent = fail_agent
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is expected
    assert KEY not in str(exc_info.value)
    assert store.payload is None


async def test_agent_and_bundle_transport_errors_are_fail_closed(service_factory):
    service, _, client = service_factory()

    async def fail_agent(account_id: str):
        del account_id
        raise RuntimeError("transport")

    client.agent = fail_agent
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.SIGNED_READ_FAILED

    service, _, client = service_factory()

    async def fail_balance(account_id: str):
        del account_id
        raise AsterError(-1006, "transport")

    client.balance = fail_balance
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.SIGNED_READ_FAILED

    service, _, client = service_factory()

    async def fail_balance_generic(account_id: str):
        del account_id
        raise RuntimeError("transport")

    client.balance = fail_balance_generic
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.SIGNED_READ_FAILED


async def test_client_factory_failure_is_normalized(service_factory):
    store = FakeStore()

    def broken_factory(identities, keys):
        del identities, keys
        raise RuntimeError(f"do not expose {KEY}")

    service = DirectOnboardingService(
        broken_factory,
        store,
        DirectOnboardingConfig(
            base_url=BASE,
            expected_egress_ips=("203.0.113.7",),
        ),
        clock_ms=lambda: NOW_MS,
    )
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.SIGNED_READ_FAILED
    assert KEY not in str(exc_info.value)


async def test_conflict_store_failure_and_unknown_marker_fail_closed(service_factory):
    class BrokenStore(FakeStore):
        async def find_identity_conflict(self, telegram_user_id: int, user_address: str):
            del telegram_user_id, user_address
            raise RuntimeError("database unavailable")

    service, _, client = service_factory(store=BrokenStore())
    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())
    assert exc_info.value.code is OnboardingErrorCode.COMMIT_FAILED
    assert client.bound is None

    service, store, client = service_factory(store=FakeStore(conflict="future-marker"))
    with pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())
    assert exc_info.value.code is OnboardingErrorCode.USER_ADDRESS_CONFLICT
    assert client.bound is None
    assert store.calls == ["conflict"]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            DirectOnboardingConfig(
                base_url=BASE,
                expected_egress_ips=("203.0.113.7",),
                expiry_safety_margin_ms=-1,
            ),
            OnboardingErrorCode.INVALID_CONFIG,
        ),
        (
            DirectOnboardingConfig(
                base_url=BASE,
                expected_egress_ips=("203.0.113.7",),
                expected_position_mode="INVALID",
            ),
            OnboardingErrorCode.INVALID_CONFIG,
        ),
        (
            DirectOnboardingConfig(
                base_url=BASE,
                expected_egress_ips=("203.0.113.7",),
                master_user_address="not-an-address",
            ),
            OnboardingErrorCode.INVALID_CONFIG,
        ),
    ],
)
async def test_local_configuration_errors_are_typed(service_factory, config, expected):
    service, store, _ = service_factory(config=config)
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is expected
    assert store.calls == []


async def test_master_address_and_bad_request_shapes_are_rejected(service_factory):
    service, store, _ = service_factory(
        config=DirectOnboardingConfig(
            base_url=BASE,
            expected_egress_ips=("203.0.113.7",),
            master_user_address=USER,
        )
    )
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.MASTER_ADDRESS

    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(object())
    assert exc_info.value.code is OnboardingErrorCode.INVALID_INPUT
    assert store.calls == []


async def test_invalid_whitelist_text_and_agent_shapes_fail_closed(service_factory):
    service, _, client = service_factory()
    client.agent_record = _agent(ipWhitelist="not-an-ip-network")
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.AGENT_IP_WHITELIST

    service, _, client = service_factory()
    client.agent_record = _agent(agentAddress=None)
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.AGENT_INVALID

    service, _, client = service_factory()
    client.agent_record = _agent(agentAddress="not-an-address")
    with pytest.raises(OnboardingError) as exc_info:
        await service.preflight(_request())
    assert exc_info.value.code is OnboardingErrorCode.AGENT_INVALID


async def test_commit_failure_does_not_activate_or_log_secret(service_factory, caplog):
    class BrokenStore(FakeStore):
        async def commit_onboarding(self, payload: OnboardingCommit) -> None:
            self.calls.append("commit")
            raise RuntimeError(f"do not expose {KEY}")

    events: list[str] = []
    service, _, _ = service_factory(store=BrokenStore(), activation=events.append)
    with caplog.at_level(logging.INFO), pytest.raises(OnboardingError) as exc_info:
        await service.onboard(_request())

    assert exc_info.value.code == OnboardingErrorCode.COMMIT_FAILED
    assert events == []
    assert KEY not in str(exc_info.value)
    assert KEY not in repr(_request())
    assert KEY not in caplog.text


def test_ephemeral_key_provider_repr_is_redacted():
    provider = EphemeralKeyProvider(EPHEMERAL_ACCOUNT_ID, KEY)
    assert KEY not in repr(provider)
    assert provider.private_key(EPHEMERAL_ACCOUNT_ID) == KEY
    assert provider.main_private_key(EPHEMERAL_ACCOUNT_ID) is None
