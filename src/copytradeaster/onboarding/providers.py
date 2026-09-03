"""Narrow protocols and ephemeral providers used by direct onboarding."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol

from copytradeaster.aster.models import AgentDTO, BalanceDTO, BuilderDTO, OrderDTO, PositionDTO
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.onboarding.models import OnboardingCommit, ReauthorizeContext
from copytradeaster.storage.repositories import AgentValidation, BuilderOnboardingAttempt


class EphemeralIdentityProvider:
    """IdentityProvider implementation for one in-memory preflight signer."""

    __slots__ = ("_account_id", "_identity")

    def __init__(self, account_id: str, identity: TradingIdentity) -> None:
        self._account_id = account_id
        self._identity = identity

    def identity(self, account_id: str) -> TradingIdentity:
        if account_id != self._account_id:
            raise KeyError("unknown ephemeral onboarding account")
        return self._identity

    def __repr__(self) -> str:
        return (
            f"EphemeralIdentityProvider(account_id={self._account_id!r}, "
            f"signer={self._identity.signer[:6]}…{self._identity.signer[-4:]})"
        )


class EphemeralKeyProvider:
    """KeyProvider implementation whose signer key is never persisted."""

    __slots__ = ("_account_id", "_signer_private_key")

    def __init__(self, account_id: str, signer_private_key: str) -> None:
        self._account_id = account_id
        self._signer_private_key = signer_private_key

    def private_key(self, account_id: str) -> str:
        if account_id != self._account_id:
            raise KeyError("unknown ephemeral onboarding account")
        return self._signer_private_key

    def main_private_key(self, account_id: str) -> None:
        if account_id != self._account_id:
            raise KeyError("unknown ephemeral onboarding account")
        return None

    def __repr__(self) -> str:
        return f"EphemeralKeyProvider(account_id={self._account_id!r}, key='<redacted>')"


class SignedReadClient(Protocol):
    async def agent(self, account_id: str) -> AgentDTO: ...

    async def builder(self, account_id: str) -> list[BuilderDTO]: ...

    async def balance(self, account_id: str) -> list[BalanceDTO]: ...

    async def position_risk(self, account_id: str) -> list[PositionDTO]: ...

    async def open_orders(self, account_id: str) -> list[OrderDTO]: ...

    async def get_position_mode(self, account_id: str) -> bool: ...


class OnboardingClientFactory(Protocol):
    def __call__(
        self, identities: EphemeralIdentityProvider, keys: EphemeralKeyProvider
    ) -> SignedReadClient | Awaitable[SignedReadClient]: ...


class OnboardingStore(Protocol):
    async def find_identity_conflict(
        self, telegram_user_id: int, user_address: str
    ) -> str | None: ...

    async def commit_onboarding(self, payload: OnboardingCommit) -> None: ...

    async def get_reauthorize_context(self, telegram_user_id: int) -> ReauthorizeContext | None: ...

    async def account_version(self, account_id: str) -> str | None: ...

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
    ) -> None: ...

    async def get_builder_attempt(
        self, telegram_user_id: int
    ) -> BuilderOnboardingAttempt | None: ...

    async def get_builder_attempt_by_pending_id(
        self, pending_id: str
    ) -> BuilderOnboardingAttempt | None: ...

    async def save_builder_attempt(self, attempt: BuilderOnboardingAttempt) -> None: ...

    async def update_builder_attempt_state(
        self, pending_id: str, state: str, *, last_error_code: str | None = None
    ) -> bool: ...

    async def delete_builder_attempt(self, telegram_user_id: int) -> bool: ...

    async def delete_builder_attempt_by_pending_id(self, pending_id: str) -> bool: ...

    async def cleanup_expired_prepared_attempts(self, now_ms: int) -> int: ...


ActivationCallback = Callable[[str], Awaitable[None] | None]


async def maybe_await(value: object) -> object:
    """Await a factory/callback result while keeping protocols easy to fake."""
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "ActivationCallback",
    "EphemeralIdentityProvider",
    "EphemeralKeyProvider",
    "OnboardingClientFactory",
    "OnboardingStore",
    "SignedReadClient",
    "maybe_await",
]
