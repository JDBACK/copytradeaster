"""Builder (Aster Code) onboarding application service — Stage 3 part 1.

The backend generates and stores the per-user signer key; the user's main
wallet only signs the ApproveAgent registration payload (combined ApproveBuilder
fields included).  The flow mirrors `onboarding/direct.py`: local validation,
typed failures, signed remote verification, atomic paused commit, and only
then runtime activation.  Pending approvals live in memory with a short TTL;
the generated signer key never crosses the application boundary and is never
logged.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from inspect import isawaitable
from urllib.parse import urlsplit

from eth_account import Account

from copytradeaster.aster.management_api import ManagementError, post_signed_registration
from copytradeaster.aster.signing import (
    eip712_json_payload,
    next_nonce,
    recover_registration,
)
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.logging_utils import get_logger
from copytradeaster.onboarding.direct import _normalize_address, validate_agent_permissions
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    AgentValidationSnapshot,
    BuilderOnboardingConfig,
    BuilderOnboardingResult,
    BuilderPrepareRequest,
    BuilderPrepareResult,
    BuilderSubmitRequest,
    DirectOnboardingConfig,
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

EPHEMERAL_PREFIX = "__builder_preflight_"


def _default_signer() -> tuple[str, str]:
    account = Account.create()
    key = account.key.hex()
    return account.address, (key if key.startswith("0x") else f"0x{key}")


class _PendingApproval:
    """One in-memory prepare→submit attempt.

    Holds the generated signer key only for the TTL window.  The custom repr
    keeps failure dumps (and therefore logs) free of key material.
    """

    __slots__ = (
        "pending_id",
        "telegram_user_id",
        "user_address",
        "signer_address",
        "signer_private_key",
        "params",
        "agent_expires_at_ms",
        "deadline_ms",
    )

    def __init__(
        self,
        *,
        pending_id: str,
        telegram_user_id: int,
        user_address: str,
        signer_address: str,
        signer_private_key: str,
        params: dict,
        agent_expires_at_ms: int,
        deadline_ms: int,
    ) -> None:
        self.pending_id = pending_id
        self.telegram_user_id = telegram_user_id
        self.user_address = user_address
        self.signer_address = signer_address
        self.signer_private_key = signer_private_key
        self.params = params
        self.agent_expires_at_ms = agent_expires_at_ms
        self.deadline_ms = deadline_ms

    def __repr__(self) -> str:
        return (
            "_PendingApproval("
            f"pending_id={self.pending_id!r}, "
            f"telegram_user_id={self.telegram_user_id!r}, "
            f"user_address={mask_address(self.user_address)!r}, "
            f"signer_address={mask_address(self.signer_address)!r}, "
            "signer_private_key='<redacted>')"
        )


class BuilderOnboardingService:
    """Prepare, verify, and commit one Builder-authorised follower identity."""

    def __init__(
        self,
        http,
        limiter,
        client_factory: OnboardingClientFactory,
        store: OnboardingStore,
        config: BuilderOnboardingConfig | None = None,
        activation: ActivationCallback | None = None,
        *,
        clock_ms: Callable[[], int] | None = None,
        signer_factory: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self._http = http
        self._limiter = limiter
        self._client_factory = client_factory
        self._store = store
        self._config = config or BuilderOnboardingConfig()
        self._activation = activation
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._signer_factory = signer_factory or _default_signer
        self._pending: dict[str, _PendingApproval] = {}
        self._user_pending: dict[int, str] = {}
        self._lock = asyncio.Lock()

    # --- prepare --------------------------------------------------------------

    async def prepare(self, request: BuilderPrepareRequest) -> BuilderPrepareResult:
        """Validate, generate the signer, and return the wallet signing payload."""
        self._validate_config()
        if not isinstance(request, BuilderPrepareRequest):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        if (
            isinstance(request.telegram_user_id, bool)
            or not isinstance(request.telegram_user_id, int)
            or request.telegram_user_id <= 0
        ):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        user_address = _normalize_address(request.user_address)
        master = self._config.master_user_address
        if master:
            from eth_utils import to_checksum_address

            try:
                normalized_master = to_checksum_address(master.strip())
            except Exception:
                raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG) from None
            if user_address.lower() == normalized_master.lower():
                raise OnboardingError(OnboardingErrorCode.MASTER_ADDRESS)
        await self._check_conflict(request.telegram_user_id, user_address)

        signer_address, signer_private_key = self._signer_factory()
        now_ms = self._clock_ms()
        agent_expires_at_ms = now_ms + self._config.agent_expiry_days * 86_400_000
        pending_id = secrets.token_hex(16)
        params: dict = {
            "agentName": self._config.agent_name,
            "agentAddress": signer_address,
            "expired": agent_expires_at_ms,
            "canSpotTrade": False,
            "canPerpTrade": True,
            "canWithdraw": False,
            "builder": self._config.builder_address,
            "maxFeeRate": self._config.max_fee_rate,
            "builderName": self._config.builder_name,
            "asterChain": "Testnet",
            "user": user_address,
            "nonce": next_nonce(),
        }
        deadline_ms = now_ms + self._config.approval_ttl_s * 1000
        pending = _PendingApproval(
            pending_id=pending_id,
            telegram_user_id=request.telegram_user_id,
            user_address=user_address,
            signer_address=signer_address,
            signer_private_key=signer_private_key,
            params=params,
            agent_expires_at_ms=agent_expires_at_ms,
            deadline_ms=deadline_ms,
        )
        async with self._lock:
            self._drop_user_pending(request.telegram_user_id)
            self._pending[pending_id] = pending
            self._user_pending[request.telegram_user_id] = pending_id
        log.info(
            "builder onboarding prepared pending_id=%s approval_ttl_s=%s",
            pending_id,
            self._config.approval_ttl_s,
        )
        return BuilderPrepareResult(
            pending_id=pending_id,
            masked_user_address=mask_address(user_address),
            masked_signer_address=mask_address(signer_address),
            builder_masked=mask_address(self._config.builder_address),
            builder_name=self._config.builder_name,
            max_fee_rate=self._config.max_fee_rate,
            signature_plan="combined_approve_agent",
            can_perp_trade=True,
            can_withdraw=False,
            agent_expires_at_ms=agent_expires_at_ms,
            approval_expires_at_ms=deadline_ms,
            eip712=eip712_json_payload(params, "ApproveAgent", self._config.registration_chain_id),
        )

    # --- submit ---------------------------------------------------------------

    async def submit(self, request: BuilderSubmitRequest) -> BuilderOnboardingResult:
        """Verify the wallet signature, submit to Aster, preflight, commit paused."""
        self._validate_config()
        if not isinstance(request, BuilderSubmitRequest):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        signature = request.signature.strip() if isinstance(request.signature, str) else ""
        if not signature or len(signature) > 200 or not signature.removeprefix("0x").isalnum():
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)

        async with self._lock:
            pending = self._pending.pop(request.pending_id, None)
            if pending is None or pending.telegram_user_id != request.telegram_user_id:
                raise OnboardingError(OnboardingErrorCode.APPROVAL_UNKNOWN)
            self._user_pending.pop(pending.telegram_user_id, None)
        if self._clock_ms() > pending.deadline_ms:
            raise OnboardingError(OnboardingErrorCode.APPROVAL_EXPIRED)

        try:
            recovered = recover_registration(
                pending.params,
                signature,
                "ApproveAgent",
                chain_id=self._config.registration_chain_id,
            )
        except Exception:
            raise OnboardingError(OnboardingErrorCode.SIGNATURE_MISMATCH) from None
        if recovered.lower() != pending.user_address.lower():
            raise OnboardingError(OnboardingErrorCode.SIGNATURE_MISMATCH)

        submitted: dict = dict(pending.params)
        submitted["signature"] = signature.removeprefix("0x")
        submitted["signatureChainId"] = str(self._config.registration_chain_id)
        try:
            await post_signed_registration(
                self._http,
                self._config.base_url,
                self._limiter,
                "/fapi/v3/approveAgent",
                submitted,
            )
        except ManagementError as exc:
            log.warning(
                "builder approveAgent submit failed code=%s msg=%s",
                exc.code,
                str(exc)[:160],
            )
            exchange_code = exc.code if isinstance(exc.code, int) else None
            raise OnboardingError(
                OnboardingErrorCode.AGENT_SUBMIT_FAILED, exchange_code=exchange_code
            ) from None

        client = await self._open_verify_client(pending)
        validation = await self._verify_agent(client, pending)
        bundle = await self._read_bundle(client, pending)
        self._validate_bundle(bundle)

        commit = OnboardingCommit(
            account_id=stable_account_id(pending.telegram_user_id),
            telegram_user_id=pending.telegram_user_id,
            user_address=pending.user_address,
            signer_address=pending.signer_address,
            signer_private_key=pending.signer_private_key,
            validation=validation,
            read_bundle=bundle,
            allow_new_entries=False,
        )
        try:
            await self._store.commit_onboarding(commit)
        except Exception:
            log.error("builder onboarding commit failed")
            raise OnboardingError(OnboardingErrorCode.COMMIT_FAILED) from None
        account_id = commit.account_id

        if self._activation is not None:
            try:
                activated = self._activation(account_id)
                if isawaitable(activated):
                    await activated
            except Exception:
                # The atomic row remains paused and can be activated on retry
                # or during boot; never roll back a successful commit here.
                log.error("builder onboarding runtime activation failed")
                raise OnboardingError(OnboardingErrorCode.ACTIVATION_FAILED) from None

        log.info("builder onboarding committed account_id=%s paused=True", account_id)
        return BuilderOnboardingResult(
            account_id=account_id,
            masked_user_address=mask_address(pending.user_address),
            masked_signer_address=mask_address(pending.signer_address),
            builder_name=self._config.builder_name,
            max_fee_rate=self._config.max_fee_rate,
            position_mode=bundle.position_mode,
            available_usdt=bundle.available_usdt,
            agent_expires_at_ms=pending.agent_expires_at_ms,
            paused=True,
        )

    # --- internals --------------------------------------------------------------

    def _validate_config(self) -> None:
        parsed = urlsplit(self._config.base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host != "fapi.asterdex-testnet.com":
            raise OnboardingError(OnboardingErrorCode.TESTNET_REQUIRED)
        if self._config.trading_chain_id != 714:
            raise OnboardingError(OnboardingErrorCode.TESTNET_REQUIRED)
        if not self._config.builder_address:
            raise OnboardingError(OnboardingErrorCode.BUILDER_CONFIG_MISSING)
        if self._config.agent_expiry_days <= 0 or self._config.approval_ttl_s <= 0:
            raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG)
        if self._config.expiry_safety_margin_ms < 0:
            raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG)
        if self._config.expected_position_mode not in {None, "ONE_WAY", "HEDGE"}:
            raise OnboardingError(OnboardingErrorCode.INVALID_CONFIG)

    async def _check_conflict(self, telegram_user_id: int, user_address: str) -> None:
        try:
            conflict = await self._store.find_identity_conflict(telegram_user_id, user_address)
        except Exception:
            log.error("builder onboarding conflict check failed")
            raise OnboardingError(OnboardingErrorCode.COMMIT_FAILED) from None
        if conflict == "owner":
            # Already-connected users must disconnect in the bot first; the
            # full replace/revoke flow is a later part of Stage 3.
            raise OnboardingError(OnboardingErrorCode.OWNER_CONFLICT)
        if conflict in {"user", "address", "master"}:
            raise OnboardingError(
                OnboardingErrorCode.MASTER_ADDRESS
                if conflict == "master"
                else OnboardingErrorCode.USER_ADDRESS_CONFLICT
            )
        if conflict:
            raise OnboardingError(OnboardingErrorCode.USER_ADDRESS_CONFLICT)

    def _permission_config(self) -> DirectOnboardingConfig:
        return DirectOnboardingConfig(
            base_url=self._config.base_url,
            trading_chain_id=self._config.trading_chain_id,
            expected_egress_ips=self._config.expected_egress_ips,
            require_agent_ip_whitelist=self._config.require_agent_ip_whitelist,
            expiry_safety_margin_ms=self._config.expiry_safety_margin_ms,
            expected_position_mode=self._config.expected_position_mode,
            master_user_address=self._config.master_user_address,
        )

    def _ephemeral_id(self, pending: _PendingApproval) -> str:
        return f"{EPHEMERAL_PREFIX}{pending.pending_id}__"

    async def _open_verify_client(self, pending: _PendingApproval) -> SignedReadClient:
        ephemeral_id = self._ephemeral_id(pending)
        identity = TradingIdentity(
            account_id=ephemeral_id,
            user=pending.user_address,
            signer=pending.signer_address,
            chain_id=self._config.trading_chain_id,
        )
        identity_provider = EphemeralIdentityProvider(ephemeral_id, identity)
        key_provider = EphemeralKeyProvider(ephemeral_id, pending.signer_private_key)
        try:
            client = await maybe_await(self._client_factory(identity_provider, key_provider))
        except Exception:
            log.warning("builder onboarding client construction failed")
            raise OnboardingError(OnboardingErrorCode.SIGNED_READ_FAILED) from None
        return client

    async def _verify_agent(
        self, client: SignedReadClient, pending: _PendingApproval
    ) -> AgentValidationSnapshot:
        ephemeral_id = self._ephemeral_id(pending)
        try:
            agent = await client.agent(ephemeral_id)
        except OnboardingError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            log.warning("builder agent verify failed class=%s", type(exc).__name__)
            if code in {-2015, -2016}:
                raise OnboardingError(
                    OnboardingErrorCode.AGENT_NOT_CONFIRMED, exchange_code=code
                ) from None
            raise OnboardingError(
                OnboardingErrorCode.SIGNED_READ_FAILED,
                exchange_code=code if isinstance(code, int) else None,
            ) from None
        return validate_agent_permissions(
            agent,
            pending.signer_address,
            self._permission_config(),
            checked_at_ms=self._clock_ms(),
        )

    async def _read_bundle(
        self, client: SignedReadClient, pending: _PendingApproval
    ) -> SignedReadBundle:
        ephemeral_id = self._ephemeral_id(pending)
        try:
            balances = tuple(await client.balance(ephemeral_id))
            positions = tuple(await client.position_risk(ephemeral_id))
            open_orders = tuple(await client.open_orders(ephemeral_id))
            dual_side_position = await client.get_position_mode(ephemeral_id)
            return SignedReadBundle(
                balances=balances,
                positions=positions,
                open_orders=open_orders,
                dual_side_position=bool(dual_side_position),
            )
        except OnboardingError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            log.warning("builder signed read failed class=%s", type(exc).__name__)
            raise OnboardingError(
                OnboardingErrorCode.SIGNED_READ_FAILED,
                exchange_code=code if isinstance(code, int) else None,
            ) from None

    def _validate_bundle(self, bundle: SignedReadBundle) -> None:
        if not bundle.is_flat:
            raise OnboardingError(OnboardingErrorCode.ACCOUNT_NOT_FLAT)
        if bundle.open_orders:
            raise OnboardingError(OnboardingErrorCode.OPEN_ORDERS_PRESENT)
        expected = self._config.expected_position_mode
        if expected is not None and bundle.position_mode != expected:
            raise OnboardingError(OnboardingErrorCode.POSITION_MODE_MISMATCH)

    def _drop_user_pending(self, telegram_user_id: int) -> None:
        previous = self._user_pending.pop(telegram_user_id, None)
        if previous is not None:
            self._pending.pop(previous, None)


__all__ = ["BuilderOnboardingService", "EPHEMERAL_PREFIX"]
