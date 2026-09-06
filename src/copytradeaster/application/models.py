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


class TradeUnavailableError(ApplicationError):
    """One opaque trade ref is missing, foreign, or outside the history scope.

    The same error covers all three cases so existence never leaks across
    accounts; the client shows one safe «unavailable» state with a way back.
    """

    public_code = "trade_unavailable"


class HistoryPageChangedError(ApplicationError):
    """The episode revision moved under an open fills page (C2, plan §23.3).

    The client must restart paging from the first page instead of continuing
    the stale cursor; retrying the same cursor can never succeed.
    """

    public_code = "history_page_changed"


class TradeSplitError(ApplicationError):
    """The requested ref split into several episodes (C2, plan §23.3).

    No single child is picked silently; the client must ask the user which
    surviving episode to open.
    """

    public_code = "trade_split"


class ServiceUnavailableError(ApplicationError):
    public_code = "service_unavailable"


class GlobalEmergencyActiveError(ServiceUnavailableError):
    """A latched global emergency blocks the operation (operator-only reset).

    Distinct public code so clients can explain the block honestly instead
    of a generic outage: the engine is up, but entries are closed by an
    explicit safety latch. Still a 503-class ServiceUnavailableError.
    """

    public_code = "global_emergency_active"


class GlobalPauseActiveError(ServiceUnavailableError):
    """The global admission gate is closed (operator-only resume).

    Distinct public code so clients can tell a closed gate from an outage.
    Still a 503-class ServiceUnavailableError.
    """

    public_code = "global_pause_active"


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


class StatusState(StrEnum):
    """Overall P2.5 System Status verdict (Mini App + Telegram authority).

    Precedence is fixed: ``blocked`` → ``degraded`` → ``stale`` → ``ready``.
    ``ready`` is only allowed when every backend-authoritative evidence —
    usable account, READY system, non-blocked copy, fresh Aster snapshot,
    valid signed permissions — holds at once.
    """

    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class AsterState(StrEnum):
    """Exchange-link state derived from account binding + snapshot freshness.

    No separate Aster ping exists: ``ok`` requires a provably fresh snapshot,
    ``stale`` keeps the last cached snapshot after a refresh failure or TTL
    expiry, ``blocked`` means the account/runtime binding is gone, and
    ``unknown`` means no provable data exists (never shown as healthy).
    """

    OK = "ok"
    STALE = "stale"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class PermissionsState(StrEnum):
    """Agent-permission state from signed validation evidence.

    A cached permission row is never fresh evidence on its own: only a
    signed refresh (or a TTL-bounded recent check) counts as ``valid``.
    A proven rejection is ``blocked``; anything else without proof is
    ``stale``/``unavailable`` with a ``refresh`` remediation.
    """

    VALID = "valid"
    STALE = "stale"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class StatusReason(StrEnum):
    """Closed typed reason behind a non-ready status (never free-form text)."""

    NONE = "none"
    ACCOUNT_NOT_CONNECTED = "account_not_connected"
    ACCOUNT_DISCONNECTED = "account_disconnected"
    SYSTEM_NOT_READY = "system_not_ready"
    SYSTEM_STALE = "system_stale"
    SYSTEM_DEGRADED = "system_degraded"
    ASTER_UNAVAILABLE = "aster_unavailable"
    SNAPSHOT_MISSING = "snapshot_missing"
    SNAPSHOT_STALE = "snapshot_stale"
    PERMISSION_UNAVAILABLE = "permission_unavailable"
    PERMISSION_STALE = "permission_stale"
    PERMISSION_INVALID = "permission_invalid"
    COPY_BLOCKED = "copy_blocked"
    COPY_DEGRADED = "copy_degraded"


class StatusRemediation(StrEnum):
    """Executable remediation set for P2.5 (intentionally tiny).

    ``refresh`` means only a repeated ``status(refresh=True)``; ``open_bot``
    means only the existing ``closeMiniApp()`` return to the Telegram chat.
    The field is ``None`` when no action is required (fresh ``ready``).
    """

    REFRESH = "refresh"
    OPEN_BOT = "open_bot"


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
    # P2.6 shared Builder verdict: True only for a current authoritative
    # ``verified`` attestation from the same truth as AccountBuilderDTO.
    builder_verified: bool = False
    # S1.4 account aggregate over one consistent snapshot; None when the
    # snapshot is missing. Additive: old consumers ignore it.
    overview: AccountOverview | None = None


