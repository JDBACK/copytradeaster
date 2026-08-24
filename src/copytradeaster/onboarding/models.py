"""Secret-safe DTOs shared by direct onboarding adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.aster.models import BalanceDTO, OrderDTO, PositionDTO


def mask_address(address: str) -> str:
    """Return a short display value; never use full addresses in UI/logs."""
    if not address:
        return "<empty>"
    if len(address) <= 12:
        return "0x***"
    return f"{address[:6]}…{address[-4:]}"


def stable_account_id(telegram_user_id: int) -> str:
    """Derive the only supported follower identity from a Telegram user ID."""
    if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int):
        raise ValueError("telegram_user_id must be a numeric integer")
    if telegram_user_id <= 0:
        raise ValueError("telegram_user_id must be positive")
    return f"tg-{telegram_user_id}"


@dataclass(frozen=True, slots=True)
class DirectOnboardingRequest:
    telegram_user_id: int
    user_address: str
    declared_signer_address: str
    signer_private_key: str

    def __repr__(self) -> str:
        return (
            "DirectOnboardingRequest("
            f"telegram_user_id={self.telegram_user_id!r}, "
            f"user_address={mask_address(self.user_address)!r}, "
            f"declared_signer_address={mask_address(self.declared_signer_address)!r}, "
            "signer_private_key='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class DirectOnboardingConfig:
    """Fail-closed policy for the testnet direct onboarding flow."""

    base_url: str = "https://fapi.asterdex-testnet.com"
    trading_chain_id: int = 714
    expected_egress_ips: tuple[str, ...] = ()
    require_agent_ip_whitelist: bool = True
    expiry_safety_margin_ms: int = 5 * 60 * 1000
    expected_position_mode: str | None = None
    master_user_address: str | None = None


@dataclass(frozen=True, slots=True)
class AgentValidationSnapshot:
    signer_address: str
    can_read: bool
    can_perp_trade: bool
    can_withdraw: bool
    expired_ms: int
    ip_whitelist: str
    checked_at_ms: int


@dataclass(frozen=True, slots=True)
class SignedReadBundle:
    balances: tuple[BalanceDTO, ...]
    positions: tuple[PositionDTO, ...]
    open_orders: tuple[OrderDTO, ...]
    dual_side_position: bool

    @property
    def position_mode(self) -> str:
        return "HEDGE" if self.dual_side_position else "ONE_WAY"

    @property
    def available_usdt(self) -> Decimal:
        return sum(
            (
                balance.available_balance
                for balance in self.balances
                if balance.asset.upper() == "USDT"
            ),
            Decimal(0),
        )

    @property
    def is_flat(self) -> bool:
        return all(position.position_amt == Decimal(0) for position in self.positions)


@dataclass(frozen=True, slots=True)
class DirectPreflightResult:
    """Safe remote result; it intentionally contains no credential material."""

    masked_user_address: str
    masked_signer_address: str
    validation: AgentValidationSnapshot
    read_bundle: SignedReadBundle
    paused: bool = True

    @property
    def position_mode(self) -> str:
        return self.read_bundle.position_mode

    @property
    def account_masked(self) -> str:
        return self.masked_user_address

    @property
    def signer_masked(self) -> str:
        return self.masked_signer_address

    @property
    def available_usdt(self) -> Decimal:
        return self.read_bundle.available_usdt

    def __repr__(self) -> str:
        return (
            "DirectPreflightResult("
            f"masked_user_address={self.masked_user_address!r}, "
            f"masked_signer_address={self.masked_signer_address!r}, "
            f"position_mode={self.position_mode!r}, paused={self.paused!r})"
        )


@dataclass(frozen=True, slots=True)
class OnboardingCommit:
    """Payload handed to the future atomic storage implementation.

    The signer key exists only for the duration of the store call.  Its custom
    repr is important because commit payloads are often included in test
    failure output.
    """

    account_id: str
    telegram_user_id: int
    user_address: str
    signer_address: str
    signer_private_key: str
    validation: AgentValidationSnapshot
    read_bundle: SignedReadBundle
    allow_new_entries: bool = False

    @property
    def label(self) -> str:
        """Storage-compatible label, generated from the stable account ID."""
        return self.account_id

    def __repr__(self) -> str:
        return (
            "OnboardingCommit("
            f"account_id={self.account_id!r}, "
            f"telegram_user_id={self.telegram_user_id!r}, "
            f"user_address={mask_address(self.user_address)!r}, "
            f"signer_address={mask_address(self.signer_address)!r}, "
            "signer_private_key='<redacted>', "
            f"allow_new_entries={self.allow_new_entries!r})"
        )


@dataclass(frozen=True, slots=True)
class DirectOnboardingResult:
    """Safe result returned after commit and optional runtime activation."""

    account_id: str
    masked_user_address: str
    masked_signer_address: str
    position_mode: str
    balance_assets: tuple[str, ...]
    position_count: int
    available_usdt: Decimal
    paused: bool = True

    @property
    def account_masked(self) -> str:
        return self.masked_user_address

    @property
    def signer_masked(self) -> str:
        return self.masked_signer_address


__all__ = [
    "AgentValidationSnapshot",
    "DirectOnboardingConfig",
    "DirectOnboardingRequest",
    "DirectOnboardingResult",
    "DirectPreflightResult",
    "OnboardingCommit",
    "SignedReadBundle",
    "mask_address",
    "stable_account_id",
]
