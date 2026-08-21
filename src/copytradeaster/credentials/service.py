"""SQLite-backed encrypted credential storage implementing KeyProvider.

Blobs are AES-GCM(JSON{"signer_key", "main_key"}). The sync KeyProvider surface
reads an in-memory cache warmed once at boot (`load`) and updated by
`import_identity` - private keys never touch logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from eth_account import Account

from copytradeaster.credentials import crypto
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.logging_utils import get_logger, redact
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import AccountRepo, CredentialRepo

log = get_logger(__name__)


@dataclass(slots=True)
class _AccountCreds:
    user_address: str
    signer_key: str
    main_key: str | None


class SqliteCredentials:
    def __init__(self, db: Database, key: bytes, trading_chain_id: int = 714) -> None:
        self._db = db
        self._key = key
        self._trading_chain_id = trading_chain_id
        self._cache: dict[str, _AccountCreds] = {}
        self._roles: dict[str, str] = {}

    async def load(self) -> None:
        """Warm the cache from the DB (called once at boot)."""
        accounts = await self._db.fetchall("SELECT id, role, user_address FROM accounts")
        cred_repo = CredentialRepo(self._db)
        for row in accounts:
            account_id = str(row["id"])
            blob = await cred_repo.get(account_id)
            data = json.loads(crypto.decrypt(self._key, blob))
            self._cache[account_id] = _AccountCreds(
                user_address=str(row["user_address"]),
                signer_key=data["signer_key"],
                main_key=data.get("main_key"),
            )
            self._roles[account_id] = str(row["role"])
        log.info("credentials loaded: %d accounts", len(self._cache))

    async def import_identity(
        self,
        account_id: str,
        user: str,
        signer_private_key_hex: str,
        role: str,
        label: str,
        main_private_key_hex: str | None = None,
    ) -> None:
        await AccountRepo(self._db).upsert(account_id, role, label, user)
        signer_address = Account.from_key(signer_private_key_hex).address
        blob = crypto.encrypt(
            self._key,
            json.dumps(
                {"signer_key": signer_private_key_hex, "main_key": main_private_key_hex}
            ).encode(),
        )
        await CredentialRepo(self._db).put_encrypted(account_id, blob)
        self._cache[account_id] = _AccountCreds(
            user_address=user,
            signer_key=signer_private_key_hex,
            main_key=main_private_key_hex,
        )
        self._roles[account_id] = role
        log.info(
            "imported %s role=%s user=%s signer=%s",
            label,
            role,
            redact(user),
            redact(signer_address),
        )

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