@dataclass(frozen=True, slots=True)
class StatusDTO:
    """Public P2.5 System Status contract (Mini App + Telegram authority).

    Every field is a backend-authoritative fact: the frontend formats time
    but never decides health from age.  The DTO carries no raw message,
    no ``aster_connected`` boolean, no ambiguous ``last_refresh_at`` — only
    typed states, a closed reason, one executable remediation, masked
    identities, and evidence timestamps.  ``last_check_at`` is the oldest
    mandatory evidence (conservative), never the time of a failed attempt.
    """

    state: StatusState = StatusState.BLOCKED
    system_state: SystemState = SystemState.UNKNOWN
    copy_state: CopyState = CopyState.EMPTY
    aster_state: AsterState = AsterState.UNKNOWN
    permissions_state: PermissionsState = PermissionsState.UNAVAILABLE
    account_masked: str | None = None
    signer_masked: str | None = None
    positions_count: int | None = None
    permissions: PermissionSummary = field(default_factory=PermissionSummary)
    snapshot_captured_at: datetime | None = None
    last_check_at: datetime | None = None
    age_seconds: int | None = None
    reason: StatusReason = StatusReason.SYSTEM_NOT_READY
    remediation: StatusRemediation | None = None
    # P2.6 shared Builder verdict, same truth as DashboardDTO/AccountBuilderDTO.
    builder_verified: bool = False


RISK_MIN = Decimal("0.005")
RISK_MAX = Decimal("0.05")
RISK_STEP = Decimal("0.005")
RISK_PRESETS: tuple[Decimal, ...] = (
    Decimal("0.01"),
    Decimal("0.02"),
    Decimal("0.03"),
    Decimal("0.05"),
)
RISK_BASE = "available_balance"


@dataclass(frozen=True, slots=True)
class RiskDTO:
    """Public Mini App risk contract (P2.4 — Risk on Stop).

    The only follower-controlled numeric setting is ``risk_percent``.
    ``min``/``max``/``step``/``presets`` are backend authority: the
    frontend renders them but never defines policy.  Legacy
    ``max_leverage``/``max_entry_deviation_pct`` stay in the internal
    ``RiskPolicy``/storage/engine but are no longer part of this public DTO.
    All values are Decimals; the transport serializes them as strings.
    """

    risk_percent: Decimal
    min: Decimal = RISK_MIN
    max: Decimal = RISK_MAX
    step: Decimal = RISK_STEP
    presets: tuple[Decimal, ...] = RISK_PRESETS
    risk_base: str = RISK_BASE


@dataclass(frozen=True, slots=True)
class RiskPreviewStep:
    """Server-computed money example for one allowed risk percent.

    The frontend switches these ready rows while the ruler moves instead of
    requesting per-pixel math. ``budget_usdt`` is the exact position-sizing
    budget (available × percent), not a guaranteed maximum loss.
    """

    risk_percent: Decimal
    budget_usdt: Decimal


@dataclass(frozen=True, slots=True)
class RiskPreviewDTO:
    """Money examples for every allowed risk step over one snapshot.

    Empty steps with ``available_usdt=None`` mean the balance is not
    available: the screen shows its fallback copy instead of saving or
    displaying a zero example. A stale snapshot still yields examples,
    labeled by ``freshness``.
    """

    available_usdt: Decimal | None
    steps: tuple[RiskPreviewStep, ...]
    snapshot_captured_at: datetime | None
    freshness: SnapshotFreshness


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


class HistoryQuality(StrEnum):
    """Episode completeness as proven by the history layer.

    ``reconciling`` is reserved for a future import-progress state; only
    proven values are emitted, never a guess.
    """

    COMPLETE = "complete"
    PARTIAL_HISTORY = "partial_history"


class TradeState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class EpisodeSource(StrEnum):
    COPY = "copy"
    EXTERNAL = "external"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SnapshotFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class RiskCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TradeListItem:
    """One follower episode for the closed/open lists.

    Money travels as canonical Decimal strings; a zero result and an
    unavailable one stay distinct. ``executed_qty`` is the total closed
    volume for a closed episode and the remainder for an open one; it is
    None only when the remainder was never proven (open placeholder).
    ``side`` is None when the direction was never proven. ``net_pnl_usdt``
    stays None until full cost attribution lands (S1.6); only the proven
    gross is shown before that.
    """

    trade_ref: str
    symbol: str
    quantity_asset: str | None
    side: PositionSide | None
    state: TradeState
    source: EpisodeSource
    opened_at: datetime | None
    closed_at: datetime | None
    executed_qty: str | None
    realized_pnl_gross_usdt: str
    net_pnl_usdt: str | None
    quality: HistoryQuality


