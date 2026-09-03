"""SQLite-backed encrypted credential storage implementing KeyProvider.

Blobs are AES-GCM(JSON{"signer_key", "main_key"}). The sync provider surface
reads an in-memory cache warmed once at boot (`load`) and updated by
`import_identity` - private keys never touch logs.

Nonces live in storage.repositories.NonceStore (async, persist-before-send);
the signing path awaits them directly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from eth_account import Account

from copytradeaster.credentials import crypto
from copytradeaster.domain.models import RiskPolicy, TradingIdentity
from copytradeaster.logging_utils import get_logger
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AgentValidation,
    CredentialRepo,
    OwnershipConflictError,
    _conn_execute,
    _conn_fetchone,
    _now_iso,
)

log = get_logger(__name__)


@dataclass(slots=True)
class _AccountCreds:
    user_address: str
    signer_key: str = field(repr=False)
    main_key: str | None = field(default=None, repr=False)


class SqliteCredentials:
    def __init__(self, db: Database, key: bytes, trading_chain_id: int = 714) -> None:
        self._db = db
        self._key = key
        self._trading_chain_id = trading_chain_id
        self._cache: dict[str, _AccountCreds] = {}
        self._roles: dict[str, str] = {}
        self._loaded = False

    @property
    def encryption_key(self) -> bytes:
        return self._key

    async def load(self) -> None:
        """Warm caches from the DB (called once at boot)."""
        # SHADOW and disabled historical followers deliberately have no live
        # credential cache entry.  A disabled REAL row is retained for audit
        # and must not make boot fail because its blob was deleted on disconnect.
        accounts = await self._db.fetchall(
            "SELECT id, role, user_address FROM accounts "
            "WHERE COALESCE(execution_mode, 'REAL')='REAL' AND enabled=1"
        )
        cred_repo = CredentialRepo(self._db)
        next_cache: dict[str, _AccountCreds] = {}
        next_roles: dict[str, str] = {}
        try:
            for row in accounts:
                account_id = str(row["id"])
                blob = await cred_repo.get(account_id)
                data = json.loads(crypto.decrypt(self._key, blob))
                next_cache[account_id] = _AccountCreds(
                    user_address=str(row["user_address"]),
                    signer_key=data["signer_key"],
                    main_key=data.get("main_key"),
                )
                next_roles[account_id] = str(row["role"])
        except BaseException:
            self._cache.clear()
            self._roles.clear()
            self._loaded = False
            raise
        self._cache = next_cache
        self._roles = next_roles
        self._loaded = True
        log.info("credentials loaded: %d accounts", len(self._cache))

    async def import_identity(
        self,
        account_id: str,
        user: str,
        signer_private_key_hex: str,
        role: str,
        label: str,
        main_private_key_hex: str | None = None,
        *,
        owner_telegram_user_id: int | None = None,
        owner_chat_id: int | None = None,
        validation: AgentValidation | Mapping[str, Any] | None = None,
    ) -> None:
        """Stage 1-compatible import with one DB transaction.

        The historical CLI import intentionally keeps its old unpaused default
        (new accounts may trade immediately).  Direct Stage 2 onboarding uses
        :meth:`commit_onboarding`, which always starts paused and requires an
        owner plus an Agent validation snapshot.
        """
        direct_onboarding = owner_telegram_user_id is not None or validation is not None
        await self._commit_identity(
            account_id=account_id,
            user=user,
            signer_private_key_hex=signer_private_key_hex,
            role=role,
            label=label,
            main_private_key_hex=main_private_key_hex,
            owner_telegram_user_id=owner_telegram_user_id,
            owner_chat_id=owner_chat_id,
            validation=validation,
            paused=direct_onboarding,
            require_validation=direct_onboarding,
            require_existing=False,
            reject_enabled_existing=direct_onboarding,
            allow_disabled_address_change=direct_onboarding,
        )

    async def commit_onboarding(
        self,
        account_id: str,
        user: str,
        signer_private_key_hex: str,
        role: str,
        label: str,
        owner_telegram_user_id: int,
        owner_chat_id: int,
        validation: AgentValidation | Mapping[str, Any],
        main_private_key_hex: str | None = None,
        policy: RiskPolicy | None = None,
        *,
        identity_source: str = "direct",
        builder_address: str | None = None,
        builder_name: str | None = None,
        builder_max_fee_rate: str | None = None,
        builder_agent_expires_at_ms: int | None = None,
        builder_verified_at: str | None = None,
    ) -> None:
        """Atomically install a direct-onboarding identity and ownership.

        Account, encrypted blob, default/existing policy, Telegram owner and
        validation snapshot are written on one connection under one explicit
        ``BEGIN IMMEDIATE`` transaction.  Cache publication happens only
        after that transaction commits successfully.
        """
        await self._commit_identity(
            account_id=account_id,
            user=user,
            signer_private_key_hex=signer_private_key_hex,
            role=role,
            label=label,
            main_private_key_hex=main_private_key_hex,
            owner_telegram_user_id=owner_telegram_user_id,
            owner_chat_id=owner_chat_id,
            validation=validation,
            policy=policy,
            paused=True,
            require_validation=True,
            require_existing=False,
            reject_enabled_existing=True,
            allow_disabled_address_change=True,
            identity_source=identity_source,
            builder_address=builder_address,
            builder_name=builder_name,
            builder_max_fee_rate=builder_max_fee_rate,
            builder_agent_expires_at_ms=builder_agent_expires_at_ms,
            builder_verified_at=builder_verified_at,
        )

    async def replace_identity(
        self,
        account_id: str,
        user: str,
        signer_private_key_hex: str,
        role: str,
        label: str,
        validation: AgentValidation | Mapping[str, Any],
        main_private_key_hex: str | None = None,
        *,
        owner_telegram_user_id: int | None = None,
        owner_chat_id: int | None = None,
        policy: RiskPolicy | None = None,
    ) -> None:
        """Atomically replace an existing account's encrypted identity.

        The canonical user address must remain attached to the account.  The
        account is paused in the same transaction and its existing policy
        fields are preserved; only ``allow_new_entries`` is forced false.
        """
        await self._commit_identity(
            account_id=account_id,
            user=user,
            signer_private_key_hex=signer_private_key_hex,
            role=role,
            label=label,
            main_private_key_hex=main_private_key_hex,
            owner_telegram_user_id=owner_telegram_user_id,
            owner_chat_id=owner_chat_id,
            validation=validation,
            policy=policy,
            paused=True,
            require_validation=True,
            require_existing=True,
            reject_enabled_existing=False,
            allow_disabled_address_change=False,
        )

    async def remove_identity(self, account_id: str) -> bool:
        """Disable an account and delete its secret/validation atomically.

        Telegram ownership/history rows are deliberately retained so a
        disconnected user remains an auditable historical owner.  Runtime
        cache removal is published only after commit.
        """
        async with self._db.transaction() as conn:
            account = await _conn_fetchone(
                conn,
                "SELECT id FROM accounts WHERE id=?",
                (account_id,),
            )
            if account is None:
                return False
            await _conn_execute(
                conn,
                "UPDATE accounts SET enabled=0, identity_source='direct', "
                "builder_address=NULL, builder_name=NULL, builder_max_fee_rate=NULL, "
                "builder_agent_expires_at_ms=NULL, builder_verified_at=NULL WHERE id=?",
                (account_id,),
            )
            await _conn_execute(
                conn,
                "UPDATE copy_settings SET enabled=0, allow_new_entries=0 WHERE account_id=?",
                (account_id,),
            )
            await _conn_execute(
                conn,
                "DELETE FROM credentials WHERE account_id=?",
                (account_id,),
            )
            await _conn_execute(
                conn,
                "DELETE FROM agent_validations WHERE account_id=?",
                (account_id,),
            )
        self.remove_cache(account_id)
        return True

    async def _commit_identity(
        self,
        *,
        account_id: str,
        user: str,
        signer_private_key_hex: str,
        role: str,
        label: str,
        main_private_key_hex: str | None,
        owner_telegram_user_id: int | None,
        owner_chat_id: int | None,
        validation: AgentValidation | Mapping[str, Any] | None,
        policy: RiskPolicy | None = None,
        paused: bool,
        require_validation: bool,
        require_existing: bool,
        reject_enabled_existing: bool,
        allow_disabled_address_change: bool,
        identity_source: str = "direct",
        builder_address: str | None = None,
        builder_name: str | None = None,
        builder_max_fee_rate: str | None = None,
        builder_agent_expires_at_ms: int | None = None,
        builder_verified_at: str | None = None,
    ) -> None:
        if not account_id or not label or not user:
            raise ValueError("account_id, label and user address are required")
        if role not in {"master", "follower"}:
            raise ValueError(f"unsupported account role: {role!r}")
        if identity_source not in {"direct", "builder"}:
            raise ValueError("identity source must be direct or builder")
        if identity_source == "builder":
            if (
                not builder_address
                or not builder_max_fee_rate
                or builder_agent_expires_at_ms is None
                or not builder_verified_at
            ):
                raise ValueError("builder attestation snapshot is required")
        elif any(
            value is not None
            for value in (
                builder_address,
                builder_name,
                builder_max_fee_rate,
                builder_agent_expires_at_ms,
                builder_verified_at,
            )
        ):
            raise ValueError("direct identity must not carry a builder snapshot")
        if (owner_telegram_user_id is None) != (owner_chat_id is None):
            raise ValueError("owner Telegram user and chat IDs must be supplied together")
        if owner_telegram_user_id is not None and role != "follower":
            raise ValueError("only follower accounts may have a Telegram owner")
        if require_validation and validation is None:
            raise ValueError("Agent validation is required for direct onboarding")

        # Validate and encrypt before BEGIN.  No secret is included in any
        # exception or log message below.
        signer_address = Account.from_key(signer_private_key_hex).address
        validation_record = self._coerce_validation(validation)
        if (
            validation_record is not None
            and validation_record.signer_address.casefold() != signer_address.casefold()
        ):
            raise ValueError("Agent validation signer does not match credential signer")
        blob = crypto.encrypt(
            self._key,
            json.dumps(
                {"signer_key": signer_private_key_hex, "main_key": main_private_key_hex}
            ).encode(),
        )

        async with self._db.transaction() as conn:
            current = await _conn_fetchone(
                conn,
                "SELECT id, role, user_address, execution_mode, enabled FROM accounts WHERE id=?",
                (account_id,),
            )
            if require_existing and current is None:
                raise KeyError(f"unknown account {account_id!r}")
            if current is not None:
                if str(current["role"]) != role:
                    raise ValueError("account role cannot change during credential replacement")
                if reject_enabled_existing and int(current["enabled"]):
                    raise OwnershipConflictError(
                        f"enabled account {account_id!r} already exists; use replace_identity"
                    )
                if (not allow_disabled_address_change or int(current["enabled"])) and str(
                    current["user_address"]
                ).casefold() != user.casefold():
                    raise ValueError("replacement user address must match the existing account")

            current_owner = await _conn_fetchone(
                conn,
                "SELECT telegram_user_id FROM telegram_users WHERE account_id=?",
                (account_id,),
            )
            if (
                current_owner is not None
                and owner_telegram_user_id is not None
                and int(current_owner["telegram_user_id"]) != int(owner_telegram_user_id)
            ):
                raise OwnershipConflictError(
                    f"account {account_id!r} already has another Telegram owner"
                )

            owner_row = None
            if owner_telegram_user_id is not None:
                owner_row = await _conn_fetchone(
                    conn,
                    "SELECT telegram_user_id, role, account_id FROM telegram_users "
                    "WHERE telegram_user_id=?",
                    (int(owner_telegram_user_id),),
                )
                if owner_row is not None:
                    if owner_row["role"] not in {"USER", "ADMIN"}:
                        raise OwnershipConflictError("unsupported Telegram owner role")
                    if owner_row["account_id"] not in (None, account_id):
                        old_account = await _conn_fetchone(
                            conn,
                            "SELECT enabled FROM accounts WHERE id=?",
                            (owner_row["account_id"],),
                        )
                        if old_account is None or int(old_account["enabled"]):
                            raise OwnershipConflictError(
                                f"Telegram user {owner_telegram_user_id} already owns "
                                "another account"
                            )
                account_owner = await _conn_fetchone(
                    conn,
                    "SELECT telegram_user_id FROM telegram_users WHERE account_id=?",
                    (account_id,),
                )
                if account_owner is not None and int(account_owner["telegram_user_id"]) != int(
                    owner_telegram_user_id
                ):
                    raise OwnershipConflictError(
                        f"account {account_id!r} already has another Telegram owner"
                    )

            await _conn_execute(
                conn,
                "INSERT INTO accounts(id, role, label, user_address, enabled, execution_mode, "
                "identity_source, builder_address, builder_name, builder_max_fee_rate, "
                "builder_agent_expires_at_ms, builder_verified_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "role=excluded.role, label=excluded.label, user_address=excluded.user_address, "
                "enabled=1, execution_mode=excluded.execution_mode, "
                "identity_source=excluded.identity_source, "
                "builder_address=excluded.builder_address, "
                "builder_name=excluded.builder_name, "
                "builder_max_fee_rate=excluded.builder_max_fee_rate, "
                "builder_agent_expires_at_ms=excluded.builder_agent_expires_at_ms, "
                "builder_verified_at=excluded.builder_verified_at",
                (
                    account_id,
                    role,
                    label,
                    user,
                    "REAL",
                    identity_source,
                    builder_address,
                    builder_name,
                    builder_max_fee_rate,
                    builder_agent_expires_at_ms,
                    builder_verified_at,
                ),
            )
            await _conn_execute(
                conn,
                "INSERT INTO credentials(account_id, encrypted, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET encrypted=excluded.encrypted, "
                "updated_at=excluded.updated_at",
                (account_id, blob, _now_iso()),
            )

            settings = await _conn_fetchone(
                conn,
                "SELECT risk_percent, max_leverage, max_entry_deviation_pct, risk_base, "
                "enabled, allow_new_entries FROM copy_settings WHERE account_id=?",
                (account_id,),
            )
            if settings is None:
                initial = policy or RiskPolicy(
                    risk_percent=Decimal("0.01"),
                    max_leverage=20,
                    max_entry_deviation_pct=Decimal("0.01"),
                )
                await _conn_execute(
                    conn,
                    "INSERT INTO copy_settings(account_id, risk_percent, max_leverage, "
                    "max_entry_deviation_pct, risk_base, enabled, allow_new_entries) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        account_id,
                        str(initial.risk_percent),
                        initial.max_leverage,
                        str(initial.max_entry_deviation_pct),
                        "available_balance",
                        int(initial.enabled),
                        0 if paused else int(initial.allow_new_entries),
                    ),
                )
            elif paused:
                # Replacement preserves every existing policy field except the
                # explicit safety barrier against new entries.  A direct
                # re-onboarding of a disabled stable account reconnects it,
                # therefore it also re-enables policy participation while
                # remaining paused for entries.
                reactivate = (
                    current is not None
                    and not int(current["enabled"])
                    and allow_disabled_address_change
                )
                await _conn_execute(
                    conn,
                    "UPDATE copy_settings SET enabled=CASE WHEN ? THEN 1 ELSE enabled END, "
                    "allow_new_entries=0 WHERE account_id=?",
                    (int(reactivate), account_id),
                )

            if owner_telegram_user_id is not None:
                now = _now_iso()
                if owner_row is None:
                    await _conn_execute(
                        conn,
                        "INSERT INTO telegram_users(telegram_user_id, chat_id, account_id, role, "
                        "notifications_enabled, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'USER', 1, ?, ?)",
                        (
                            int(owner_telegram_user_id),
                            int(owner_chat_id),
                            account_id,
                            now,
                            now,
                        ),
                    )
                else:
                    await _conn_execute(
                        conn,
                        "UPDATE telegram_users SET chat_id=?, account_id=?, updated_at=? "
                        "WHERE telegram_user_id=?",
                        (
                            int(owner_chat_id),
                            account_id,
                            now,
                            int(owner_telegram_user_id),
                        ),
                    )

            if validation_record is not None:
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
                        validation_record.signer_address,
                        int(validation_record.can_read),
                        int(validation_record.can_perp_trade),
                        int(validation_record.can_withdraw),
                        validation_record.expired_ms,
                        validation_record.ip_whitelist,
                        validation_record.status,
                        validation_record.checked_at,
                        validation_record.last_error_code,
                    ),
                )

        # This is deliberately after the transaction context has committed.
        self.replace_cache(
            account_id,
            user,
            signer_private_key_hex,
            role,
            main_private_key_hex,
        )
        # The durable import is an important action, but wallet identities
        # remain credential-adjacent data and are not useful in an audit line.
        log.info("credentials imported account_id=%s role=%s", account_id, role)

    @staticmethod
    def _coerce_validation(
        validation: AgentValidation | Mapping[str, Any] | None,
    ) -> AgentValidation | None:
        if validation is None:
            return None
        if isinstance(validation, AgentValidation):
            return validation
        return AgentValidation.from_mapping(validation)

    # --- public cache publication helpers -----------------------------------------

    def install_cache(
        self,
        account_id: str,
        user_address: str,
        signer_private_key_hex: str,
        role: str,
        main_private_key_hex: str | None = None,
    ) -> None:
        """Publish a freshly committed identity into the sync key-provider cache."""
        self._cache[account_id] = _AccountCreds(
            user_address=user_address,
            signer_key=signer_private_key_hex,
            main_key=main_private_key_hex,
        )
        self._roles[account_id] = role

    def replace_cache(
        self,
        account_id: str,
        user_address: str,
        signer_private_key_hex: str,
        role: str,
        main_private_key_hex: str | None = None,
    ) -> None:
        """Publish a replacement only after its DB transaction has committed."""
        self.install_cache(
            account_id,
            user_address,
            signer_private_key_hex,
            role,
            main_private_key_hex,
        )

    def remove_cache(self, account_id: str) -> bool:
        """Remove an identity from the sync cache after durable disconnect."""
        existed = account_id in self._cache or account_id in self._roles
        self._cache.pop(account_id, None)
        self._roles.pop(account_id, None)
        return existed

    # --- KeyProvider surface (sync, cache-backed) ---------------------------------

    def private_key(self, account_id: str) -> str:
        return self._get(account_id).signer_key

    def main_private_key(self, account_id: str) -> str | None:
        return self._get(account_id).main_key

    def identity(self, account_id: str) -> TradingIdentity:
        creds = self._get(account_id)
        return TradingIdentity(
            account_id=account_id,
            user=creds.user_address,
            signer=Account.from_key(creds.signer_key).address,
            chain_id=self._trading_chain_id,
        )

    def has_main_key(self, account_id: str) -> bool:
        return self._get(account_id).main_key is not None

    def account_ids(self, role: str | None = None) -> list[str]:
        if role is None:
            return list(self._cache)
        return [a for a, r in self._roles.items() if r == role]

    def _get(self, account_id: str) -> _AccountCreds:
        try:
            return self._cache[account_id]
        except KeyError:
            raise KeyError(f"unknown or not-loaded account {account_id!r}") from None
