"""Transport-neutral Stage 2 DTOs, errors, and application protocol.

This module deliberately contains no aiogram, Telegram, Aster, storage, or
concrete service implementation.  Telegram and a future Mini App can share
these use-case contracts without sharing presenter models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from copytradeaster.onboarding.errors import OnboardingErrorCode


class ApplicationError(Exception):
    """Base error safe to classify at a transport boundary."""

    public_code = "application_error"


class AuthorizationError(ApplicationError):
    public_code = "not_authorized"


class ValidationError(ApplicationError):
    public_code = "invalid_input"


class OnboardingApplicationError(ApplicationError):
    """Safe application-boundary classification for direct onboarding failures.

    The underlying onboarding service may know an exchange status code, but
    the Bot/Mini App boundary receives only this stable enum.  Presenters map
    it to localised, actionable copy and never parse exception strings.
    """

    public_code = "onboarding_error"

    def __init__(self, code: OnboardingErrorCode) -> None:
        if not isinstance(code, OnboardingErrorCode):
            raise TypeError("onboarding error code is invalid")
        self.code = code
        super().__init__(code.value)

    def __repr__(self) -> str:
        return f"OnboardingApplicationError(code={self.code.value!r})"


class NotReadyError(ApplicationError):
    public_code = "not_ready"


class StaleOperationError(ApplicationError):
    public_code = "stale_operation"


class ConfirmationExpiredError(ApplicationError):
    public_code = "confirmation_expired"


class ServiceUnavailableError(ApplicationError):
    public_code = "service_unavailable"


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class AccessRequestStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class CopyState(StrEnum):
    EMPTY = "empty"
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class MiniAppAccessState(StrEnum):
    """Server-derived access result for the Mini App session gate."""

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SystemState(StrEnum):
    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ConfirmationAction(StrEnum):
    RESUME = "resume"
    EMERGENCY = "emergency"
    DISCONNECT = "disconnect"
    GLOBAL_RESUME = "global_resume"
    GLOBAL_PAUSE = "global_pause"
    GLOBAL_EMERGENCY = "global_emergency"


class MiniAppConnectionState(StrEnum):
    EMPTY = "empty"
    ONBOARDING = "onboarding"
    RECOVERY_REQUIRED = "recovery_required"
    CONNECTED = "connected"


@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: int
    chat_id: int
    role: UserRole = UserRole.USER
    account_id: str | None = None
    allowed: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


@dataclass(frozen=True, slots=True)
class PermissionSummary:
    can_read: bool | None = None
    can_perp_trade: bool | None = None
    can_withdraw: bool | None = None
    whitelist_ok: bool | None = None
    checked_at: datetime | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardDTO:
    copy_state: CopyState = CopyState.EMPTY
    system_state: SystemState = SystemState.UNKNOWN
    account_id: str | None = None
    account_masked: str | None = None
    signer_masked: str | None = None
    risk_percent: Decimal | None = None
    max_leverage: int | None = None
    open_positions: int | None = None
    updated_at: datetime | None = None
    snapshot_captured_at: datetime | None = None
    message: str | None = None
    permission: PermissionSummary = field(default_factory=PermissionSummary)
    notification_enabled: bool = True
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class StatusDTO:
    system_state: SystemState = SystemState.UNKNOWN
    copy_state: CopyState = CopyState.EMPTY
    aster_connected: bool = False
    account_masked: str | None = None
    positions_count: int | None = None
    permissions: PermissionSummary = field(default_factory=PermissionSummary)
    last_refresh_at: datetime | None = None
    age_seconds: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class RiskDTO:
    risk_percent: Decimal
    max_leverage: int
    max_entry_deviation_pct: Decimal
    risk_base: str = "available_balance"


class ActivityCategory(StrEnum):
    """High-level grouping for the read-only Activity timeline.

    ``system`` covers preparation and engine state changes (e.g. leverage
    control plane); ``trade`` covers order-affecting actions such as entries,
    closes, and protective orders.
    """

    TRADE = "trade"
    SYSTEM = "system"


class ActivityStatus(StrEnum):
    """Terminal or in-flight state exposed to the Mini App.

    No raw ``execution_commands.status`` code is ever surfaced; the backend
    collapses PENDING/SENDING/SUBMITTED into ``pending`` (with a bounded
    ``pending_age``), CONFIRMED into ``completed``, and UNKNOWN/FAILED into
    ``failed`` (with a typed remediation copy).
    """

    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"


class ActivityFilter(StrEnum):
    """Filter values accepted by the activity use case and HTTP endpoint."""

    ALL = "all"
    TRADE = "trade"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ActivityItem:
    """Safe Activity DTO shown in the Mini App timeline.

    Only normalized values reach the UI.  ``command_key``, ``account_id``,
    addresses, nonces, ``client_order_id``, raw Aster codes, the journal
    ``payload`` JSON, and any internal exception text are filtered server-side
    and never serialized into a Mini App response.
    """

    category: ActivityCategory
    title: str
    status: ActivityStatus
    symbol: str | None = None
    occurred_at: datetime | None = None
    pending_age: int | None = None
    remediation: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityPage:
    items: tuple[ActivityItem, ...] = ()
    page: int = 0
    has_previous: bool = False
    has_next: bool = False
    filter: ActivityFilter = ActivityFilter.ALL


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionProtectionState(StrEnum):
    PROTECTED = "protected"
    MISSING_STOP = "missing_stop"
    PARTIAL_STOP = "partial_stop"
    DUPLICATE_STOP = "duplicate_stop"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class PositionSummaryDTO:
    symbol: str
    side: PositionSide
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_profit: Decimal
    leverage: int
    protection_state: PositionProtectionState
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    risk_to_stop_usdt: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PositionPageDTO:
    positions: tuple[PositionSummaryDTO, ...] = ()
    count: int = 0
    system_state: SystemState = SystemState.UNKNOWN
    snapshot_captured_at: datetime | None = None
    is_stale: bool = False
    message: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationSettingsDTO:
    enabled: bool = True
    last_delivery_at: datetime | None = None
    pending: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class DirectCredentialsInput:
    user_address: str
    signer_address: str
    signer_private_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RiskUpdateInput:
    risk_percent: Decimal | None = None
    max_leverage: int | None = None
    max_entry_deviation_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Confirmation:
    token: str
    action: ConfirmationAction
    account_id: str | None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    account_id: str
    account_masked: str
    signer_masked: str
    paused: bool = True
    message: str | None = None


@dataclass(frozen=True, slots=True)
class AdminDashboardDTO:
    system_state: SystemState = SystemState.UNKNOWN
    global_pause: bool = False
    global_emergency: bool = False
    accounts_count: int = 0
    outbox_pending: int = 0
    outbox_failed: int = 0
    pending_access_requests: int = 0
    master_account_masked: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class AdminUserItem:
    user_id: int
    account_connected: bool = False
    notifications_enabled: bool = True
    access_enabled: bool = True
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class AdminUserPage:
    items: tuple[AdminUserItem, ...] = ()
    page: int = 0
    has_previous: bool = False
    has_next: bool = False


@dataclass(frozen=True, slots=True)
class AdminFollowerItem:
    account_id: str
    owner_user_id: int | None = None
    account_masked: str | None = None
    copy_state: CopyState = CopyState.DEGRADED
    risk_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AdminFollowerPage:
    items: tuple[AdminFollowerItem, ...] = ()
    page: int = 0
    has_previous: bool = False
    has_next: bool = False


@dataclass(frozen=True, slots=True)
class AccessRequestDTO:
    request_id: int
    user_id: int
    chat_id: int
    status: AccessRequestStatus
    username: str | None = None
    display_name: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decision_changed: bool = False
    # True only for the call that atomically inserted the PENDING row.  The
    # Telegram presenter uses this to send one direct admin card even when
    # duplicate captcha callbacks race each other.
    created: bool = field(default=False, compare=False)


@dataclass(frozen=True, slots=True)
class AdminAccessRequestPage:
    items: tuple[AccessRequestDTO, ...] = ()
    page: int = 0
    has_previous: bool = False
    has_next: bool = False


@dataclass(frozen=True, slots=True)
class MiniAppSessionDTO:
    """Transport-neutral Mini App session result.

    The Telegram initData check happens at the transport; this DTO carries
    only the server-derived outcome and never a chat identity or secret.
    """

    user_id: int
    access_state: MiniAppAccessState = MiniAppAccessState.UNKNOWN
    environment: str = "testnet"
    connection_state: MiniAppConnectionState = MiniAppConnectionState.EMPTY
    account_connected: bool = False
    account_masked: str | None = None
    copy_state: CopyState = CopyState.EMPTY
    is_admin: bool = False
    recovery_reason: str | None = None
    onboarding_state: str | None = None


@dataclass(frozen=True, slots=True)
class BuilderPrepareDTO:
    """Review payload shown to the user before the wallet signature.

    ``already_connected`` reports a recovered runtime after a previous commit
    whose activation failed; in that case ``pending_id`` is None and the
    presenter routes straight to the connected home instead of the wizard.
    """

    pending_id: str | None
    already_connected: bool = False
    user_masked: str | None = None
    agent_masked: str | None = None
    builder_masked: str | None = None
    builder_name: str = ""
    max_fee_rate: str = ""
    signature_plan: str = "combined_approve_agent"
    can_perp_trade: bool = True
    can_withdraw: bool = False
    agent_expires_at_ms: int = 0
    approval_expires_at_ms: int = 0
    eip712: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BuilderOnboardedDTO:
    account_id: str
    account_masked: str
    signer_masked: str
    builder_name: str
    max_fee_rate: str
    position_mode: str
    available_usdt: Decimal
    agent_expires_at_ms: int
    paused: bool = True
    message: str | None = None


class ApplicationProtocol(Protocol):
    """Shared application boundary; implementations live outside Telegram."""

    async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext: ...

    async def access_request(self, user_id: int) -> AccessRequestDTO | None: ...

    async def submit_access_request(
        self,
        user_id: int,
        chat_id: int,
        *,
        username: str | None,
        display_name: str | None,
    ) -> AccessRequestDTO: ...

    async def dashboard(self, user_id: int, refresh: bool = False) -> DashboardDTO: ...
    async def positions(self, user_id: int, refresh: bool = False) -> PositionPageDTO: ...
    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO: ...

    async def risk(self, user_id: int) -> RiskDTO: ...

    async def activity(
        self, user_id: int, page: int = 0, filter: ActivityFilter = ActivityFilter.ALL
    ) -> ActivityPage: ...

    async def notification_settings(self, user_id: int) -> NotificationSettingsDTO: ...

    async def set_notifications(self, user_id: int, enabled: bool) -> NotificationSettingsDTO: ...

    async def connect_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult: ...

    async def replace_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult: ...

    async def miniapp_session(self, user_id: int) -> MiniAppSessionDTO: ...

    async def miniapp_recover_runtime(self, user_id: int) -> MiniAppSessionDTO: ...

    async def builder_prepare(self, user_id: int, user_address: str) -> BuilderPrepareDTO: ...

    async def builder_submit(
        self, user_id: int, pending_id: str, signature: str
    ) -> BuilderOnboardedDTO: ...

    async def builder_resume_attempt(self, user_id: int) -> BuilderOnboardedDTO: ...

    async def update_risk(self, user_id: int, values: RiskUpdateInput) -> RiskDTO: ...

    async def pause(self, user_id: int) -> DashboardDTO: ...

    async def resume(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> DashboardDTO: ...

    async def begin_confirmation(
        self, user_id: int, action: ConfirmationAction, account_id: str | None = None
    ) -> Confirmation: ...

    async def confirm_action(self, user_id: int, confirmation: Confirmation) -> DashboardDTO: ...

    async def admin_dashboard(self, user_id: int) -> AdminDashboardDTO: ...

    async def admin_users(self, user_id: int, page: int = 0) -> AdminUserPage: ...

    async def admin_allow_user(self, user_id: int, target_user_id: int) -> AdminUserPage: ...

    async def admin_set_user_access(
        self, user_id: int, target_user_id: int, enabled: bool
    ) -> AdminUserPage: ...

    async def admin_access_requests(
        self, user_id: int, page: int = 0
    ) -> AdminAccessRequestPage: ...

    async def admin_decide_access(
        self, user_id: int, request_id: int, *, approve: bool
    ) -> AccessRequestDTO: ...

    async def admin_followers(self, user_id: int, page: int = 0) -> AdminFollowerPage: ...

    async def admin_reconcile(
        self, user_id: int, account_id: str | None = None
    ) -> AdminDashboardDTO: ...

    async def admin_pause(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO: ...

    async def admin_resume(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO: ...

    async def admin_emergency(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO: ...