@dataclass(frozen=True, slots=True)
class TradeListPage:
    items: tuple[TradeListItem, ...] = ()
    next_cursor: str | None = None
    as_of: datetime | None = None
    coverage_from: datetime | None = None
    coverage_complete: bool = False


@dataclass(frozen=True, slots=True)
class FillFacts:
    """One episode share of one exchange execution (C2, plan §23.3).

    Ids stay strings, never JS numbers. ``quantity``/``price``/``realized_pnl``
    are the source exchange facts; ``allocated_quantity``/``role``/
    ``allocated_realized_pnl``/``position_side`` are this episode's proven
    share. A shared flip fill appears once per episode with only its share;
    shares of one fill sum to the source fill.
    """

    trade_id: str
    order_id: str
    side: str
    quantity: str
    price: str
    realized_pnl: str
    executed_at: datetime
    allocated_quantity: str
    role: str
    allocated_realized_pnl: str
    position_side: str


@dataclass(frozen=True, slots=True)
class TradeEventItem:
    """Structured user event: the frontend formats copy, never facts."""

    event_ref: str
    kind: str
    trade_ref: str | None
    occurred_at: datetime
    facts: dict[str, str | int | bool | None]


@dataclass(frozen=True, slots=True)
class TradeEventsPage:
    """One episode's fills plus its user events, paged independently.

    ``fills_cursor``/``events_cursor`` are the positions for the next
    request (None when that array is exhausted); ``next_fills_cursor``/
    ``next_events_cursor`` repeat the same values under explicit names and
    legacy ``next_cursor`` repeats the events position.
    """

    fills: tuple[FillFacts, ...] = ()
    events: tuple[TradeEventItem, ...] = ()
    next_cursor: str | None = None
    fills_cursor: str | None = None
    events_cursor: str | None = None
    next_fills_cursor: str | None = None
    next_events_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class PendingOperation:
    """One unsettled journal command, without ids or payloads."""

    symbol: str | None
    operation: str
    state: str
    age_seconds: int


@dataclass(frozen=True, slots=True)
class ActivityFeedPage:
    items: tuple[TradeEventItem, ...] = ()
    next_cursor: str | None = None
    high_water: int = 0
    pending: tuple[PendingOperation, ...] = ()


@dataclass(frozen=True, slots=True)
class TradeDetailDTO:
    """One episode with its money breakdown and provenance.

    ``result_state`` names the finality explicitly: only the proven gross
    is shown until full cost attribution lands.
    """

    trade: TradeListItem
    entry_qty: str
    exit_qty: str
    remaining_qty: str | None
    average_entry: str | None
    average_exit: str | None
    duration_ms: int | None
    revision: int
    position_side: str
    result_state: str = "gross_only"


@dataclass(frozen=True, slots=True)
class AccountOverview:
    """Dashboard aggregate over one consistent snapshot.

    No summed ``risk_to_stop`` total: the per-position estimate is not a
    guaranteed loss bound (plan §5), so the aggregate carries counts and
    the proven unrealized total only.
    """

    as_of: datetime | None
    freshness: SnapshotFreshness
    open_count: int | None
    unrealized_pnl_usdt: str | None
    available_usdt: str | None
    protected_count: int | None
    risk_coverage: RiskCoverage


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
    # S1.4 resume facts for the confirmation copy; None for other actions.
    # ``master_in_position=None`` means the master read was unavailable, not
    # flat; ``permission_ok=None`` means no cached proof either way.
    resume_context: ResumeContextDTO | None = None


@dataclass(frozen=True, slots=True)
class ResumeContextDTO:
    """Proven resume facts so the confirmation explains the real outcome.

    Replaces the hardcoded «master is in position» claim: the client picks
    its copy from these facts. Snapshot reads are cached-only here; the
    enforcing fresh checks still happen inside the resume workflow.
    """

    master_in_position: bool | None
    snapshot_freshness: SnapshotFreshness
    permission_ok: bool | None


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


