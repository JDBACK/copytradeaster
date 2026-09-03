"""Concrete Stage 2 adapters over the existing application composition.

The transport-neutral application service owns policy and authorization.  This
module is the narrow composition seam that gives it real SQLite, Aster V3 and
Coordinator operations without teaching the service about ``App`` internals.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from copytradeaster.app import App
from copytradeaster.application.control import EmergencyController, EmergencyReport
from copytradeaster.application.models import ServiceUnavailableError
from copytradeaster.application.service import (
    ActivityKindFilter,
    OnboardingPort,
    Stage2AccountLifecycle,
    Stage2RuntimeAdapter,
    UserApplicationService,
)
from copytradeaster.aster.models import AgentDTO, BuilderDTO
from copytradeaster.aster.trading_api import UNKNOWN_CODES, AsterClient, AsterError
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import AccountState
from copytradeaster.logging_utils import get_logger
from copytradeaster.onboarding.builder import BuilderOnboardingService
from copytradeaster.onboarding.direct import DirectOnboardingService, validate_agent_permissions
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderOnboardingResult,
    BuilderPrepareRequest,
    BuilderPrepareResult,
    BuilderReauthorizeResult,
    BuilderSubmitRequest,
    DirectOnboardingConfig,
    DirectOnboardingRequest,
    DirectOnboardingResult,
    DirectPreflightResult,
    OnboardingCommit,
    ReauthorizeContext,
    stable_account_id,
)
from copytradeaster.onboarding.providers import (
    EphemeralIdentityProvider,
    EphemeralKeyProvider,
    OnboardingStore,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AgentValidation,
    BuilderOnboardingAttempt,
    BuilderOnboardingAttemptsRepo,
    TelegramUserRepo,
    _conn_execute,
    _conn_fetchone,
)
from copytradeaster.trading.leverage import LeverageError

log = get_logger(__name__)


async def _prepare_follower_leverage(app: App, account_id: str) -> None:
    """Provision leverage for a published follower before entries are allowed.

    Runs in the adapter seam - never inside the transport-neutral onboarding
    service. REAL accounts only; SHADOW runtimes have no exchange leverage.
    A partial failure raises LeverageError with safe categories only: the
    onboarding service maps it to ACTIVATION_FAILED (the durable row stays
    paused), and resume rolls the entry gate back to paused.
    """
    manager = getattr(app, "leverage", None)
    if manager is None:
        return
    runtime = app.follower_registry.get(account_id)
    if runtime is None or getattr(runtime, "execution_mode", "REAL") != "REAL":
        return
    report = await manager.prepare_account(account_id, tuple(app.cfg.symbol_whitelist))
    if report.failed:
        details = ", ".join(
            f"{symbol}:{category}" for symbol, category in sorted(report.failed.items())
        )
        raise LeverageError(f"leverage prepare failed: {details}")


class SqliteOnboardingStore(OnboardingStore):
    """Persist direct onboarding through the encrypted credentials boundary."""

    def __init__(
        self,
        db: Database,
        credentials: SqliteCredentials,
        users: TelegramUserRepo | None = None,
        *,
        permission_override: bool = False,
    ) -> None:
        self._db = db
        self._credentials = credentials
        self._users = users or TelegramUserRepo(db)
        self._permission_override = permission_override

    @property
    def permission_override(self) -> bool:
        return self._permission_override

    async def find_identity_conflict(self, telegram_user_id: int, user_address: str) -> str | None:
        owner = await self._users.get(telegram_user_id)
        if owner is None or str(owner["role"]) not in {"USER", "ADMIN"}:
            return "owner"
        expected_account_id = stable_account_id(telegram_user_id)
        if owner["account_id"] is not None:
            if str(owner["account_id"]) != expected_account_id:
                return "owner"
            account = await self._db.fetchone(
                "SELECT enabled FROM accounts WHERE id=?", (owner["account_id"],)
            )
            if account is not None and int(account["enabled"]):
                return "owner"

        row = await self._db.fetchone(
            "SELECT id, role, enabled FROM accounts WHERE lower(user_address)=lower(?)",
            (user_address,),
        )
        if row is None:
            return None
        if str(row["role"]) == "master":
            return "master"
        if int(row["enabled"]):
            return "address"
        # A disabled account is reconnectable only when the pre-allowlisted
        # owner and stable account ID prove that this is the same slot.
        linked_owner = await self._users.by_account(str(row["id"]))
        if (
            str(row["id"]) == expected_account_id
            and linked_owner is not None
            and int(linked_owner["telegram_user_id"]) == int(telegram_user_id)
            and str(linked_owner["role"]) in {"USER", "ADMIN"}
        ):
            return None
        return "address"

    async def owner_for(self, telegram_user_id: int) -> Any | None:
        """Return the pre-allowlisted row used by onboarding/replacement."""
        return await self._users.get(telegram_user_id)

    async def identity_for_recovery(
        self, account_id: str, telegram_user_id: int
    ) -> tuple[str, str] | None:
        """Return (user_address, signer_address) when this user durably owns
        the account; used to re-publish a committed-but-unpublished Builder
        runtime.  Returns None when ownership or durable state cannot be
        proven."""
        owner = await self._users.get(telegram_user_id)
        if owner is None or str(owner["role"]) not in {"USER", "ADMIN"}:
            return None
        if str(owner["account_id"] or "") != account_id:
            return None
        account = await self._db.fetchone(
            "SELECT user_address, enabled FROM accounts WHERE id=?", (account_id,)
        )
        if account is None or not int(account["enabled"]):
            return None
        validation = await self._db.fetchone(
            "SELECT signer_address FROM agent_validations WHERE account_id=?", (account_id,)
        )
        if validation is None:
            return None
        return str(account["user_address"]), str(validation["signer_address"])

    def validation_record(self, payload: OnboardingCommit | Any) -> AgentValidation:
        validation = payload.validation
        checked_at = datetime.fromtimestamp(
            validation.checked_at_ms / 1000,
            tz=UTC,
        ).isoformat()
        return AgentValidation(
            signer_address=validation.signer_address,
            can_read=validation.can_read,
            can_perp_trade=validation.can_perp_trade,
            can_withdraw=validation.can_withdraw,
            expired_ms=validation.expired_ms,
            ip_whitelist=validation.ip_whitelist,
            status="VALID_OVERRIDE" if self._permission_override else "VALID",
            checked_at=checked_at,
        )

    async def replace_identity(
        self,
        *,
        account_id: str,
        user_address: str,
        signer_private_key: str,
        telegram_user_id: int,
        validation: Any,
    ) -> None:
        owner = await self.owner_for(telegram_user_id)
        if (
            owner is None
            or str(owner["role"]) not in {"USER", "ADMIN"}
            or str(owner["account_id"]) != account_id
        ):
            raise ValueError("Telegram user must own the follower account")
        await self._credentials.replace_identity(
            account_id=account_id,
            user=user_address,
            signer_private_key_hex=signer_private_key,
            role="follower",
            label=account_id,
            validation=self.validation_record(validation),
            owner_telegram_user_id=telegram_user_id,
            owner_chat_id=int(owner["chat_id"]),
        )

    async def commit_onboarding(self, payload: OnboardingCommit) -> None:
        owner = await self._users.get(payload.telegram_user_id)
        if owner is None or str(owner["role"]) not in {"USER", "ADMIN"}:
            raise ValueError("Telegram user must be pre-allowlisted")
        if owner["account_id"] not in (None, payload.account_id):
            raise ValueError("Telegram user owns another account")
        if payload.account_id != stable_account_id(payload.telegram_user_id):
            raise ValueError("account ID is not the stable Telegram account ID")
        validation = self.validation_record(payload)
        builder_verified_at: str | None = None
        if payload.identity_source == "builder":
            if (
                not payload.builder_address
                or not payload.builder_max_fee_rate
                or payload.builder_agent_expires_at_ms is None
                or payload.builder_verified_at_ms is None
            ):
                raise ValueError("builder attestation snapshot is required")
            builder_verified_at = datetime.fromtimestamp(
                payload.builder_verified_at_ms / 1000, tz=UTC
            ).isoformat()
        await self._credentials.commit_onboarding(
            account_id=payload.account_id,
            user=payload.user_address,
            signer_private_key_hex=payload.signer_private_key,
            role="follower",
            label=payload.label,
            owner_telegram_user_id=payload.telegram_user_id,
            owner_chat_id=int(owner["chat_id"]),
            validation=validation,
            identity_source=payload.identity_source,
            builder_address=payload.builder_address,
            builder_name=payload.builder_name,
            builder_max_fee_rate=payload.builder_max_fee_rate,
            builder_agent_expires_at_ms=payload.builder_agent_expires_at_ms,
            builder_verified_at=builder_verified_at,
        )

    async def get_reauthorize_context(self, telegram_user_id: int) -> ReauthorizeContext | None:
        """Return the current builder identity when same-signer re-approval applies.

        Only an enabled builder-source follower with a stored Agent signer
        qualifies.  Direct accounts, disconnected accounts and missing
        validation all return None so the reauthorization path stays closed.
        """
        owner: Any = await self._users.get(telegram_user_id)
        if owner is None or str(owner["role"]) not in {"USER", "ADMIN"}:
            return None
        account_id = owner["account_id"]
        if not account_id:
            return None
        account: Any = await self._db.fetchone(
            "SELECT user_address, role, enabled, identity_source FROM accounts WHERE id=?",
            (str(account_id),),
        )
        if (
            account is None
            or str(account["role"]) != "follower"
            or not int(account["enabled"])
            or str(account["identity_source"] or "direct") != "builder"
        ):
            return None
        validation: Any = await self._db.fetchone(
            "SELECT signer_address FROM agent_validations WHERE account_id=?",
            (str(account_id),),
        )
        if validation is None or not str(validation["signer_address"] or "").strip():
            return None
        return ReauthorizeContext(
            account_id=str(account_id),
            user_address=str(account["user_address"]),
            signer_address=str(validation["signer_address"]),
        )

    async def account_version(self, account_id: str) -> str | None:
        """Credential/validation version marker for attestation race checks.

        Same shape as the runtime adapter marker so pre-read and pre-store
        versions are directly comparable.
        """
        row: Any = await self._db.fetchone(
            "SELECT c.updated_at, a.signer_address, ac.enabled "
            "FROM credentials c "
            "JOIN accounts ac ON ac.id=c.account_id "
            "LEFT JOIN agent_validations a ON a.account_id=c.account_id "
            "WHERE c.account_id=?",
            (account_id,),
        )
        if row is None:
            return None
        return f"{row['updated_at']}:{row['signer_address'] or '-'}:{int(row['enabled'])}"

    async def commit_reauthorization(
        self,
        *,
        account_id: str,
        telegram_user_id: int,
        validation: AgentValidation,
        builder_address: str,
        builder_name: str,
        builder_max_fee_rate: str,
        builder_agent_expires_at_ms: int,
        builder_verified_at: str,
        expected_version: str,
    ) -> None:
        """Atomically repair the Builder proof for the current identity.

        The Agent validation, Builder snapshot and paused entry gate are
        written in one transaction.  The credential version is re-checked
        inside that transaction: a replacement/disconnect that landed while
        the signed reads were in flight aborts instead of storing an old
        proof for a new signer.
        """
        async with self._db.transaction() as conn:
            owner = await _conn_fetchone(
                conn,
                "SELECT account_id, role FROM telegram_users WHERE telegram_user_id=?",
                (int(telegram_user_id),),
            )
            if (
                owner is None
                or str(owner["account_id"]) != account_id
                or str(owner["role"]) not in {"USER", "ADMIN"}
            ):
                raise ValueError("Telegram user must own the follower account")
            account = await _conn_fetchone(
                conn,
                "SELECT role, enabled, identity_source FROM accounts WHERE id=?",
                (account_id,),
            )
            if account is None or str(account["role"]) != "follower" or not int(account["enabled"]):
                raise ValueError("builder account is not active")
            version_row = await _conn_fetchone(
                conn,
                "SELECT c.updated_at, a.signer_address, ac.enabled "
                "FROM credentials c "
                "JOIN accounts ac ON ac.id=c.account_id "
                "LEFT JOIN agent_validations a ON a.account_id=c.account_id "
                "WHERE c.account_id=?",
                (account_id,),
            )
            if version_row is None:
                raise ValueError("account identity is gone")
            current_version = (
                f"{version_row['updated_at']}:"
                f"{version_row['signer_address'] or '-'}:"
                f"{int(version_row['enabled'])}"
            )
            if current_version != expected_version:
                raise ValueError("account identity changed during verification")
            if (
                str(validation.signer_address).casefold()
                != str(version_row["signer_address"] or "").casefold()
                or not str(version_row["signer_address"] or "").strip()
            ):
                # The proof must belong to the current signer; a version
                # string alone cannot prove that after a same-timestamp swap.
                raise ValueError("attestation signer does not match current identity")
            await _conn_execute(
                conn,
                "INSERT INTO agent_validations(account_id, signer_address, can_read, "
                "can_perp_trade, can_withdraw, expired_ms, ip_whitelist, status, "
                "checked_at, last_error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET signer_address=excluded.signer_address, "
                "can_read=excluded.can_read, can_perp_trade=excluded.can_perp_trade, "
                "can_withdraw=excluded.can_withdraw, expired_ms=excluded.expired_ms, "
                "ip_whitelist=excluded.ip_whitelist, status=excluded.status, "
                "checked_at=excluded.checked_at, last_error_code=excluded.last_error_code",
                (
                    account_id,
                    validation.signer_address,
                    int(validation.can_read),
                    int(validation.can_perp_trade),
                    int(validation.can_withdraw),
                    validation.expired_ms,
                    validation.ip_whitelist,
                    validation.status,
                    validation.checked_at,
                    validation.last_error_code,
                ),
            )
            await _conn_execute(
                conn,
                "UPDATE accounts SET identity_source='builder', builder_address=?, "
                "builder_name=?, builder_max_fee_rate=?, builder_agent_expires_at_ms=?, "
                "builder_verified_at=? WHERE id=?",
                (
                    builder_address,
                    builder_name,
                    builder_max_fee_rate,
                    int(builder_agent_expires_at_ms),
                    builder_verified_at,
                    account_id,
                ),
            )
            await _conn_execute(
                conn,
                "UPDATE copy_settings SET allow_new_entries=0 WHERE account_id=?",
                (account_id,),
            )

    async def get_builder_attempt(self, telegram_user_id: int) -> BuilderOnboardingAttempt | None:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        return await repo.get_by_user_id(telegram_user_id)

    async def get_builder_attempt_by_pending_id(
        self, pending_id: str
    ) -> BuilderOnboardingAttempt | None:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        return await repo.get_by_pending_id(pending_id)

    async def save_builder_attempt(self, attempt: BuilderOnboardingAttempt) -> None:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        await repo.save(attempt)

    async def update_builder_attempt_state(
        self, pending_id: str, state: str, *, last_error_code: str | None = None
    ) -> bool:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        return await repo.update_state(pending_id, state, last_error_code=last_error_code)

    async def delete_builder_attempt(self, telegram_user_id: int) -> bool:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        return await repo.delete(telegram_user_id)

    async def delete_builder_attempt_by_pending_id(self, pending_id: str) -> bool:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        return await repo.delete_by_pending_id(pending_id)

    async def cleanup_expired_prepared_attempts(self, now_ms: int) -> int:
        repo = BuilderOnboardingAttemptsRepo(self._db, self._credentials.encryption_key)
        return await repo.cleanup_expired_prepared(now_ms)


class DirectOnboardingAdapter(OnboardingPort):
    """Aster V3 signed preflight/commit adapter for direct Telegram onboarding."""

    def __init__(self, app: App, store: SqliteOnboardingStore) -> None:
        self._app = app
        self._store = store
        override = bool(app.cfg.testnet_ip_whitelist_override and app.cfg.is_testnet)
        master_user_address = None
        if "main" in app.credentials.account_ids(role="master"):
            master_user_address = app.credentials.identity("main").user
        config = DirectOnboardingConfig(
            base_url=app.cfg.rest_base_url,
            trading_chain_id=714,
            expected_egress_ips=tuple(app.cfg.aster_egress_ips),
            require_agent_ip_whitelist=app.cfg.require_agent_ip_whitelist and not override,
            expiry_safety_margin_ms=5 * 60 * 1000,
            expected_position_mode=app.cfg.position_mode,
            master_user_address=master_user_address,
        )
        self._service = DirectOnboardingService(
            self._client_factory,
            store,
            config=config,
            activation=self._activate,
        )

    def _client_factory(
        self,
        identities: EphemeralIdentityProvider,
        keys: EphemeralKeyProvider,
    ) -> AsterClient:
        return AsterClient(
            http=self._app.http,
            base_url=self._app.cfg.rest_base_url,
            limiter=self._app.limiter,
            identities=identities,
            keys=keys,
            nonces=self._app.nonces,
            dry_run=self._app.cfg.dry_run,
        )

    async def _activate(self, account_id: str) -> None:
        if self._app.follower_registry.get(account_id) is None:
            await self._app.register_follower(account_id)
        # The account is committed paused; leverage must be confirmed per
        # symbol BEFORE the user is allowed to resume entries.
        await _prepare_follower_leverage(self._app, account_id)

    async def onboard(self, request: DirectOnboardingRequest) -> DirectOnboardingResult:
        return await self._service.onboard(request)

    async def replace(
        self, request: DirectOnboardingRequest, *, preflight: Any
    ) -> DirectOnboardingResult:
        if not isinstance(request, DirectOnboardingRequest) or not isinstance(
            preflight, DirectPreflightResult
        ):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        try:
            account_id = stable_account_id(request.telegram_user_id)
        except ValueError:
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT) from None
        if request.declared_signer_address.casefold() != (
            preflight.validation.signer_address.casefold()
        ):
            raise OnboardingError(OnboardingErrorCode.INVALID_INPUT)
        if not preflight.read_bundle.is_flat or preflight.read_bundle.open_orders:
            raise OnboardingError(OnboardingErrorCode.ACCOUNT_NOT_FLAT)
        row = await self._app.db.fetchone(
            "SELECT id, role, user_address, enabled FROM accounts WHERE id=?",
            (account_id,),
        )
        owner = await self._store.owner_for(request.telegram_user_id)
        if (
            row is None
            or str(row["role"]) != "follower"
            or not int(row["enabled"])
            or str(row["user_address"]).casefold() != request.user_address.casefold()
            or owner is None
            or str(owner["role"]) not in {"USER", "ADMIN"}
            or str(owner["account_id"]) != account_id
        ):
            raise OnboardingError(OnboardingErrorCode.USER_ADDRESS_CONFLICT)
        try:
            await self._store.replace_identity(
                account_id=account_id,
                user_address=str(row["user_address"]),
                signer_private_key=request.signer_private_key,
                telegram_user_id=request.telegram_user_id,
                validation=preflight,
            )
        except OnboardingError:
            raise
        except Exception:
            raise OnboardingError(OnboardingErrorCode.COMMIT_FAILED) from None
        return DirectOnboardingResult(
            account_id=account_id,
            masked_user_address=preflight.masked_user_address,
            masked_signer_address=preflight.masked_signer_address,
            position_mode=preflight.position_mode,
            balance_assets=tuple(balance.asset for balance in preflight.read_bundle.balances),
            position_count=sum(
                position.position_amt != Decimal(0) for position in preflight.read_bundle.positions
            ),
            available_usdt=preflight.available_usdt,
            paused=True,
        )


class BuilderOnboardingAdapter:
    """Aster V3 Builder-flow onboarding adapter for Mini App part 1.

    Wraps `BuilderOnboardingService` with the same App-scoped HTTP client,
    rate limiter, and paused activation as the direct adapter.  The generated
    signer key is committed through the same encrypted store boundary.
    """

    def __init__(self, app: App, store: SqliteOnboardingStore) -> None:
        self._app = app
        self._store = store
        override = bool(app.cfg.testnet_ip_whitelist_override and app.cfg.is_testnet)
        master_user_address = None
        if "main" in app.credentials.account_ids(role="master"):
            master_user_address = app.credentials.identity("main").user
        config = BuilderOnboardingConfig(
            base_url=app.cfg.rest_base_url,
            trading_chain_id=714,
            registration_chain_id=app.cfg.registration_chain_id,
            builder_address=app.cfg.builder_address,
            builder_name=app.cfg.builder_name,
            max_fee_rate=app.cfg.max_fee_rate,
            agent_expiry_days=app.cfg.builder_agent_expiry_days,
            approval_ttl_s=app.cfg.builder_approval_ttl_s,
            expected_egress_ips=tuple(app.cfg.aster_egress_ips),
            require_agent_ip_whitelist=app.cfg.require_agent_ip_whitelist and not override,
            expected_position_mode=app.cfg.position_mode,
            master_user_address=master_user_address,
        )
        self._service = BuilderOnboardingService(
            app.http,
            app.limiter,
            self._client_factory,
            store,
            config=config,
            activation=self._activate,
            runtime_client_factory=self._runtime_client,
        )

    def _runtime_client(self, account_id: str) -> Any:
        """Live signed-read client for the current identity (reauthorization only)."""
        runtime = self._app.follower_registry.get(account_id)
        if runtime is None:
            raise KeyError(f"follower runtime {account_id!r} not found")
        return runtime.client

    def _client_factory(
        self,
        identities: EphemeralIdentityProvider,
        keys: EphemeralKeyProvider,
    ) -> AsterClient:
        return AsterClient(
            http=self._app.http,
            base_url=self._app.cfg.rest_base_url,
            limiter=self._app.limiter,
            identities=identities,
            keys=keys,
            nonces=self._app.nonces,
            dry_run=self._app.cfg.dry_run,
        )

    async def _activate(self, account_id: str) -> None:
        if self._app.follower_registry.get(account_id) is None:
            await self._app.register_follower(account_id)
        # The account is committed paused; leverage must be confirmed per
        # symbol BEFORE the user is allowed to resume entries.
        await _prepare_follower_leverage(self._app, account_id)

    async def prepare(self, request: BuilderPrepareRequest) -> BuilderPrepareResult:
        return await self._service.prepare(request)

    async def submit(self, request: BuilderSubmitRequest) -> BuilderOnboardingResult:
        return await self._service.submit(request)

    async def reauthorize_prepare(self, telegram_user_id: int) -> BuilderPrepareResult:
        return await self._service.reauthorize_prepare(telegram_user_id)

    async def reauthorize_submit(self, request: BuilderSubmitRequest) -> BuilderReauthorizeResult:
        return await self._service.reauthorize_submit(request)

    async def identity_for_recovery(
        self, account_id: str, telegram_user_id: int
    ) -> tuple[str, str] | None:
        return await self._store.identity_for_recovery(account_id, telegram_user_id)

    async def get_builder_attempt(self, telegram_user_id: int) -> Any:
        return await self._store.get_builder_attempt(telegram_user_id)

    async def delete_builder_attempt(self, telegram_user_id: int) -> bool:
        return await self._store.delete_builder_attempt(telegram_user_id)


class AppStage2RuntimeAdapter(Stage2RuntimeAdapter):
    """Concrete Stage 2 runtime port backed by one composed :class:`App`."""

    def __init__(self, app: App, *, clock: Callable[[], datetime] | None = None) -> None:
        self.app = app
        self.db = app.db
        self.cfg = app.cfg
        self.credentials = app.credentials
        self._snapshot_cache: dict[str, tuple[AccountState, datetime]] = {}
        # Injectable for deterministic snapshot-TTL tests; production keeps
        # wall-clock time so captured_at reflects the true exchange read.
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def testnet(self) -> bool:
        return self.cfg.is_testnet

    @property
    def ready(self) -> bool:
        return self.app.ready

    async def is_account_registered(self, account_id: str) -> bool:
        return self.app.follower_registry.get(account_id) is not None

    async def account_version(self, account_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT c.updated_at, a.signer_address, ac.enabled "
            "FROM credentials c "
            "JOIN accounts ac ON ac.id=c.account_id "
            "LEFT JOIN agent_validations a ON a.account_id=c.account_id "
            "WHERE c.account_id=?",
            (account_id,),
        )
        if row is None:
            return None
        return f"{row['updated_at']}:{row['signer_address'] or '-'}:{int(row['enabled'])}"

    async def recover_account_runtime(
        self,
        account_id: str,
        user_address: str,
        signer_address: str,
    ) -> bool:
        if self.app.follower_registry.get(account_id) is not None:
            return False
        row = await self.db.fetchone(
            "SELECT role, enabled, execution_mode FROM accounts WHERE id=?",
            (account_id,),
        )
        policy = await self.app.settings.get_policy(account_id)
        identity = self.credentials.identity(account_id)
        if (
            row is None
            or str(row["role"]) != "follower"
            or not int(row["enabled"])
            or str(row["execution_mode"] or "REAL") != "REAL"
            or policy is None
            or policy.allow_new_entries
            or identity.user.casefold() != user_address.strip().casefold()
            or identity.signer.casefold() != signer_address.strip().casefold()
        ):
            return False
        await self.app.register_follower(account_id)
        try:
            # Best-effort here: the account is published paused either way and
            # the background worker retries any symbol left not-ready.
            await _prepare_follower_leverage(self.app, account_id)
        except LeverageError as exc:
            log.warning("leverage prepare deferred for %s: %s", account_id, exc)
        return True

    async def cached_snapshot(
        self, account_id: str
    ) -> AccountState | tuple[AccountState, datetime] | None:
        entry = self._snapshot_cache.get(account_id)
        return entry

    async def refresh_snapshot(
        self, account_id: str
    ) -> AccountState | tuple[AccountState, datetime]:
        snapshot = await self.app.snapshot_follower(account_id)
        captured_at = self._clock()
        self._snapshot_cache[account_id] = (snapshot, captured_at)
        return (snapshot, captured_at)

    async def read_activity(
        self,
        account_id: str,
        limit: int,
        offset: int,
        kind_filter: ActivityKindFilter | None = None,
    ) -> list[Any]:
        if limit <= 0 or offset < 0:
            raise ValueError("activity bounds are invalid")
        # The category predicate belongs in the WHERE clause: applying it after
        # LIMIT/OFFSET would truncate the general journal first and hide every
        # matching row beyond that window.
        clause, params = kind_filter.clause() if kind_filter is not None else ("", ())
        return await self.db.fetchall(
            "SELECT command_key, kind, status, payload, created_at, updated_at "
            "FROM execution_commands WHERE account_id=?"
            f"{clause} "
            "ORDER BY updated_at DESC, command_key DESC LIMIT ? OFFSET ?",
            (account_id, *params, int(limit), int(offset)),
        )

    async def pause_account(self, account_id: str) -> None:
        await self.app.coordinator.close_generation_admission()
        # Closing drains the current generation.  Reopen before the targeted
        # reconcile so reduction actions can be admitted while the durable
        # account policy remains paused and therefore cannot create entries.
        await self.app.coordinator.open_generation_admission()
        await self.app.reconcile_follower(account_id, reason="account_pause")

    async def resume_account(self, account_id: str) -> None:
        # Resume is the natural re-verification point: leverage must be
        # confirmed for every whitelisted symbol BEFORE generation admission
        # reopens. A failure propagates so the application service rolls the
        # durable entry gate back to paused (fail-closed).
        await _prepare_follower_leverage(self.app, account_id)
        await self.app.coordinator.open_generation_admission()

    async def reconcile_account(self, account_id: str) -> None:
        await self.app.reconcile_follower(account_id, reason="application")

    async def reconcile_all(self) -> None:
        await self.app.reconciler.reconcile(self.app.projector.state, reason="application")

    async def pause_entries(self, timeout_s: float = 5.0) -> None:
        await self.app.pause_entries(timeout_s=timeout_s)

    async def resume_entries(self) -> None:
        await self.app.resume_entries()

    async def reopen_entries_while_paused(self) -> None:
        await self.app.reopen_entries_while_paused()

    def _permission_config(self, *, require_whitelist: bool) -> DirectOnboardingConfig:
        return DirectOnboardingConfig(
            base_url=self.cfg.rest_base_url,
            trading_chain_id=714,
            expected_egress_ips=tuple(self.cfg.aster_egress_ips),
            require_agent_ip_whitelist=require_whitelist,
            expiry_safety_margin_ms=5 * 60 * 1000,
            expected_position_mode=self.cfg.position_mode,
        )

    async def refresh_agent_permission(self, account_id: str) -> AgentValidation:
        runtime = self.app.follower_registry.get(account_id)
        if runtime is None:
            raise KeyError(f"follower runtime {account_id!r} not found")
        identity = self.credentials.identity(account_id)
        try:
            agent: AgentDTO = await runtime.client.agent(account_id)
        except AsterError as exc:
            if exc.code in UNKNOWN_CODES:
                raise ServiceUnavailableError(
                    "signed Agent permission read is unavailable"
                ) from None
            raise
        checked_at_ms = int(time.time() * 1000)
        status = "VALID"
        try:
            snapshot = validate_agent_permissions(
                agent,
                identity.signer,
                self._permission_config(require_whitelist=self.cfg.require_agent_ip_whitelist),
                checked_at_ms=checked_at_ms,
            )
        except OnboardingError as exc:
            if exc.code is not OnboardingErrorCode.AGENT_IP_WHITELIST:
                raise
            if not await self.permission_override_allowed(account_id):
                raise
            snapshot = validate_agent_permissions(
                agent,
                identity.signer,
                self._permission_config(require_whitelist=False),
                checked_at_ms=checked_at_ms,
            )
            status = "VALID_OVERRIDE"
        return AgentValidation(
            signer_address=snapshot.signer_address,
            can_read=snapshot.can_read,
            can_perp_trade=snapshot.can_perp_trade,
            can_withdraw=snapshot.can_withdraw,
            expired_ms=snapshot.expired_ms,
            ip_whitelist=snapshot.ip_whitelist,
            status=status,
            checked_at=datetime.fromtimestamp(checked_at_ms / 1000, tz=UTC).isoformat(),
        )

    async def permission_override_allowed(self, account_id: str) -> bool:
        return bool(
            self.cfg.testnet_ip_whitelist_override
            and self.cfg.is_testnet
            and await self.is_account_registered(account_id)
        )

    async def refresh_builder_record(self, account_id: str) -> list[BuilderDTO]:
        """Signed GET /fapi/v3/builder for the current identity.

        Transport failures map exactly like the Agent permission read:
        unknown-outcome/rate-limit codes are transient, everything else
        propagates for the caller to classify.
        """
        runtime = self.app.follower_registry.get(account_id)
        if runtime is None:
            raise KeyError(f"follower runtime {account_id!r} not found")
        try:
            records = await runtime.client.builder(account_id)
        except AsterError as exc:
            if exc.code in UNKNOWN_CODES:
                raise ServiceUnavailableError(
                    "signed Builder attestation read is unavailable"
                ) from None
            raise
        if not isinstance(records, list):
            raise ServiceUnavailableError("signed Builder attestation read is unavailable")
        return records

    async def validate_resume_safety(self, account_id: str, snapshot: AccountState) -> None:
        if not isinstance(snapshot, AccountState) or snapshot.account_id != account_id:
            raise ValueError("resume snapshot is invalid")
        for order in snapshot.open_orders:
            if not str(order["client_order_id"]).startswith("ct"):
                raise RuntimeError("manual open order blocks resume")

        basis_rows = await self.db.fetchall(
            "SELECT symbol, pos_side, issued_target FROM sync_basis WHERE account_id=?",
            (account_id,),
        )
        issued: dict[tuple[str, str], Decimal] = {}
        for row in basis_rows:
            raw_target = row["issued_target"]
            if raw_target is None:
                continue
            try:
                target = json.loads(str(raw_target))
                if not isinstance(target, dict) or "qty" not in target:
                    raise ValueError("issued target is not an object with qty")
                quantity = Decimal(str(target["qty"]))
                if not quantity.is_finite():
                    raise ValueError("issued target quantity is not finite")
                issued[(str(row["symbol"]), str(row["pos_side"]))] = quantity
            except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
                raise RuntimeError("sync basis issued target is invalid") from None
        for key, position in snapshot.positions.items():
            if position.qty == 0:
                continue
            target = issued.get((key.symbol, key.pos_side.value))
            if target is None:
                raise RuntimeError("manual position blocks resume")
            if key.pos_side.value == "BOTH":
                if target == 0 or position.qty * target < 0 or abs(position.qty) > abs(target):
                    raise RuntimeError("position exceeds issued target")
            elif target < 0 or position.qty < 0 or position.qty > target:
                raise RuntimeError("position exceeds issued target")

    def follower_lifecycle(
        self, account_id: str
    ) -> AbstractAsyncContextManager[Stage2AccountLifecycle]:
        return self.app.follower_lifecycle(account_id)

    async def commit_disconnect(self, account_id: str) -> None:
        if not await self.credentials.remove_identity(account_id):
            raise KeyError(f"account {account_id!r} is not registered")
        self._snapshot_cache.pop(account_id, None)

    async def build_replacement_runtime(
        self, account_id: str, request: DirectOnboardingRequest, preflight: Any
    ) -> Any:
        del request, preflight
        return self.app.new_real_follower_runtime(account_id)

    async def master_has_open_positions(self) -> bool:
        return any(position.qty != 0 for position in self.app.projector.state.positions.values())


class AppEmergencyStateAdapter:
    """Persist emergency pause state through the existing settings repository."""

    def __init__(self, app: App) -> None:
        self._settings = app.settings

    async def pause_account(self, account_id: str) -> None:
        await self._settings.patch_policy(account_id, allow_new_entries=False)

    async def begin_global(self, operation_id: str) -> None:
        await self._settings.set_flag("emergency_active", "true")
        await self._settings.set_flag("allow_new_entries", "false")
        await self._settings.set_flag("emergency_operation_id", operation_id)

    async def record_result(self, report: EmergencyReport) -> None:
        status = "CONFIRMED" if report.success else "FAILED"
        await self._settings.set_flag("emergency_last_result", f"{status}:{report.operation_id}")


@dataclass(frozen=True, slots=True)
class Stage2Composition:
    """Concrete service/adapters assembled around one already-created App."""

    runtime: AppStage2RuntimeAdapter
    onboarding: DirectOnboardingAdapter
    builder_onboarding: BuilderOnboardingAdapter
    emergency_state: AppEmergencyStateAdapter
    emergency: EmergencyController
    application: UserApplicationService


def build_stage2_composition(
    app: App,
    *,
    admin_ids: list[int] | str | None = None,
) -> Stage2Composition:
    """Build Stage 2 use cases without changing App startup or CLI wiring."""
    runtime = AppStage2RuntimeAdapter(app)
    store = SqliteOnboardingStore(
        app.db,
        app.credentials,
        permission_override=bool(app.cfg.testnet_ip_whitelist_override and app.cfg.is_testnet),
    )
    onboarding = DirectOnboardingAdapter(app, store)
    builder_onboarding = BuilderOnboardingAdapter(app, store)
    emergency_state = AppEmergencyStateAdapter(app)
    emergency = EmergencyController(
        lifecycle_factory=app.follower_lifecycle,
        executor=app.executor,
        state=emergency_state,
    )
    application = UserApplicationService(
        runtime,
        admin_ids=app.cfg.telegram_admin_ids if admin_ids is None else admin_ids,
        onboarding=onboarding,
        builder_onboarding=builder_onboarding,
        emergency_controller=emergency,
    )
    return Stage2Composition(
        runtime, onboarding, builder_onboarding, emergency_state, emergency, application
    )


__all__ = [
    "AppEmergencyStateAdapter",
    "AppStage2RuntimeAdapter",
    "BuilderOnboardingAdapter",
    "DirectOnboardingAdapter",
    "SqliteOnboardingStore",
    "Stage2Composition",
    "build_stage2_composition",
]
