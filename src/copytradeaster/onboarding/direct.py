"""Direct V3 onboarding application service.

The service is deliberately independent of Telegram and concrete persistence.
It performs all local and signed remote checks before handing one secret-
containing payload to an atomic store protocol.  Runtime activation is called
only after that commit and accounts are always persisted paused.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from inspect import isawaitable
from typing import Any
from urllib.parse import urlsplit

from eth_account import Account
from eth_utils import is_address, to_checksum_address

from copytradeaster.aster.models import AgentDTO
from copytradeaster.aster.network_profile import is_official_combination
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.logging_utils import get_logger
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    AgentValidationSnapshot,
    DirectOnboardingConfig,
    DirectOnboardingRequest,
    DirectOnboardingResult,
    DirectPreflightResult,
    OnboardingCommit,
    SignedReadBundle,
    mask_address,
    stable_account_id,
)
from copytradeaster.onboarding.providers import (
    ActivationCallback,
    EphemeralIdentityProvider,
    EphemeralKeyProvider,
    OnboardingClientFactory,
    OnboardingStore,
    SignedReadClient,
    maybe_await,
)

log = get_logger(__name__)

EPHEMERAL_ACCOUNT_ID = "__onboarding_preflight__"


class _PreparedOnboarding:
    __slots__ = (
        "account_id",
        "telegram_user_id",
        "user_address",
        "signer_address",
        "signer_private_key",
        "validation",
        "read_bundle",
    )

    def __init__(
        self,
        *,
        account_id: str,
        telegram_user_id: int,
        user_address: str,
        signer_address: str,
        signer_private_key: str,
        validation: AgentValidationSnapshot,
        read_bundle: SignedReadBundle,
    ) -> None:
        self.account_id = account_id
        self.telegram_user_id = telegram_user_id
        self.user_address = user_address
        self.signer_address = signer_address
        self.signer_private_key = signer_private_key
        self.validation = validation
        self.read_bundle = read_bundle

    def public_preflight(self) -> DirectPreflightResult:
        return DirectPreflightResult(
            masked_user_address=mask_address(self.user_address),
            masked_signer_address=mask_address(self.signer_address),
            validation=self.validation,
            read_bundle=self.read_bundle,
        )


class DirectOnboardingService:
    """Validate and atomically commit one direct Aster follower identity."""

    def __init__(
        self,
        client_factory: OnboardingClientFactory,
        store: OnboardingStore,
        config: DirectOnboardingConfig | None = None,
        activation: ActivationCallback | None = None,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._store = store
        self._config = config or DirectOnboardingConfig()
        self._activation = activation
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def preflight(self, request: DirectOnboardingRequest) -> DirectPreflightResult:
        """Run local and remote checks without persistence or activation."""
        prepared = await self._prepare(request)
        return prepared.public_preflight()

    async def onboard(self, request: DirectOnboardingRequest) -> DirectOnboardingResult:
        """Preflight, atomic commit, then paused runtime activation."""
        local = self._validate_local(request)
        await self._check_conflict(local["telegram_user_id"], local["user_address"])
        prepared = await self._prepare_validated(local)

        payload = OnboardingCommit(
            account_id=prepared.account_id,
            telegram_user_id=prepared.telegram_user_id,
            user_address=prepared.user_address,
            signer_address=prepared.signer_address,
            signer_private_key=prepared.signer_private_key,
            validation=prepared.validation,
            read_bundle=prepared.read_bundle,
            allow_new_entries=False,
        )
        try:
            await self._store.commit_onboarding(payload)
        except Exception:
            log.error("direct onboarding commit failed")
            raise OnboardingError(OnboardingErrorCode.COMMIT_FAILED) from None
        account_id = prepared.account_id

        if self._activation is not None:
            try:
                activated = self._activation(account_id)
                if isawaitable(activated):
                    await activated
            except Exception:
                # The atomic row remains paused and can be activated on retry
                # or during boot; never roll back a successful commit here.
                log.error("direct onboarding runtime activation failed")
                raise OnboardingError(OnboardingErrorCode.ACTIVATION_FAILED) from None

        return DirectOnboardingResult(
            account_id=account_id,
            masked_user_address=mask_address(prepared.user_address),
            masked_signer_address=mask_address(prepared.signer_address),
            position_mode=prepared.read_bundle.position_mode,
            balance_assets=tuple(balance.asset for balance in prepared.read_bundle.balances),
            position_count=sum(
                position.position_amt != Decimal(0) for position in prepared.read_bundle.positions
            ),
            available_usdt=prepared.read_bundle.available_usdt,
            paused=True,
        )

    async def _prepare(self, request: DirectOnboardingRequest) -> _PreparedOnboarding:
        local = self._validate_local(request)
        return await self._prepare_validated(local)

    async def _prepare_validated(self, local: dict[str, Any]) -> _PreparedOnboarding:
        identity = TradingIdentity(
            account_id=EPHEMERAL_ACCOUNT_ID,
            user=local["user_address"],
            signer=local["signer_address"],
            chain_id=self._config.trading_chain_id,
        )
        identity_provider = EphemeralIdentityProvider(EPHEMERAL_ACCOUNT_ID, identity)
        key_provider = EphemeralKeyProvider(EPHEMERAL_ACCOUNT_ID, local["signer_private_key"])
        try:
            client_result = self._client_factory(identity_provider, key_provider)
            client = await maybe_await(client_result)
        except Exception:
            log.error("direct onboarding client construction failed")
            raise OnboardingError(OnboardingErrorCode.SIGNED_READ_FAILED) from None

        agent = await self._read_agent(client)
        validation = validate_agent_permissions(
            agent,
            local["signer_address"],
            self._config,
            checked_at_ms=self._clock_ms(),
        )
        bundle = await self._read_bundle(client)
        self._validate_bundle(bundle)

        return _PreparedOnboarding(
            account_id=local["account_id"],
            telegram_user_id=local["telegram_user_id"],
            user_address=local["user_address"],
            signer_address=local["signer_address"],
            signer_private_key=local["signer_private_key"],
            validation=validation,
            read_bundle=bundle,
        )

    async def _check_conflict(self, telegram_user_id: int, user_address: str) -> None:
        try:
            conflict = await self._store.find_identity_conflict(telegram_user_id, user_address)
        except Exception:
            log.error("direct onboarding conflict check failed")
            raise OnboardingError(OnboardingErrorCode.COMMIT_FAILED) from None
        if conflict == "owner":
            raise OnboardingError(OnboardingErrorCode.OWNER_CONFLICT)
        if conflict in {"user", "address", "master"}:
            raise OnboardingError(
                OnboardingErrorCode.MASTER_ADDRESS
                if conflict == "master"
                else OnboardingErrorCode.USER_ADDRESS_CONFLICT
            )
        if conflict:
            # The store may return an implementation-specific conflict marker;
            # do not expose it to a user or treat it as permission to proceed.
            raise OnboardingError(OnboardingErrorCode.USER_ADDRESS_CONFLICT)

    def _validate_local(self, request: DirectOnboardingRequest) -> dict[str, Any]:
        self._validate_testnet_url()
        if not isinstance(request, DirectOnboardingRequest):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        if (
            isinstance(request.telegram_user_id, bool)
            or not isinstance(request.telegram_user_id, int)
            or request.telegram_user_id <= 0
        ):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)

        user_address = _normalize_address(request.user_address)
        declared_signer = _normalize_address(request.declared_signer_address)
        private_key = (
            request.signer_private_key.strip()
            if isinstance(request.signer_private_key, str)
            else ""
        )
        if not private_key or len(private_key) > 132:
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        try:
            derived_signer = Account.from_key(private_key).address
        except Exception:
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT) from None
        if derived_signer.lower() != declared_signer.lower():
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)

        master = self._config.master_user_address
        if master:
            try:
                normalized_master = to_checksum_address(master.strip())
            except Exception:
                raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG) from None
            if user_address.lower() == normalized_master.lower():
                raise OnboardingError(OnboardingErrorCode.MASTER_ADDRESS)

        try:
            account_id = stable_account_id(request.telegram_user_id)
        except ValueError:
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT) from None
        return {
            "account_id": account_id,
            "telegram_user_id": request.telegram_user_id,
            "user_address": user_address,
            "signer_address": declared_signer,
            "signer_private_key": private_key,
        }

    def _validate_testnet_url(self) -> None:
        parsed = urlsplit(self._config.base_url)
        if parsed.scheme != "https" or not is_official_combination(
            self._config.base_url, self._config.trading_chain_id
        ):
            raise OnboardingError(OnboardingErrorCode.TESTNET_REQUIRED)
        if self._config.expiry_safety_margin_ms < 0:
            raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG)
        if self._config.expected_position_mode not in {None, "ONE_WAY", "HEDGE"}:
            raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG)

    async def _read_agent(self, client: SignedReadClient) -> AgentDTO:
        try:
            return await client.agent(EPHEMERAL_ACCOUNT_ID)
        except AsterError as exc:
            log.warning("direct onboarding agent read failed code=%s", exc.code)
            if exc.code in {-2015, -2016}:
                raise OnboardingError(
                    OnboardingErrorCode.AGENT_NOT_FOUND, exchange_code=exc.code
                ) from None
            raise OnboardingError(
                OnboardingErrorCode.SIGNED_READ_FAILED, exchange_code=exc.code
            ) from None
        except Exception:
            log.warning("direct onboarding agent read failed type=unknown")
            raise OnboardingError(OnboardingErrorCode.SIGNED_READ_FAILED) from None

    async def _read_bundle(self, client: SignedReadClient) -> SignedReadBundle:
        try:
            balances = tuple(await client.balance(EPHEMERAL_ACCOUNT_ID))
            positions = tuple(await client.position_risk(EPHEMERAL_ACCOUNT_ID))
            open_orders = tuple(await client.open_orders(EPHEMERAL_ACCOUNT_ID))
            dual_side_position = await client.get_position_mode(EPHEMERAL_ACCOUNT_ID)
            return SignedReadBundle(
                balances=balances,
                positions=positions,
                open_orders=open_orders,
                dual_side_position=bool(dual_side_position),
            )
        except AsterError as exc:
            log.warning("direct onboarding signed read failed code=%s", exc.code)
            raise OnboardingError(
                OnboardingErrorCode.SIGNED_READ_FAILED, exchange_code=exc.code
            ) from None
        except Exception:
            log.warning("direct onboarding signed read failed type=unknown")
            raise OnboardingError(OnboardingErrorCode.SIGNED_READ_FAILED) from None

    def _validate_bundle(self, bundle: SignedReadBundle) -> None:
        if not bundle.is_flat:
            raise OnboardingError(OnboardingErrorCode.ACCOUNT_NOT_FLAT)
        if bundle.open_orders:
            raise OnboardingError(OnboardingErrorCode.OPEN_ORDERS_PRESENT)
        expected = self._config.expected_position_mode
        if expected is not None and bundle.position_mode != expected:
            raise OnboardingError(OnboardingErrorCode.POSITION_MODE_MISMATCH)


def _normalize_address(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or not is_address(value.strip()):
        raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
    try:
        return to_checksum_address(value.strip())
    except Exception:
        raise OnboardingError(OnboardingErrorCode.INVALID_INPUT) from None


def _ip_whitelist_text(value: str | list[str] | None) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _parse_networks(value: str | Iterable[str] | None) -> list[ipaddress._BaseNetwork]:
    if value is None:
        return []
    tokens = value.split() if isinstance(value, str) else [str(item).strip() for item in value]
    out: list[ipaddress._BaseNetwork] = []
    for token in tokens:
        if not token:
            continue
        try:
            out.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            return []
    return out


def _network_covers(candidate: ipaddress._BaseNetwork, expected: ipaddress._BaseNetwork) -> bool:
    return candidate.version == expected.version and expected.subnet_of(candidate)


def validate_agent_permissions(
    agent: AgentDTO,
    signer_address: str,
    config: DirectOnboardingConfig,
    *,
    checked_at_ms: int,
) -> AgentValidationSnapshot:
    """Validate one signed Agent response and return its safe persisted snapshot.

    Direct onboarding and later resume/status refreshes must enforce exactly
    the same signer, permission, expiry, and whitelist policy.  Keeping this
    pure check public avoids a second, subtly different validator in the
    Stage 2 runtime adapter.
    """
    if not agent.agent_address or not agent.expired:
        raise OnboardingError(OnboardingErrorCode.AGENT_INVALID)
    try:
        agent_address = _normalize_address(agent.agent_address)
        expected_signer = _normalize_address(signer_address)
    except OnboardingError:
        raise OnboardingError(OnboardingErrorCode.AGENT_INVALID) from None
    if agent_address.lower() != expected_signer.lower():
        raise OnboardingError(OnboardingErrorCode.AGENT_SIGNER_MISMATCH)
    if agent.can_read is not True:
        raise OnboardingError(OnboardingErrorCode.AGENT_READ_PERMISSION)
    if agent.can_perp_trade is not True:
        raise OnboardingError(OnboardingErrorCode.AGENT_PERP_PERMISSION)
    if agent.can_withdraw is not False:
        raise OnboardingError(OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION)
    if int(agent.expired) <= checked_at_ms + config.expiry_safety_margin_ms:
        raise OnboardingError(OnboardingErrorCode.AGENT_EXPIRED)

    if config.require_agent_ip_whitelist:
        agent_networks = _parse_networks(agent.ip_whitelist)
        expected_networks = _parse_networks(config.expected_egress_ips)
        if not agent_networks or not expected_networks:
            raise OnboardingError(OnboardingErrorCode.AGENT_IP_WHITELIST)
        if not all(
            any(_network_covers(candidate, expected) for candidate in agent_networks)
            for expected in expected_networks
        ):
            raise OnboardingError(OnboardingErrorCode.AGENT_IP_WHITELIST)

    return AgentValidationSnapshot(
        signer_address=expected_signer,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=int(agent.expired),
        ip_whitelist=_ip_whitelist_text(agent.ip_whitelist),
        checked_at_ms=int(checked_at_ms),
    )


__all__ = [
    "DirectOnboardingService",
    "EPHEMERAL_ACCOUNT_ID",
    "validate_agent_permissions",
]