class AccountBuilderState(StrEnum):
    """Authoritative Builder attestation verdict for the Account screen.

    The durable snapshot is last-known evidence only: ``verified`` requires
    a fresh combined signed Agent + Builder check (or a TTL-bounded recent
    one).  ``stale`` keeps showing the snapshot after a transient failure;
    ``blocked`` is a proven negative; ``unavailable`` has no usable snapshot;
    ``not_applicable`` is a direct (non-Builder) account.  There is no
    ``pending`` — that belongs to the onboarding flow.
    """

    VERIFIED = "verified"
    EXPIRING = "expiring"
    STALE = "stale"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class AccountBuilderReason(StrEnum):
    """Closed typed reason behind a non-verified attestation."""

    NONE = "none"
    DIRECT_ACCOUNT = "direct_account"
    ACCOUNT_DISCONNECTED = "account_disconnected"
    AGENT_INVALID = "agent_invalid"
    AGENT_EXPIRING = "agent_expiring"
    BUILDER_NOT_CONFIRMED = "builder_not_confirmed"
    BUILDER_MISMATCH = "builder_mismatch"
    FEE_MISMATCH = "fee_mismatch"
    TRANSIENT_UNAVAILABLE = "transient_unavailable"
    SNAPSHOT_MISSING = "snapshot_missing"
    IDENTITY_CHANGED = "identity_changed"


class AccountBuilderRemediation(StrEnum):
    """Executable remediation set for the Account + Builder screen.

    ``refresh`` re-runs ``account_builder(refresh=True)``; ``reauthorize``
    is the only signal that shows the re-authorize action.  The field is
    ``None`` when no action applies.  The frontend never decides on its own.
    """

    REFRESH = "refresh"
    REAUTHORIZE = "reauthorize"


@dataclass(frozen=True, slots=True)
class AccountBuilderDTO:
    """Public Account + Builder attestation contract (P2.6).

    Only masked identities and the actual authorized fee cap reach the UI.
    Full addresses, keys, signatures, nonces, raw Aster payloads and
    internal exception text never enter this DTO.
    """

    identity_source: str = "direct"
    state: AccountBuilderState = AccountBuilderState.UNAVAILABLE
    reason: AccountBuilderReason = AccountBuilderReason.SNAPSHOT_MISSING
    remediation: AccountBuilderRemediation | None = AccountBuilderRemediation.REFRESH
    account_masked: str | None = None
    signer_masked: str | None = None
    builder_address_masked: str | None = None
    builder_name: str | None = None
    max_fee_rate: str | None = None
    permission: PermissionSummary = field(default_factory=PermissionSummary)
    agent_expires_at_ms: int | None = None
    verified_at: datetime | None = None
    last_check_at: datetime | None = None
    age_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class BuilderReauthorizedDTO:
    """Safe result of a same-signer Builder re-approval (always paused)."""

    account_id: str
    account_masked: str
    signer_masked: str
    builder_name: str
    max_fee_rate: str
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

    async def activity(
        self, user_id: int, page: int = 0, filter: ActivityFilter = ActivityFilter.ALL
    ) -> ActivityPage: ...

    async def trades(
        self,
        user_id: int,
        *,
        period: str = "7d",
        cursor: str | None = None,
        limit: int | None = None,
        symbol: str | None = None,
    ) -> TradeListPage: ...

    async def trade(self, user_id: int, trade_ref: str) -> TradeDetailDTO: ...

    async def trade_events(
        self,
        user_id: int,
        trade_ref: str,
        *,
        cursor: int = 0,
        limit: int | None = None,
        fills_cursor: str | None = None,
        events_cursor: int | str | None = None,
        fills_limit: int | None = None,
        events_limit: int | None = None,
    ) -> TradeEventsPage: ...

    async def activity_feed(
        self,
        user_id: int,
        *,
        filter: ActivityFilter = ActivityFilter.ALL,
        cursor: int = 0,
        limit: int | None = None,
    ) -> ActivityFeedPage: ...

    async def dashboard(self, user_id: int, refresh: bool = False) -> DashboardDTO: ...
    async def positions(self, user_id: int, refresh: bool = False) -> PositionPageDTO: ...
    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO: ...
    async def risk(self, user_id: int) -> RiskDTO: ...
    async def risk_preview(self, user_id: int) -> RiskPreviewDTO: ...

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

    async def account_builder(self, user_id: int, refresh: bool = False) -> AccountBuilderDTO: ...

    async def builder_reauthorize_prepare(self, user_id: int) -> BuilderPrepareDTO: ...

    async def builder_reauthorize_submit(
        self, user_id: int, pending_id: str, signature: str
    ) -> BuilderReauthorizedDTO: ...

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
