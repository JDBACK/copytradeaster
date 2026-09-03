"""Account-specific Aster Builder attribution (P2.7)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)

BUILDER_FEE_ABSOLUTE_CAP = Decimal("0.001")


class AttributionKind(StrEnum):
    DIRECT = "DIRECT"
    BUILDER_VALID = "BUILDER_VALID"
    BUILDER_UNAVAILABLE = "BUILDER_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class BuilderAttribution:
    """Immutable outbound snapshot: builder address + Decimal fee rate."""

    builder: str
    fee_rate: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.builder, str) or not self.builder.strip():
            raise ValueError("builder address is required")
        if isinstance(self.fee_rate, float):
            raise TypeError("fee_rate float is forbidden")
        if isinstance(self.fee_rate, bool):
            raise TypeError("fee_rate bool is forbidden")
        if not isinstance(self.fee_rate, Decimal):
            raise TypeError("fee_rate must be Decimal")


@dataclass(frozen=True, slots=True)
class AttributionDecision:
    kind: AttributionKind
    attribution: BuilderAttribution | None = None
    reason: str | None = None


class _AccountsPort(Protocol):
    async def get(self, account_id: str) -> Any | None: ...


class _ValidationsPort(Protocol):
    async def get(self, account_id: str) -> Any | None: ...


class _CredentialsPort(Protocol):
    def identity(self, account_id: str) -> Any: ...


def _val(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except Exception:
        try:
            value = getattr(row, key)
        except Exception:
            return default
    return default if value is None else value


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip() in {"1", "true", "True"}
    return bool(value)


def _parse_perm(value: Any) -> bool | None:
    """Strict tri-state permission parsing: True, False, or unknown.

    Only exact representations count: bools, 0/1 ints, and "0"/"1"/
    "true"/"false" strings (trimmed, case-insensitive). Anything else
    (None, 2, floats, garbage strings) is unknown and must fail closed
    at the call site. Withdraw is valid only when strictly False
    (P2.6 triple: read/perp exactly True, withdraw exactly False).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true"}:
            return True
        if text in {"0", "false"}:
            return False
        return None
    return None


def _parse_fee(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float):
        return None
    try:
        fee = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    return fee


def _looks_like_address(value: str) -> bool:
    text = value.strip()
    if len(text) != 42 or not text.startswith("0x"):
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True


class BuilderAttributionProvider:
    """Resolve outbound builder attribution from durable backend state."""

    def __init__(
        self,
        *,
        accounts: _AccountsPort,
        validations: _ValidationsPort,
        credentials: _CredentialsPort,
        builder_address: str,
        builder_name: str,
        max_fee_rate: str,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._accounts = accounts
        self._validations = validations
        self._credentials = credentials
        self._builder_address = str(builder_address or "").strip()
        self._builder_name = str(builder_name or "").strip()
        self._max_fee_rate = str(max_fee_rate or "").strip()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def resolve(self, account_id: str) -> AttributionDecision:
        try:
            return await self._resolve_inner(account_id)
        except Exception:
            log.warning("builder attribution lookup failed account_id=%s", account_id)
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="lookup-failed"
            )

    async def _resolve_inner(self, account_id: str) -> AttributionDecision:
        row = await self._accounts.get(account_id)
        if row is None:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="account-unknown"
            )
        raw_source = _val(row, "identity_source", "direct")
        source = str(raw_source or "direct").strip().lower()
        if source == "direct" or source == "":
            return AttributionDecision(kind=AttributionKind.DIRECT)
        if source != "builder":
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="identity-unknown"
            )
        if str(_val(row, "role", "") or "") != "follower":
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="account-inactive"
            )
        if not _is_true(_val(row, "enabled", 0)):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="account-inactive"
            )
        persisted_address = _val(row, "builder_address", None)
        persisted_fee_raw = _val(row, "builder_max_fee_rate", None)
        snapshot_expiry_raw = _val(row, "builder_agent_expires_at_ms", None)
        verified_at = _val(row, "builder_verified_at", None)
        if (
            not isinstance(persisted_address, str)
            or not persisted_address.strip()
            or persisted_fee_raw is None
            or str(persisted_fee_raw).strip() == ""
            or snapshot_expiry_raw is None
            or verified_at is None
            or str(verified_at).strip() == ""
        ):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="snapshot-incomplete"
            )
        try:
            snapshot_expiry = int(snapshot_expiry_raw)
        except (TypeError, ValueError):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="snapshot-incomplete"
            )
        now_ms = int(self._clock_ms())
        if snapshot_expiry <= now_ms:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="agent-expired"
            )
        validation = await self._validations.get(account_id)
        if validation is None:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="validation-missing"
            )
        can_read = _val(validation, "can_read", None)
        can_perp = _val(validation, "can_perp_trade", None)
        can_withdraw = _val(validation, "can_withdraw", None)
        # Strict tri-state: only exact True counts for read/perp, only exact
        # False counts for withdraw (P2.6 triple). Malformed values (None/2/garbage)
        # fail closed and never default into a valid state.
        if _parse_perm(can_read) is not True:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="permissions-invalid"
            )
        if _parse_perm(can_perp) is not True:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="permissions-invalid"
            )
        if _parse_perm(can_withdraw) is not False:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="permissions-invalid"
            )
        try:
            validation_expiry = int(_val(validation, "expired_ms", 0))
        except (TypeError, ValueError):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="validation-missing"
            )
        if validation_expiry <= now_ms:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="agent-expired"
            )
        try:
            identity = self._credentials.identity(account_id)
        except Exception:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="signer-mismatch"
            )
        current_signer = getattr(identity, "signer", None)
        stored_signer = _val(validation, "signer_address", "")
        if (
            not isinstance(current_signer, str)
            or not current_signer.strip()
            or not isinstance(stored_signer, str)
            or not stored_signer.strip()
            or stored_signer.strip().lower() != current_signer.strip().lower()
        ):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="signer-mismatch"
            )
        if not self._builder_address:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="config-missing"
            )
        if not _looks_like_address(persisted_address) or not _looks_like_address(
            self._builder_address
        ):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="builder-address-mismatch"
            )
        if persisted_address.strip().lower() != self._builder_address.strip().lower():
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="builder-address-mismatch"
            )
        persisted_fee = _parse_fee(persisted_fee_raw)
        configured_fee = _parse_fee(self._max_fee_rate)
        if persisted_fee is None or configured_fee is None:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="fee-invalid"
            )
        if not persisted_fee.is_finite() or not configured_fee.is_finite():
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="fee-invalid"
            )
        if persisted_fee < Decimal(0) or configured_fee < Decimal(0):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="fee-invalid"
            )
        if persisted_fee > BUILDER_FEE_ABSOLUTE_CAP or configured_fee > BUILDER_FEE_ABSOLUTE_CAP:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="fee-above-cap"
            )
        if persisted_fee != configured_fee:
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="fee-mismatch"
            )
        try:
            snapshot = BuilderAttribution(builder=persisted_address.strip(), fee_rate=persisted_fee)
        except (TypeError, ValueError):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="fee-invalid"
            )
        return AttributionDecision(kind=AttributionKind.BUILDER_VALID, attribution=snapshot)


__all__ = [
    "AttributionDecision",
    "AttributionKind",
    "BUILDER_FEE_ABSOLUTE_CAP",
    "BuilderAttribution",
    "BuilderAttributionProvider",
]
