"""Protocols for the boundaries the Aster adapter depends on.

The adapter knows nothing about SQLite; storage-backed implementations live in
credentials/service.py and storage/repositories.py.
"""

from __future__ import annotations

from typing import Protocol

from copytradeaster.domain.models import TradingIdentity


class IdentityProvider(Protocol):
    def identity(self, account_id: str) -> TradingIdentity: ...


class KeyProvider(Protocol):
    def private_key(self, account_id: str) -> str:
        """Signer key (trading context)."""
        ...

    def main_private_key(self, account_id: str) -> str | None:
        """Main wallet key (registration context), if stored."""
        ...


class NonceSource(Protocol):
    async def next_nonce(self, signer: str) -> int:
        """MUST commit/persist BEFORE returning (persist-before-send contract)."""
