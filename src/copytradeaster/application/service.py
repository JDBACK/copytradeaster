"""Transport-neutral application service for Telegram and future clients.

The service deliberately depends on small repository/runtime ports.  The
current :class:`copytradeaster.app.App` can be wrapped by composition code,
while tests and a future Mini App can provide faithful adapters without
importing Telegram types here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import inspect
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Protocol

from copytradeaster.application.models import (
    RISK_BASE,
    RISK_MAX,
    RISK_MIN,
    RISK_PRESETS,
    RISK_STEP,
    AccessRequestDTO,
    AccessRequestStatus,
    AccountBuilderDTO,
    AccountBuilderReason,
    AccountBuilderRemediation,
    AccountBuilderState,
    AccountOverview,
    ActivityCategory,
    ActivityFeedPage,
    ActivityFilter,
    ActivityItem,
    ActivityPage,
    ActivityStatus,
    AdminAccessRequestPage,
    AdminDashboardDTO,
    AdminFollowerItem,
    AdminFollowerPage,
    AdminUserItem,
    AdminUserPage,
    AsterState,
    AuthorizationError,
    BuilderOnboardedDTO,
    BuilderPrepareDTO,
    BuilderReauthorizedDTO,
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    CopyState,
    DashboardDTO,
    DirectCredentialsInput,
    GlobalEmergencyActiveError,
    GlobalPauseActiveError,
    HistoryPageChangedError,
    MiniAppAccessState,
    MiniAppConnectionState,
    MiniAppSessionDTO,
    NotificationSettingsDTO,
    OnboardingApplicationError,
    OnboardingResult,
    PendingOperation,
    PermissionsState,
    PermissionSummary,
    PositionPageDTO,
    PositionProtectionState,
    PositionSide,
    PositionSummaryDTO,
    ResumeContextDTO,
    RiskDTO,
    RiskPreviewDTO,
    RiskUpdateInput,
    ServiceUnavailableError,
    SnapshotFreshness,
    StaleOperationError,
    StatusDTO,
    StatusReason,
    StatusRemediation,
    StatusState,
    SystemState,
    TradeDetailDTO,
    TradeEventsPage,
    TradeListPage,
    TradeSplitError,
    TradeUnavailableError,
    UserContext,
    UserRole,
    ValidationError,
)
from copytradeaster.application.trade_views import (
    build_overview,
    feed_kinds,
    present_allocated_fill,
    present_event,
    present_trade,
    present_trade_detail,
    quantity_assets,
    resolve_owned_ref,
    risk_budget_steps,
)
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.history.accounting import EpisodeMoney, load_episode_money
from copytradeaster.history.models import TradeResolutionDTO
from copytradeaster.logging_utils import get_logger, log_control_audit, log_onboarding_audit
from copytradeaster.onboarding.builder import validate_builder_record
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderPrepareRequest,
    BuilderSubmitRequest,
    DirectOnboardingRequest,
    DirectOnboardingResult,
    mask_address,
    stable_account_id,
)
from copytradeaster.storage.episodes import EpisodeRepository, EpisodeRevisionConflict
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    CommandRepo,
    NotificationOutboxRepo,
    SettingsRepo,
    TelegramAccessRequestRepo,
    TelegramUserRepo,
)

TESTNET_REST_URL = "https://fapi.asterdex-testnet.com"
TESTNET_WS_URL = "wss://fstream.asterdex-testnet.com/ws"
PERMISSION_TTL_SECONDS = 60
# P2.6: an Agent authorization this close to expiry needs re-approval.
# The value is backend policy — the frontend never computes it.
BUILDER_EXPIRY_WARNING_MS = 7 * 24 * 60 * 60 * 1000
TESTNET_PERMISSION_FALLBACK_TTL_SECONDS = 86_400
ACTIVITY_PAGE_SIZE = 10
TRADE_PAGE_SIZE = 20
ADMIN_PAGE_SIZE = 10
ACCESS_REQUEST_RETRY_COOLDOWN_SECONDS = 60
# The token is embedded together with action/revision in callback_data by the
# presenter.  Keep a 16-byte ASCII budget so the longest action still remains
# comfortably below Telegram's 64-byte callback_data ceiling.
CONFIRMATION_TOKEN_MAX_BYTES = 16
_MISSING = object()
_TRADE_PERIODS = {"7d": 7, "30d": 30, "all": None}
_PENDING_OPERATIONS = {
    "MARKET_OPEN": "entry",
    "MARKET_CLOSE": "exit",
    "PLACE_STOP": "protection",
    "PLACE_TP": "protection",
    "CANCEL_ORDER": "cancel",
    "SET_LEVERAGE": "leverage",
}


def _payload_symbol(payload: Any) -> str | None:
    """Command symbol for the pending block; never the payload itself."""
    parsed = _parse_activity_payload(payload)
    symbol = _safe_activity_symbol(parsed)
    return symbol


_SAFE_CODE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAFE_ERRORS = (
    AuthorizationError,
    ValidationError,
    OnboardingApplicationError,
    ConfirmationExpiredError,
    StaleOperationError,
    ServiceUnavailableError,
    TradeUnavailableError,
    HistoryPageChangedError,
    TradeSplitError,
)


_CONNECTIVITY_ASTER_CODES = frozenset({-1000, -1006, -1007})

# Degraded reasons that describe the engine's own scheduled re-verification and
# reconnect churn rather than a fault. ``_establish_master_boundary`` fail-closes
# every few sweeps, rebuilds the master against REST and proves convergence again
# within seconds; a master stream blip does the same while it re-listens. Their
# only real consequence is withheld entry admission, which the trading gate
# (``allow_new_entries`` / ``_entry_admission_ready``) already enforces on its
# own. Presenting them as DEGRADED made Dashboard, Positions and Status flash
# "requires attention" for work that is routine and self-resolving.
ROUTINE_MAINTENANCE_REASONS = frozenset(
    {
        "periodic-snapshot",
        "master-dirty",
        "stream-connected",
        "stream-disconnected",
        "stream-reconnecting",
        "stream-startup-timeout",
        "master-changed-before-ready",
        "master-boundary-not-quiescent",
    }
)
# ``master-recovery:<reason>:<category>`` inherits its reason from whatever made
# the projector dirty, so it is always self-healing by construction.
ROUTINE_MAINTENANCE_PREFIXES = ("master-recovery:",)
# A routine window that outlives this is no longer routine: the engine is stuck
# in a fail-closed state and the user has to know, however inoffensive the
# reason looks.
ROUTINE_DEGRADED_GRACE_MS = 20_000


def _is_routine_maintenance(reason: str) -> bool:
    """True while the engine is only re-proving itself."""
    return reason in ROUTINE_MAINTENANCE_REASONS or reason.startswith(ROUTINE_MAINTENANCE_PREFIXES)


def _is_connectivity_error(exc: BaseException) -> bool:
    """Transport-classified failures only; app bugs never read as outages.

    Mirrors the executor outcome-unknown taxonomy (timeout/gateway codes and
    5xx) plus stdlib transport errors. A 4xx (auth, validation) or a local
    bug is not a lost exchange connection and records no event.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if not isinstance(exc, AsterError):
        return False
    if exc.code in _CONNECTIVITY_ASTER_CODES:
        return True
    status = exc.http_status
    if isinstance(status, int) and not isinstance(status, bool) and status >= 500:
        return True
    message = exc.msg.lower() if isinstance(exc.msg, str) else ""
    return exc.code == 0 and ("timeout" in message or "timed out" in message)


log = get_logger(__name__)


class Stage2AccountLifecycle(Protocol):
    """Locked account lifecycle used by replacement and disconnect.

    The concrete adapter owns the lock order (follower lock, then executor
    mutation lock).  ``assert_safe_to_remove`` is authoritative and must check
    flat positions, open orders, and in-flight journal work.  Publication is a
    compare-and-swap against the runtime captured by the context.
    """

    async def assert_safe_to_remove(self) -> None: ...

    async def publish_replacement(self, replacement: Any) -> None: ...

    async def unpublish_after_commit(self) -> None: ...


class Stage2RuntimeAdapter(Protocol):
    """Narrow runtime boundary for the transport-neutral application service.

    Every method has one meaning.  In particular, adapters must not implement
    these operations as optional no-ops: a missing method is an integration
    error and the service fails closed before changing durable state.
    """

    testnet: bool
    ready: bool

    async def is_account_registered(self, account_id: str) -> bool: ...

    async def account_version(self, account_id: str) -> str | None:
        """Return a safe version marker that changes with credential replacement."""

    async def recover_account_runtime(
        self,
        account_id: str,
        user_address: str,
        signer_address: str,
    ) -> bool:
        """Publish an enabled paused account after a prior activation failure."""

    async def cached_snapshot(self, account_id: str) -> Any | None: ...

    async def refresh_snapshot(self, account_id: str) -> Any: ...

    async def pause_account(self, account_id: str) -> None:
        """Drain admitted work after the application persisted account pause."""

    async def resume_account(self, account_id: str) -> None:
        """Reopen account admission after the application persisted resume."""

    async def reconcile_account(self, account_id: str) -> None: ...

    async def reconcile_all(self) -> None: ...

    async def pause_entries(self, timeout_s: float = 5.0) -> None:
        """Set global pause, close generation admission, and drain it."""

    async def resume_entries(self) -> None:
        """Reopen global generation admission after durable resume."""

    async def reopen_entries_while_paused(self) -> None:
        """Reopen reductions/protection while the durable entry gate stays false."""

    async def refresh_agent_permission(self, account_id: str) -> AgentValidation: ...

    async def refresh_builder_record(self, account_id: str) -> list[Any]:
        """Signed GET /fapi/v3/builder records for the current identity."""
        ...

    async def permission_override_allowed(self, account_id: str) -> bool: ...

    async def validate_resume_safety(self, account_id: str, snapshot: Any) -> None:
        """Accept managed bot state; reject manual/external state."""

    def follower_lifecycle(
        self, account_id: str
    ) -> AbstractAsyncContextManager[Stage2AccountLifecycle]: ...

    async def commit_disconnect(self, account_id: str) -> None:
        """Atomically disable policy/account, delete credentials, and cache."""

    async def build_replacement_runtime(
        self, account_id: str, request: DirectOnboardingRequest, preflight: Any
    ) -> Any: ...

    async def master_has_open_positions(self) -> bool: ...


class EmergencyControllerPort(Protocol):
    """The existing :class:`EmergencyController` public methods only."""

    async def emergency_account(
        self, account_id: str, *, operation_id: str | None = None
    ) -> Any: ...

    async def emergency_global(
        self, account_ids: Iterable[str], *, operation_id: str | None = None
    ) -> Any: ...


class OnboardingPort(Protocol):
    async def preflight(self, request: DirectOnboardingRequest) -> Any: ...

    async def onboard(self, request: DirectOnboardingRequest) -> Any: ...

    async def replace(self, request: DirectOnboardingRequest, *, preflight: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class _ConfirmationRecord:
    user_id: int
    action: ConfirmationAction
    account_id: str | None
    account_version: str | None
    expires_at: datetime


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000
        try:
            parsed = datetime.fromtimestamp(raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_code(value: Any, fallback: str = "unknown", limit: int = 64) -> str:
    text = _SAFE_CODE.sub("_", str(value or ""))[:limit].strip("_")
    return text or fallback


def _parse_activity_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return {str(key): value for key, value in payload.items()}
    if not isinstance(payload, str) or not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    if isinstance(parsed, Mapping):
        return {str(key): value for key, value in parsed.items()}
    return {}


def _safe_activity_symbol(payload: Mapping[str, Any]) -> str | None:
    raw = payload.get("symbol")
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    if not text or len(text) > 24 or not all(ch.isalnum() or ch in "-_/" for ch in text):
        return None
    return text


def _coerce_activity_filter(value: Any) -> ActivityFilter:
    if isinstance(value, ActivityFilter):
        return value
    if isinstance(value, str):
        try:
            return ActivityFilter(value.strip().lower())
        except ValueError:
            pass
    raise ValidationError("activity filter is invalid")


_FEED_CURSOR_PREFIX = "v1:"
_FEED_CURSOR_FILTERS = ("all", "trade", "system")


def _encode_feed_cursor(
    *,
    scope: HistoryScope,
    filter_value: str,
    high_water: int,
    last_occurred_at: int,
    last_seq: int,
) -> str:
    """Opaque v1 feed cursor: scope token, filter, high water, last tuple."""
    payload = {
        "filter": filter_value,
        "high_water": high_water,
        "last_occurred_at": last_occurred_at,
        "last_seq": last_seq,
        "scope": f"{scope.account_id}:{scope.environment}",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _FEED_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_feed_cursor(cursor: str) -> tuple[str, str, int, int, int]:
    """Split a v1 feed cursor; any malformed input is a plain ValueError."""
    if not isinstance(cursor, str) or not cursor.startswith(_FEED_CURSOR_PREFIX):
        raise ValueError("feed cursor version is invalid")
    encoded = cursor[len(_FEED_CURSOR_PREFIX) :]
    if not encoded:
        raise ValueError("feed cursor is invalid")
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("feed cursor is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("feed cursor is invalid")
    scope_token = payload.get("scope")
    filter_value = payload.get("filter")
    high_water = payload.get("high_water")
    last_occurred_at = payload.get("last_occurred_at")
    last_seq = payload.get("last_seq")
    if not isinstance(scope_token, str) or not scope_token or ":" not in scope_token:
        raise ValueError("feed cursor scope is invalid")
    if not isinstance(filter_value, str):
        raise ValueError("feed cursor filter is invalid")
    if filter_value not in _FEED_CURSOR_FILTERS:
        raise ValueError("feed cursor filter is invalid")
    if type(high_water) is not int or high_water < 0:
        raise ValueError("feed cursor high-water is invalid")
    if type(last_occurred_at) is not int or last_occurred_at < 0:
        raise ValueError("feed cursor position is invalid")
    if type(last_seq) is not int or last_seq < 1:
        raise ValueError("feed cursor position is invalid")
    if last_seq > high_water:
        raise ValueError("feed cursor position is beyond the pinned snapshot")
    return scope_token, filter_value, high_water, last_occurred_at, last_seq


def _is_true(value: Any) -> bool:
    return value is True or str(value).casefold() in {"1", "true", "yes", "on"}


def _is_false(value: Any) -> bool:
    return value is False or str(value).casefold() in {"0", "false", "no", "off"}


def _new_confirmation_token() -> str:
    """Generate an opaque ASCII token that fits Telegram callback data."""
    token = secrets.token_urlsafe(12)
    if (
        not isinstance(token, str)
        or not token
        or not token.isascii()
        or len(token.encode("utf-8")) > CONFIRMATION_TOKEN_MAX_BYTES
    ):
        raise ServiceUnavailableError("confirmation token budget exceeded")
    return token


def _require_successful_control(result: Any) -> Any:
    """Reject typed control reports that completed with a safety failure."""
    if result is False or getattr(result, "success", True) is False:
        raise ServiceUnavailableError("control operation was not completed")
    return result


def _control_audit_boundary(action: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a public control boundary with safe attempt/terminal events."""

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(method)
        async def wrapped(self: Any, user_id: Any, *args: Any, **kwargs: Any) -> Any:
            audit_action = action
            if action == "confirm_action":
                confirmation = kwargs.get("confirmation")
                if confirmation is None and args:
                    confirmation = args[0]
                try:
                    raw_action = getattr(confirmation, "action", None)
                except Exception:  # pragma: no cover - malformed caller object
                    raw_action = None
                audit_action = self._confirmation_audit_action(raw_action)
            account_id = self._audit_account_id(user_id)
            target_user_id: int | None = None
            if action in {"admin_allow_user", "admin_set_user_access"}:
                raw_target = kwargs.get("target_user_id")
                if raw_target is None and args:
                    raw_target = args[0]
                target_user_id = self._numeric_id(raw_target)
            self._control_attempt(audit_action, user_id, account_id, target_user_id)
            try:
                result = await method(self, user_id, *args, **kwargs)
            except Exception as exc:
                self._control_rejected(
                    action=audit_action,
                    user_id=user_id,
                    account_id=account_id,
                    exc=exc,
                    target_user_id=target_user_id,
                )
                raise

            changed_fields: tuple[str, ...] = ()
            state: str | None = None
            if action == "set_notifications":
                changed_fields = ("notifications_enabled",)
                enabled = getattr(result, "enabled", None)
                state = "enabled" if enabled is True else "disabled" if enabled is False else None
            elif action == "update_risk":
                values = kwargs.get("values")
                if values is None and args:
                    values = args[0]
                if isinstance(values, RiskUpdateInput):
                    changed_fields = tuple(
                        field
                        for field in (
                            "risk_percent",
                            "max_leverage",
                            "max_entry_deviation_pct",
                        )
                        if getattr(values, field) is not None
                    )
            elif action == "pause":
                changed_fields = ("allow_new_entries",)
                state = "paused"
            elif action == "resume":
                changed_fields = ("allow_new_entries",)
                state = "resumed"
            elif action == "admin_allow_user":
                changed_fields = ("access_enabled",)
                state = "enabled"
            elif action == "admin_set_user_access":
                enabled = kwargs.get("enabled")
                if enabled is None and len(args) > 1:
                    enabled = args[1]
                if isinstance(enabled, bool):
                    changed_fields = ("access_enabled",)
                    state = "enabled" if enabled else "disabled"
            elif action == "admin_decide_access":
                # A request id is an operation identifier, never a user
                # identifier.  Only the safe DTO returned after persistence
                # may provide the target user for a successful decision.
                if isinstance(result, AccessRequestDTO):
                    target_user_id = self._numeric_id(result.user_id)
                    if result.status is AccessRequestStatus.APPROVED:
                        state = "approved"
                        if result.decision_changed is True:
                            changed_fields = ("status", "access_enabled")
                    elif result.status is AccessRequestStatus.REJECTED:
                        state = "rejected"
                        if result.decision_changed is True:
                            changed_fields = ("status",)
            elif action == "admin_reconcile":
                raw_account_id = kwargs.get("account_id")
                if raw_account_id is None and args:
                    raw_account_id = args[0]
                state = "account" if raw_account_id is not None else "all"
            elif action == "admin_pause":
                changed_fields = ("allow_new_entries",)
                state = "paused"
            elif action == "admin_resume":
                changed_fields = ("allow_new_entries",)
                state = "resumed"
            elif action == "admin_emergency":
                changed_fields = ("allow_new_entries", "emergency_active")
                state = "emergency"
            elif audit_action in {
                "confirm_resume",
                "confirm_emergency",
                "confirm_disconnect",
                "confirm_global_pause",
                "confirm_global_resume",
                "confirm_global_emergency",
            }:
                state = {
                    "confirm_resume": "resumed",
                    "confirm_emergency": "emergency",
                    "confirm_disconnect": "disconnected",
                    "confirm_global_pause": "paused",
                    "confirm_global_resume": "resumed",
                    "confirm_global_emergency": "emergency",
                }[audit_action]
                changed_fields = (
                    ("account_enabled",)
                    if audit_action == "confirm_disconnect"
                    else ("allow_new_entries",)
                )
            self._control_success(
                result,
                action=audit_action,
                user_id=user_id,
                account_id=account_id,
                target_user_id=target_user_id,
                changed_fields=changed_fields,
                state=state,
            )
            return result

        return wrapped

    return decorator


class UserApplicationService:
    """Shared use-case boundary with repeated authorization and ownership checks."""

    def __init__(
        self,
        runtime: Stage2RuntimeAdapter,
        *,
        admin_ids: Iterable[int] | str | None = None,
        users: Any | None = None,
        accounts: Any | None = None,
        settings: Any | None = None,
        validations: Any | None = None,
        outbox: Any | None = None,
        onboarding: Any | None = None,
        builder_onboarding: Any | None = None,
        emergency_controller: EmergencyControllerPort | None = None,
        episodes: EpisodeRepository | None = None,
        user_events: EventRepository | None = None,
        history: HistoryRepository | None = None,
        commands: CommandRepo | None = None,
        testnet_check: bool | Callable[[], bool | Awaitable[bool]] | None = None,
        clock: Callable[[], datetime] | None = None,
        confirmation_ttl_seconds: float = 60.0,
        permission_ttl_seconds: int = PERMISSION_TTL_SECONDS,
        activity_page_size: int = ACTIVITY_PAGE_SIZE,
        trade_page_size: int = TRADE_PAGE_SIZE,
    ) -> None:
        if runtime is None:
            raise ValueError("runtime adapter is required")
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation TTL must be positive")
        if permission_ttl_seconds <= 0:
            raise ValueError("permission TTL must be positive")
        if not 1 <= activity_page_size <= 100:
            raise ValueError("activity page size must be between 1 and 100")
        if not 1 <= trade_page_size <= 50:
            raise ValueError("trade page size must be between 1 and 50")

        self._runtime = runtime
        db = getattr(runtime, "db", None)
        self._users = users if users is not None else getattr(runtime, "users", None)
        self._accounts = accounts if accounts is not None else getattr(runtime, "accounts", None)
        self._settings = settings if settings is not None else getattr(runtime, "settings", None)
        self._validations = (
            validations if validations is not None else getattr(runtime, "validations", None)
        )
        self._outbox = outbox if outbox is not None else getattr(runtime, "outbox", None)
        self._access_requests = getattr(runtime, "access_requests", None)
        self._onboarding = (
            onboarding if onboarding is not None else getattr(runtime, "onboarding", None)
        )
        self._builder_onboarding = builder_onboarding
        self._emergency = (
            emergency_controller
            if emergency_controller is not None
            else getattr(runtime, "emergency_controller", None)
        )
        self._episodes = episodes
        self._user_events = user_events
        self._history = history
        self._commands = commands
        if db is not None:
            self._users = self._users if self._users is not None else TelegramUserRepo(db)
            self._accounts = self._accounts if self._accounts is not None else AccountRepo(db)
            self._settings = self._settings if self._settings is not None else SettingsRepo(db)
            self._validations = (
                self._validations if self._validations is not None else AgentValidationRepo(db)
            )
            self._outbox = self._outbox if self._outbox is not None else NotificationOutboxRepo(db)
            self._access_requests = (
                self._access_requests
                if self._access_requests is not None
                else TelegramAccessRequestRepo(db)
            )
            self._episodes = episodes if episodes is not None else EpisodeRepository(db)
            self._user_events = user_events if user_events is not None else EventRepository(db)
            self._history = history if history is not None else HistoryRepository(db)
            self._commands = commands if commands is not None else CommandRepo(db)
        self._admin_ids = frozenset(self._parse_admin_ids(admin_ids, runtime))
        self._testnet_check = testnet_check
        self._clock = clock or (lambda: datetime.now(UTC))
        self._confirmation_ttl = float(confirmation_ttl_seconds)
        self._permission_ttl = int(permission_ttl_seconds)
        self._activity_page_size = int(activity_page_size)
        self._trade_page_size = int(trade_page_size)
        self._confirmations: dict[str, _ConfirmationRecord] = {}
        self._confirmation_lock = asyncio.Lock()
        self._mutation_locks: dict[str, asyncio.Lock] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._connectivity: dict[str, bool] = {}
        self._global_mutation_lock = asyncio.Lock()

    @staticmethod
    def _parse_admin_ids(value: Iterable[int] | str | None, runtime: Any) -> list[int]:
        if value is None:
            value = getattr(getattr(runtime, "cfg", None), "telegram_admin_ids", ())
        if isinstance(value, str):
            value = value.strip().strip("[]")
            values: Iterable[Any] = () if not value else re.split(r"[\s,]+", value)
        else:
            values = value or ()
        result: list[int] = []
        for item in values:
            if isinstance(item, bool):
                raise ValueError("admin IDs must be positive integers")
            try:
                parsed = int(item)
            except (TypeError, ValueError) as exc:
                raise ValueError("admin IDs must be positive integers") from exc
            if parsed <= 0:
                raise ValueError("admin IDs must be positive integers")
            result.append(parsed)
        return list(dict.fromkeys(result))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _account_lock(self, account_id: str) -> asyncio.Lock:
        lock = self._mutation_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._mutation_locks[account_id] = lock
        return lock

    async def _call_optional(
        self,
        target: Any,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if target is None:
            return _MISSING
        method = getattr(target, name, None)
        if method is None or not callable(method):
            return _MISSING
        return await _maybe_await(method(*args, **kwargs))

    async def _call_required(
        self,
        target: Any,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = await self._call_optional(target, name, *args, **kwargs)
        if result is _MISSING:
            raise ServiceUnavailableError("required application operation is unavailable")
        if result is False:
            raise ServiceUnavailableError("application operation was not completed")
        return result

    @staticmethod
    def _require_method(target: Any, name: str) -> Callable[..., Any]:
        method = getattr(target, name, None)
        if method is None or not callable(method):
            raise ServiceUnavailableError("required application operation is unavailable")
        return method

    @staticmethod
    def _numeric_id(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return int(value)

    async def _user_row(self, user_id: int) -> Any | None:
        result = await self._call_optional(self._users, "get", user_id)
        return None if result is _MISSING else result

    async def _account_row(self, account_id: str) -> Any | None:
        result = await self._call_optional(self._accounts, "get", account_id)
        return None if result is _MISSING else result

    async def _user_by_account(self, account_id: str) -> Any | None:
        result = await self._call_optional(self._users, "by_account", account_id)
        return None if result is _MISSING else result

    @staticmethod
    def _access_request_dto(
        row: Any,
        *,
        decision_changed: bool = False,
        created: bool = False,
    ) -> AccessRequestDTO:
        try:
            status = AccessRequestStatus(str(_row_value(row, "status")))
        except ValueError as exc:
            raise ServiceUnavailableError("access request status is invalid") from exc
        return AccessRequestDTO(
            request_id=int(_row_value(row, "id")),
            user_id=int(_row_value(row, "telegram_user_id")),
            chat_id=int(_row_value(row, "chat_id")),
            status=status,
            username=_row_value(row, "username"),
            display_name=_row_value(row, "display_name"),
            requested_at=_as_datetime(_row_value(row, "requested_at")),
            decided_at=_as_datetime(_row_value(row, "decided_at")),
            decision_changed=decision_changed,
            created=created,
        )

    @staticmethod
    def _profile_username(value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().removeprefix("@")
        return text if re.fullmatch(r"[A-Za-z0-9_]{1,32}", text) else None

    @staticmethod
    def _profile_display_name(value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())[:64]
        return text or None

    async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext:
        """Return a neutral deny context for non-private/unknown identities."""
        normalized_user = self._numeric_id(user_id)
        normalized_chat = self._numeric_id(chat_id)
        if normalized_user is None or normalized_chat is None or chat_type != "private":
            return UserContext(
                user_id=normalized_user or 0,
                chat_id=normalized_chat or 0,
                allowed=False,
            )
        try:
            row = await self._user_row(normalized_user)
        except Exception:
            # A storage/backend failure is not an unknown user.  Returning a
            # neutral deny context here would let the Telegram boundary treat
            # an unavailable authorization store as a guest and expose the
            # public captcha/request flow.  Keep the outward error generic,
            # while preserving the fail-closed distinction for the transport.
            raise ServiceUnavailableError("authorization backend unavailable") from None
        stored_chat_id = self._numeric_id(_row_value(row, "chat_id"))
        if row is None or stored_chat_id != normalized_chat:
            return UserContext(
                user_id=normalized_user,
                chat_id=normalized_chat,
                allowed=False,
            )
        if normalized_user in self._admin_ids:
            return UserContext(
                user_id=normalized_user,
                chat_id=normalized_chat,
                role=UserRole.ADMIN,
                account_id=_row_value(row, "account_id"),
                allowed=True,
            )
        if str(_row_value(row, "role", "USER")) == UserRole.USER and _is_true(
            _row_value(row, "access_enabled", 1)
        ):
            return UserContext(
                user_id=normalized_user,
                chat_id=normalized_chat,
                account_id=_row_value(row, "account_id"),
                allowed=True,
            )
        return UserContext(
            user_id=normalized_user,
            chat_id=normalized_chat,
            allowed=False,
        )

    async def access_request(self, user_id: int) -> AccessRequestDTO | None:
        """Return the latest request without granting access to other use cases."""
        normalized = self._numeric_id(user_id)
        if normalized is None:
            raise ValidationError("Telegram user ID is invalid")
        try:
            row = await self._call_required(
                self._access_requests,
                "latest_for_user",
                normalized,
            )
            return None if row is None else self._access_request_dto(row)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("access request is unavailable") from None

    async def submit_access_request(
        self,
        user_id: int,
        chat_id: int,
        *,
        username: str | None,
        display_name: str | None,
    ) -> AccessRequestDTO:
        """Persist one captcha-qualified request; no access is granted here."""
        normalized_user = self._numeric_id(user_id)
        normalized_chat = self._numeric_id(chat_id)
        if normalized_user is None or normalized_chat is None:
            raise ValidationError("Telegram identity is invalid")
        try:
            current = await self._user_row(normalized_user)
            if current is not None:
                if int(_row_value(current, "chat_id", 0) or 0) != normalized_chat:
                    raise AuthorizationError("private chat identity does not match")
                if normalized_user in self._admin_ids or (
                    str(_row_value(current, "role", "USER")) == UserRole.USER
                    and _is_true(_row_value(current, "access_enabled", 1))
                ):
                    raise ValidationError("Telegram user already has access")
                if str(_row_value(current, "role", "USER")) != UserRole.USER:
                    raise AuthorizationError("Telegram role cannot request access")
            latest = await self._call_required(
                self._access_requests,
                "latest_for_user",
                normalized_user,
            )
            if latest is not None and str(_row_value(latest, "status")) == "REJECTED":
                decided_at = _as_datetime(_row_value(latest, "decided_at"))
                if (
                    decided_at is not None
                    and (self._now() - decided_at).total_seconds()
                    < ACCESS_REQUEST_RETRY_COOLDOWN_SECONDS
                ):
                    raise ValidationError("access request retry is temporarily limited")
            # The result flag is part of the persistence boundary, not a
            # process-local guess: duplicate callbacks may pass the read
            # above concurrently.  Keep a compatibility fallback for small
            # injected repository doubles that expose only create_pending.
            result = await self._call_optional(
                self._access_requests,
                "create_pending_result",
                normalized_user,
                normalized_chat,
                username=self._profile_username(username),
                display_name=self._profile_display_name(display_name),
            )
            if result is _MISSING:
                row = await self._call_required(
                    self._access_requests,
                    "create_pending",
                    normalized_user,
                    normalized_chat,
                    username=self._profile_username(username),
                    display_name=self._profile_display_name(display_name),
                )
                created = False
            else:
                try:
                    row, created = result
                except (TypeError, ValueError) as exc:
                    raise ServiceUnavailableError(
                        "access request persistence returned an invalid result"
                    ) from exc
            return self._access_request_dto(row, created=bool(created))
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("access request could not be submitted") from None

    async def _context(self, user_id: int, *, admin: bool = False) -> UserContext:
        normalized = self._numeric_id(user_id)
        if normalized is None:
            raise AuthorizationError("authorization required")
        if admin and normalized not in self._admin_ids:
            raise AuthorizationError("administrator authorization required")
        row = await self._user_row(normalized)
        if normalized in self._admin_ids:
            return UserContext(
                user_id=normalized,
                chat_id=int(_row_value(row, "chat_id", 0) or 0),
                role=UserRole.ADMIN,
                account_id=_row_value(row, "account_id"),
                allowed=True,
            )
        if (
            row is None
            or str(_row_value(row, "role", "USER")) != UserRole.USER
            or not _is_true(_row_value(row, "access_enabled", 1))
        ):
            raise AuthorizationError("user is not authorized")
        return UserContext(
            user_id=normalized,
            chat_id=int(_row_value(row, "chat_id", 0) or 0),
            account_id=_row_value(row, "account_id"),
            allowed=True,
        )

    async def _owned_account(
        self,
        user_id: int,
        account_id: str | None = None,
        *,
        allow_disabled: bool = False,
    ) -> tuple[UserContext, Any, Any]:
        context = await self._context(user_id)
        owned_id = str(context.account_id or "")
        if not owned_id or (account_id is not None and account_id != owned_id):
            raise AuthorizationError("account ownership check failed")
        row = await self._account_row(owned_id)
        if row is None or str(_row_value(row, "role", "")) != "follower":
            raise AuthorizationError("account ownership check failed")
        if str(_row_value(row, "execution_mode", "REAL") or "REAL") != "REAL":
            raise ServiceUnavailableError("REAL testnet account is required")
        if not allow_disabled and not _is_true(_row_value(row, "enabled", 0)):
            raise AuthorizationError("account is disconnected")
        owner = await self._user_by_account(owned_id)
        if owner is None or int(_row_value(owner, "telegram_user_id", -1)) != user_id:
            raise AuthorizationError("account ownership check failed")
        return context, row, owner

    async def _flag(self, key: str, default: Any = None) -> Any:
        result = await self._call_optional(self._settings, "get_flag", key)
        return default if result is _MISSING else result

    async def _set_flag(self, key: str, value: str) -> None:
        await self._call_required(self._settings, "set_flag", key, value)

    async def _system_state(self) -> SystemState:
        try:
            degraded = await self._flag("degraded_active")
            if _is_true(degraded):
                return SystemState.DEGRADED
            return await self._readiness_state()
        except Exception:
            return SystemState.UNKNOWN

    async def _readiness_state(self, *, honour_not_ready: bool = True) -> SystemState:
        """READY/STALE/UNKNOWN ladder, ignoring the degraded latch."""
        ready = await self._flag("ready")
        if _is_true(ready):
            return await self._heartbeat_state()
        if honour_not_ready and _is_false(ready):
            return SystemState.UNKNOWN
        if not honour_not_ready:
            return await self._heartbeat_state()
        marker = getattr(self._runtime, "ready", _MISSING)
        if marker is True:
            return SystemState.READY
        return SystemState.UNKNOWN

    async def _heartbeat_state(self) -> SystemState:
        """Alive-and-publishing verdict; a dead heartbeat is real staleness."""
        heartbeat = await self._flag("heartbeat_at")
        if heartbeat:
            try:
                age_ms = max(int(time.time() * 1000) - int(heartbeat), 0)
                interval = int(
                    getattr(getattr(self._runtime, "cfg", None), "reconcile_interval_s", 15)
                )
                if age_ms > max(2 * interval, 30) * 1000:
                    return SystemState.STALE
            except (TypeError, ValueError):
                return SystemState.STALE
        return SystemState.READY

    async def _presentation_state(self) -> SystemState:
        """System state as a user should read it.

        Identical to :meth:`_system_state` except that the engine's scheduled
        re-verification and reconnect churn (``ROUTINE_MAINTENANCE_REASONS``) is
        not presented as a fault while it is still resolving. Those windows are
        fail-closed by design, close within seconds and leave the displayed
        account data authoritative, so reporting them is noise rather than
        information. A routine window that outlives
        ``ROUTINE_DEGRADED_GRACE_MS`` is a stall, not churn, and is reported like
        any other degradation. Trading safety is unaffected either way —
        admission is gated by ``allow_new_entries`` and the engine's own
        admission flag, never by this presentation verdict.
        """
        state = await self._system_state()
        if state is not SystemState.DEGRADED:
            return state
        try:
            reason = str(await self._flag("degraded_reason") or "").strip().lower()
            if not _is_routine_maintenance(reason):
                return state
            if await self._degraded_episode_exceeded_grace():
                return state
            # The boundary also lowers ``ready`` (it withholds entry admission).
            # That is the trading gate's business, not the user's, so the
            # presentation verdict falls back to the liveness heartbeat.
            return await self._readiness_state(honour_not_ready=False)
        except Exception:  # noqa: BLE001 - presentation never fails a read
            return state

    async def _degraded_episode_exceeded_grace(self) -> bool:
        """Has this degraded episode lasted longer than routine churn can?

        ``degraded_since`` is stamped by the runtime next to ``heartbeat_at`` and
        both are compared against the same wall clock that writes them, not the
        injectable snapshot clock.
        """
        since = await self._flag("degraded_since")
        if not since:
            # No transition stamp (older row, or a writer that predates it):
            # treat the episode as new, which is the conservative silence.
            return False
        try:
            started_ms = int(since)
        except (TypeError, ValueError):
            return False
        return int(time.time() * 1000) - started_ms > ROUTINE_DEGRADED_GRACE_MS

    async def _ensure_testnet(self) -> None:
        check = self._testnet_check
        if check is not None:
            result = check() if callable(check) else check
            if not _is_true(await _maybe_await(result)):
                raise ServiceUnavailableError("testnet runtime is required")
            return
        cfg = getattr(self._runtime, "cfg", None)
        if cfg is not None:
            rest = str(getattr(cfg, "rest_base_url", "")).rstrip("/")
            ws = str(getattr(cfg, "ws_user_base", "")).rstrip("/")
            if rest == TESTNET_REST_URL and ws == TESTNET_WS_URL:
                return
            raise ServiceUnavailableError("testnet runtime is required")
        marker = getattr(self._runtime, "testnet", _MISSING)
        if marker is not _MISSING and _is_true(marker):
            return
        raise ServiceUnavailableError("testnet runtime is required")

    async def _ensure_ready(self) -> None:
        ready = await self._flag("ready", _MISSING)
        if ready is not _MISSING and not _is_true(ready):
            raise ServiceUnavailableError("engine is not ready")
        marker = getattr(self._runtime, "ready", _MISSING)
        if marker is not _MISSING and not _is_true(marker):
            raise ServiceUnavailableError("engine is not ready")

    async def _validation_row(self, account_id: str) -> Any | None:
        result = await self._call_optional(self._validations, "get", account_id)
        return None if result is _MISSING else result

    async def _refresh_permission_snapshot(self, account_id: str) -> None:
        """Refresh and persist one signed Agent permission snapshot.

        A stored row is an audit/cache record, never a reason to grant a
        destructive operation after its freshness window.  The caller must
        inject the signed-read operation explicitly; absence fails closed.
        """
        try:
            # The adapter method is the signed Aster USER_DATA read.  It must
            # return the repository record shape; accepting arbitrary mappings
            # here would make a stale cache look like a fresh permission check.
            record = await self._call_required(
                self._runtime, "refresh_agent_permission", account_id
            )
            if not isinstance(record, AgentValidation):
                raise ValueError("signed permission adapter returned an invalid record")
            signer = record.signer_address
            if not isinstance(signer, str) or not signer.strip():
                raise ValueError("missing signer")
            if record.can_read is not True or record.can_perp_trade is not True:
                raise ValueError("insufficient agent permissions")
            if record.can_withdraw is not False:
                raise ValueError("withdraw permission must be denied")
            expiry = int(record.expired_ms)
            now_ms = int(self._now().timestamp() * 1000)
            if expiry <= now_ms:
                raise ValueError("agent permission is expired")
            persisted = AgentValidation(
                signer_address=signer.strip(),
                can_read=True,
                can_perp_trade=True,
                can_withdraw=False,
                expired_ms=expiry,
                ip_whitelist=record.ip_whitelist,
                status=str(record.status),
                checked_at=self._now().isoformat(),
                last_error_code=None,
            )
            await self._call_required(self._validations, "upsert", account_id, validation=persisted)
        except _SAFE_ERRORS:
            # Authentication/ownership/testnet boundary errors fail closed
            # and are never relabeled as stale or rejected.
            raise
        except AsterError as exc:
            if exc.code in {-2015, -2016}:
                # The Agent is gone / no longer authorized (AGENT_NOT_FOUND):
                # proven invalid, never a transient outage.  This must stay a
                # rejection so the resume gate cannot take the timed override
                # fallback on a revoked Agent.
                raise StaleOperationError("permission refresh was rejected") from None
            # Transport / rate-limit (429/418) / HTTP 5xx during the signed
            # read: transient infrastructure failure, never a proven
            # permission rejection.
            raise ServiceUnavailableError("signed Agent permission read is unavailable") from None
        except (OnboardingError, ValueError):
            # Proven-invalid Agent evidence (failed triple/expiry/whitelist/
            # signature validation, or a malformed record): blocked, never
            # served from old cache as fresh.
            raise StaleOperationError("permission refresh was rejected") from None
        except Exception:
            # Persistence/upsert or any other infrastructure failure is not
            # a proven rejection either.
            raise ServiceUnavailableError("signed Agent permission read is unavailable") from None

    async def _permission(
        self, account_id: str, *, refresh: bool = False
    ) -> tuple[PermissionSummary, datetime | None]:
        if refresh:
            await self._refresh_permission_snapshot(account_id)
        row = await self._validation_row(account_id)
        if row is None:
            return PermissionSummary(warning="permission_check_unavailable"), None
        status = str(_row_value(row, "status", "")).upper()
        warning = (
            "permission_check_override"
            if status == "VALID_OVERRIDE"
            else None
            if status in {"OK", "VALID", "CONFIRMED", "PASSED"}
            else "permission_check_pending"
        )
        checked_at = _as_datetime(_row_value(row, "checked_at"))
        return (
            PermissionSummary(
                can_read=_row_bool(row, "can_read"),
                can_perp_trade=_row_bool(row, "can_perp_trade"),
                can_withdraw=_row_bool(row, "can_withdraw"),
                whitelist_ok=(
                    True
                    if warning == "permission_check_override"
                    else None
                    if not _row_value(row, "ip_whitelist")
                    else warning is None
                ),
                checked_at=checked_at,
                warning=warning,
            ),
            checked_at,
        )

    async def _runtime_registered(self, account_id: str) -> bool:
        result = await self._call_optional(self._runtime, "is_account_registered", account_id)
        if result is _MISSING:
            raise ServiceUnavailableError("required application operation is unavailable")
        return bool(result)

    async def _account_version(self, account_id: str) -> str:
        value = await self._call_required(self._runtime, "account_version", account_id)
        if not isinstance(value, str) or not value:
            raise ServiceUnavailableError("account credential version is unavailable")
        return value

    async def _snapshot(self, account_id: str, *, refresh: bool) -> tuple[Any, datetime | None]:
        if not refresh:
            result = await self._call_optional(self._runtime, "cached_snapshot", account_id)
            if result is _MISSING or result is None:
                return None, None
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], datetime):
                return result[0], result[1]
            return result, None
        task = self._refresh_tasks.get(account_id)
        if task is None or task.done():
            task = asyncio.ensure_future(
                self._call_optional(self._runtime, "refresh_snapshot", account_id)
            )
            self._refresh_tasks[account_id] = task
            task.add_done_callback(
                lambda done, key=account_id, expected=task: (
                    self._refresh_tasks.pop(key, None)
                    if self._refresh_tasks.get(key) is expected
                    else None
                )
            )
        # Shielded join: one exchange read serves every concurrent caller,
        # and one cancelled waiter never aborts the others.
        try:
            result = await asyncio.shield(task)
        except Exception as exc:
            if _is_connectivity_error(exc):
                await self._record_connectivity(account_id, reachable=False)
            raise
        if result is _MISSING:
            raise ServiceUnavailableError("account snapshot is unavailable")
        await self._record_connectivity(account_id, reachable=True)
        if result is None:
            return None, None
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], datetime):
            return result[0], result[1]
        return result, self._now()

    def _snapshot_ttl_seconds(self) -> float:
        """Bounded freshness window for cached snapshots.

        A cached snapshot counts as fresh only within
        ``max(2 x reconcile_interval_s, 30)`` seconds of capture; an older
        cache must surface as stale instead of silently serving outdated
        risk figures while the system heartbeat stays READY.
        """
        cfg = getattr(self._runtime, "cfg", None)
        reconcile = getattr(cfg, "reconcile_interval_s", None)
        try:
            reconcile_value = int(reconcile) if reconcile is not None else 15
        except (TypeError, ValueError):
            reconcile_value = 15
        return float(max(reconcile_value * 2, 30))

    @staticmethod
    def _position_count(snapshot: Any) -> int | None:
        positions = _row_value(snapshot, "positions", _MISSING)
        if positions is _MISSING or positions is None:
            return None
        values = positions.values() if isinstance(positions, Mapping) else positions
        count = 0
        for position in values:
            qty = _row_value(position, "qty", _row_value(position, "position_amt", 1))
            try:
                if Decimal(str(qty)) != 0:
                    count += 1
            except (InvalidOperation, TypeError, ValueError):
                count += 1
        return count

    async def _copy_state_and_message(
        self,
        account_id: str,
        account_row: Any,
        system_state: SystemState,
    ) -> tuple[CopyState, str | None]:
        """Shared Dashboard/Status copy-state semantics (single authority).

        Status and Dashboard must never diverge on BLOCKED / DEGRADED /
        PAUSED / ACTIVE, so both derive the verdict here; only Dashboard
        also needs the human message, which stays internal to presenters.
        """
        policy = await self._call_optional(self._settings, "get_policy", account_id)
        account_enabled = _is_true(_row_value(account_row, "enabled", 0))
        execution_mode = str(_row_value(account_row, "execution_mode", "REAL") or "REAL")
        degraded = system_state is SystemState.DEGRADED
        global_emergency = _is_true(await self._flag("emergency_active"))
        global_pause = _is_false(await self._flag("allow_new_entries", "true"))
        if not account_enabled or execution_mode != "REAL":
            # Disabled historical rows remain owned for audit/reconnect.  Do
            # not present them as a hard runtime block with no recovery path.
            copy_state = CopyState.EMPTY
            message = "account is disconnected; connect credentials to continue"
        elif policy is _MISSING or policy is None or not _is_true(_row_value(policy, "enabled", 0)):
            copy_state = CopyState.BLOCKED
            message = "account policy is unavailable"
        elif degraded:
            copy_state = CopyState.DEGRADED
            message = "runtime requires attention"
        elif global_emergency:
            copy_state = CopyState.PAUSED
            message = "global emergency is active"
        elif global_pause:
            copy_state = CopyState.PAUSED
            message = "global pause is active"
        elif not _is_true(_row_value(policy, "allow_new_entries", 0)):
            copy_state = CopyState.PAUSED
            message = "new entries are paused"
        else:
            copy_state = CopyState.ACTIVE
            message = None
        return copy_state, message

    async def _dashboard_for(
        self,
        context: UserContext,
        account_row: Any | None,
        *,
        account_id: str | None,
        snapshot: Any = None,
        snapshot_captured_at: datetime | None = None,
    ) -> DashboardDTO:
        system_state = await self._presentation_state()
        user_row = await self._user_row(context.user_id)
        enabled = bool(_row_value(user_row, "notifications_enabled", 1))
        if account_id is None or account_row is None:
            return DashboardDTO(
                system_state=system_state,
                account_id=None,
                notification_enabled=enabled,
                is_admin=context.role is UserRole.ADMIN,
                message="account is not connected",
            )
        permission, _ = await self._permission(account_id)
        policy = await self._call_optional(self._settings, "get_policy", account_id)
        copy_state, message = await self._copy_state_and_message(
            account_id, account_row, system_state
        )
        if snapshot is None:
            snapshot, _ = await self._snapshot(account_id, refresh=False)
        identity = await self._identity(account_id)
        return DashboardDTO(
            copy_state=copy_state,
            system_state=system_state,
            account_id=account_id,
            account_masked=mask_address(str(_row_value(account_row, "user_address", ""))),
            signer_masked=identity,
            risk_percent=_decimal_or_none(_row_value(policy, "risk_percent")),
            max_leverage=_int_or_none(_row_value(policy, "max_leverage")),
            open_positions=self._position_count(snapshot),
            updated_at=_as_datetime(
                _row_value(account_row, "updated_at", _row_value(account_row, "created_at"))
            ),
            snapshot_captured_at=snapshot_captured_at,
            message=message,
            permission=permission,
            notification_enabled=enabled,
            is_admin=context.role is UserRole.ADMIN,
            builder_verified=await self._builder_verified(account_id),
            overview=await self._overview_for(
                account_id, snapshot, snapshot_captured_at, system_state
            ),
        )

    async def _identity(self, account_id: str) -> str | None:
        credentials = getattr(self._runtime, "credentials", None)
        try:
            result = await self._call_optional(credentials, "identity", account_id)
        except Exception:
            return None
        if result is _MISSING or result is None:
            return None
        signer = _row_value(result, "signer", _row_value(result, "signer_address"))
        if signer is None:
            return None
        return mask_address(str(signer))

    @staticmethod
    def _parse_position_summary(
        position: Any,
        is_fresh: bool,
        fee_buffer_pct: Decimal = Decimal("0.001"),
    ) -> PositionSummaryDTO | None:
        raw_qty = _row_value(position, "qty", _row_value(position, "position_amt", Decimal(0)))
        try:
            qty_dec = Decimal(str(raw_qty))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if qty_dec == Decimal(0):
            return None

        # Determine side:
        key = _row_value(position, "key", None)
        pos_side = getattr(key, "pos_side", None) if key else None
        if pos_side is None:
            raw_ps = str(_row_value(position, "position_side", "BOTH")).upper()
            if raw_ps == "LONG":
                side = PositionSide.LONG
            elif raw_ps == "SHORT":
                side = PositionSide.SHORT
            else:
                side = PositionSide.LONG if qty_dec > 0 else PositionSide.SHORT
        else:
            if getattr(pos_side, "value", str(pos_side)).upper() == "LONG":
                side = PositionSide.LONG
            elif getattr(pos_side, "value", str(pos_side)).upper() == "SHORT":
                side = PositionSide.SHORT
            else:
                side = PositionSide.LONG if qty_dec > 0 else PositionSide.SHORT

        symbol = str(getattr(key, "symbol", None) or _row_value(position, "symbol", ""))
        entry_price = _decimal_or_none(_row_value(position, "entry_price", Decimal(0))) or Decimal(
            0
        )
        mark_price = _decimal_or_none(_row_value(position, "mark_price", Decimal(0))) or Decimal(0)
        unrealized_profit = _decimal_or_none(
            _row_value(
                position,
                "unrealized_profit",
                _row_value(position, "un_realized_profit", Decimal(0)),
            )
        ) or Decimal(0)
        leverage = _int_or_none(_row_value(position, "leverage", 1)) or 1

        stop_obj = _row_value(position, "stop", None)
        tp_obj = _row_value(position, "take_profit", None)
        extra_stops = _row_value(position, "extra_stops", ()) or ()

        stop_price = _decimal_or_none(_row_value(stop_obj, "stop_price", None))
        tp_price = _decimal_or_none(_row_value(tp_obj, "stop_price", None))

        # Protection state evaluation:
        if stop_obj is None:
            protection_state = PositionProtectionState.MISSING_STOP
        elif len(extra_stops) > 0:
            protection_state = PositionProtectionState.DUPLICATE_STOP
        else:
            # Validate Stop order properties:
            # 1. Side must exactly oppose position direction (LONG -> SELL,
            #    SHORT -> BUY).  A missing or unknown side cannot be trusted
            #    as protective intent and stays UNCONFIRMED.
            stop_side_raw = str(_row_value(stop_obj, "side", "")).upper()
            expected_stop_side = "SELL" if side == PositionSide.LONG else "BUY"
            is_opposing_side = stop_side_raw == expected_stop_side

            # 2. Must be reduce_only or close_position
            close_pos = bool(_row_value(stop_obj, "close_position", False))
            reduce_only = bool(_row_value(stop_obj, "reduce_only", False))
            is_protective_intent = close_pos or reduce_only

            # 3. Coverage check: a close_position order covers the whole live
            #    position at trigger time (exchange reports orig_qty=0 for
            #    such orders), while a quantity-based (reduce_only) stop must
            #    carry orig_qty >= abs(position qty).  Zero or invalid quantity
            #    on a quantity-based stop is UNCONFIRMED; a positive but
            #    insufficient quantity is PARTIAL_STOP.
            stop_orig_qty = _decimal_or_none(
                _row_value(stop_obj, "orig_qty", Decimal(0))
            ) or Decimal(0)
            abs_qty = abs(qty_dec)

            if not is_opposing_side or not is_protective_intent:
                protection_state = PositionProtectionState.UNCONFIRMED
            elif not close_pos and (stop_orig_qty <= 0 or stop_orig_qty < abs_qty):
                # A quantity-based stop without a valid quantity covering the
                # whole position cannot be trusted as full protection.
                protection_state = (
                    PositionProtectionState.PARTIAL_STOP
                    if stop_orig_qty > 0
                    else PositionProtectionState.UNCONFIRMED
                )
            elif close_pos and stop_orig_qty > 0 and stop_orig_qty < abs_qty:
                # Defensive: a close_position flag with an explicit fixed
                # quantity smaller than the position is treated as partial.
                protection_state = PositionProtectionState.PARTIAL_STOP
            else:
                # Coverage holds; check that stop price is on the protective
                # side of mark (or entry if mark <= 0).
                ref_price = mark_price if mark_price > 0 else entry_price
                if stop_price is not None and ref_price > 0:
                    if side == PositionSide.LONG and stop_price >= ref_price:
                        # unconfirmed or invalid stop level
                        protection_state = PositionProtectionState.UNCONFIRMED
                    elif side == PositionSide.SHORT and stop_price <= ref_price:
                        protection_state = PositionProtectionState.UNCONFIRMED
                    else:
                        protection_state = PositionProtectionState.PROTECTED
                elif stop_price is not None:
                    protection_state = PositionProtectionState.PROTECTED
                else:
                    protection_state = PositionProtectionState.MISSING_STOP

        risk_to_stop_usdt: Decimal | None = None
        if (
            is_fresh
            and protection_state == PositionProtectionState.PROTECTED
            and stop_price is not None
        ):
            distance = (
                abs(mark_price - stop_price) if mark_price > 0 else abs(entry_price - stop_price)
            )
            if distance > 0:
                per_unit_loss = distance * (Decimal(1) + fee_buffer_pct)
                risk_to_stop_usdt = abs(qty_dec) * per_unit_loss

        return PositionSummaryDTO(
            symbol=symbol,
            side=side,
            qty=abs(qty_dec),
            entry_price=entry_price,
            mark_price=mark_price,
            unrealized_profit=unrealized_profit,
            leverage=leverage,
            protection_state=protection_state,
            stop_price=stop_price,
            take_profit_price=tp_price,
            risk_to_stop_usdt=risk_to_stop_usdt,
        )

    async def positions(self, user_id: int, refresh: bool = False) -> PositionPageDTO:
        try:
            context = await self._context(user_id)
            account_id = str(context.account_id) if context.account_id else None
            system_state = await self._presentation_state()
            if account_id is None:
                return PositionPageDTO(
                    positions=(),
                    count=0,
                    system_state=system_state,
                    message="account is not connected",
                )
            context, row, _ = await self._owned_account(
                user_id,
                account_id,
                allow_disabled=True,
            )
            snapshot = None
            message = None
            captured_at = None
            refresh_failed = False
            if refresh:
                try:
                    snapshot, captured_at = await self._snapshot(account_id, refresh=True)
                except _SAFE_ERRORS:
                    raise
                except Exception:
                    refresh_failed = True
                    message = "Aster positions are temporarily unavailable"
                    # Fall back to cached snapshot
                    snapshot, captured_at = await self._snapshot(account_id, refresh=False)
            else:
                snapshot, captured_at = await self._snapshot(account_id, refresh=False)
            # Bounded freshness TTL: a cached snapshot is only considered
            # fresh while its capture time is within max(2 x reconcile
            # interval, 30s).  Without this bound a live heartbeat would
            # keep serving arbitrary old cached risk figures as fresh.
            snapshot_ttl_s = self._snapshot_ttl_seconds()
            cache_expired = captured_at is not None and (
                (self._now() - captured_at).total_seconds() > snapshot_ttl_s
            )

            is_stale = (
                system_state is SystemState.STALE
                or refresh_failed
                or snapshot is None
                or system_state is not SystemState.READY
                or cache_expired
            )
            is_fresh = (
                not is_stale
                and system_state is SystemState.READY
                and captured_at is not None
                and not cache_expired
            )

            cfg = getattr(self._runtime, "cfg", None)
            fee_buffer_pct = Decimal(str(getattr(cfg, "fee_buffer_pct", "0.001")))

            raw_positions = self._raw_positions(snapshot)

            parsed_list: list[PositionSummaryDTO] = []
            for p in raw_positions:
                summary = self._parse_position_summary(
                    p, is_fresh=is_fresh, fee_buffer_pct=fee_buffer_pct
                )
                if summary is not None:
                    parsed_list.append(summary)

            assets = await self._quantity_assets(account_id, [item.symbol for item in parsed_list])
            parsed_list = [
                replace(item, quantity_asset=assets.get(item.symbol)) for item in parsed_list
            ]
            parsed_list.sort(key=lambda item: (item.symbol, item.side))

            return PositionPageDTO(
                positions=tuple(parsed_list),
                count=len(parsed_list),
                system_state=system_state,
                snapshot_captured_at=captured_at,
                is_stale=is_stale,
                message=message,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("positions are unavailable") from None

    async def dashboard(self, user_id: int, refresh: bool = False) -> DashboardDTO:
        try:
            context = await self._context(user_id)
            account_id = str(context.account_id) if context.account_id else None
            row = None
            if account_id is not None:
                context, row, _ = await self._owned_account(
                    user_id,
                    account_id,
                    allow_disabled=True,
                )
            snapshot = None
            captured_at = None
            if account_id is not None:
                snapshot, captured_at = await self._snapshot(account_id, refresh=refresh)
            return await self._dashboard_for(
                context,
                row,
                account_id=account_id,
                snapshot=snapshot,
                snapshot_captured_at=captured_at,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("dashboard is unavailable") from None

    async def _status_permission(
        self, account_id: str, *, refresh: bool
    ) -> tuple[PermissionSummary, datetime | None, bool, bool]:
        """Signed permission evidence for System Status.

        Returns ``(summary, checked_at, fresh, rejected)``.  A transient
        ``ServiceUnavailable`` during the signed read keeps the last safe
        cached snapshot (stale, never fresh); a proven ``StaleOperation``
        rejection is surfaced as ``rejected`` so the caller blocks instead
        of trusting old cache.  Authentication/ownership/testnet boundary
        errors are never converted into a stale fallback.
        """
        if refresh:
            try:
                summary, checked_at = await self._permission(account_id, refresh=True)
            except StaleOperationError:
                summary, checked_at = await self._permission(account_id)
                return summary, checked_at, False, True
            except ServiceUnavailableError:
                summary, checked_at = await self._permission(account_id)
                return summary, checked_at, False, False
            return summary, checked_at, True, False
        summary, checked_at = await self._permission(account_id)
        return summary, checked_at, False, False

    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO:
        """Authoritative read-only System Status (P2.5 control-plane slice).

        Reuses ``_presentation_state()``, the shared Dashboard copy-state
        semantics, the process-local runtime snapshot cache, and the signed
        Agent permission refresh.  A failed refresh never destroys the last
        useful snapshot and never mints a new timestamp: stale evidence is
        returned as stale with its original capture time.  ``refresh=False``
        stays a cheap cached read and never performs a signed/network
        refresh.  Overall precedence: blocked → degraded → stale → ready.
        """
        try:
            context = await self._context(user_id)
            account_id = str(context.account_id) if context.account_id else None
            system_state = await self._presentation_state()
            if account_id is None:
                return StatusDTO(
                    state=StatusState.BLOCKED,
                    system_state=system_state,
                    copy_state=CopyState.EMPTY,
                    aster_state=AsterState.BLOCKED,
                    permissions_state=PermissionsState.UNAVAILABLE,
                    reason=StatusReason.ACCOUNT_NOT_CONNECTED,
                    remediation=StatusRemediation.OPEN_BOT,
                )
            context, row, _ = await self._owned_account(
                user_id,
                account_id,
                allow_disabled=True,
            )
            account_masked = mask_address(str(_row_value(row, "user_address", "")))
            signer_masked = await self._identity(account_id)
            registered = await self._runtime_registered(account_id)
            execution_mode = str(_row_value(row, "execution_mode", "REAL") or "REAL")
            usable = (
                _is_true(_row_value(row, "enabled", 0)) and registered and execution_mode == "REAL"
            )
            copy_state, _ = await self._copy_state_and_message(account_id, row, system_state)

            snapshot: Any = None
            captured_at: datetime | None = None
            refresh_failed = False
            if refresh:
                try:
                    snapshot, captured_at = await self._snapshot(account_id, refresh=True)
                except _SAFE_ERRORS:
                    raise
                except Exception:
                    # Transient read failure: fall back to the cached snapshot
                    # without minting a new capture timestamp.
                    refresh_failed = True
                    snapshot, captured_at = await self._snapshot(account_id, refresh=False)
            else:
                snapshot, captured_at = await self._snapshot(account_id, refresh=False)

            snapshot_ttl_s = self._snapshot_ttl_seconds()
            cache_expired = captured_at is not None and (
                (self._now() - captured_at).total_seconds() > snapshot_ttl_s
            )
            snapshot_fresh = (
                snapshot is not None
                and captured_at is not None
                and not cache_expired
                and not refresh_failed
            )
            if not usable:
                aster_state = AsterState.BLOCKED
            elif snapshot is None:
                aster_state = AsterState.UNKNOWN
            elif not snapshot_fresh:
                aster_state = AsterState.STALE
            else:
                aster_state = AsterState.OK

            permission, checked_at, fresh_signed, rejected = await self._status_permission(
                account_id, refresh=refresh
            )
            validation_row = await self._validation_row(account_id)
            try:
                permission_expiry_ms = int(_row_value(validation_row, "expired_ms", 0))
            except (TypeError, ValueError):
                permission_expiry_ms = 0
            now = self._now()
            now_ms = int(now.timestamp() * 1000)
            override_raw = await self._call_optional(
                self._runtime, "permission_override_allowed", account_id
            )
            override_allowed = override_raw is not _MISSING and bool(override_raw)
            triple_ok = (
                permission.can_read is True
                and permission.can_perp_trade is True
                and permission.can_withdraw is False
            )
            warning_ok = permission.warning is None or (
                permission.warning == "permission_check_override" and override_allowed
            )
            evidence_ok = (
                validation_row is not None
                and triple_ok
                and permission_expiry_ms > now_ms
                and warning_ok
            )
            # A signed-this-call validation, or a cached check inside the same
            # TTL bound the resume gate trusts, counts as valid evidence.  A
            # cached row alone is never fresh proof after a signed rejection,
            # and a failed signed attempt never leaves a recent cache looking
            # freshly validated: the transient failure itself marks stale.
            checked_fresh = checked_at is not None and (
                fresh_signed or (now - checked_at).total_seconds() <= self._permission_ttl
            )
            signed_refresh_failed = refresh and not fresh_signed and not rejected
            expired_only = (
                validation_row is not None
                and triple_ok
                and warning_ok
                and permission_expiry_ms <= now_ms
            )
            if rejected:
                permissions_state = PermissionsState.BLOCKED
            elif evidence_ok and checked_fresh and not signed_refresh_failed:
                permissions_state = PermissionsState.VALID
            elif evidence_ok or expired_only:
                permissions_state = PermissionsState.STALE
            else:
                permissions_state = PermissionsState.UNAVAILABLE

            # Conservative last check: the status is never younger than its
            # oldest mandatory evidence, and never advances on a failed attempt.
            evidences = [moment for moment in (captured_at, checked_at) if moment is not None]
            last_check_at = min(evidences) if evidences else None
            age_seconds = (
                None
                if last_check_at is None
                else max(int((now - last_check_at).total_seconds()), 0)
            )

            if not usable:
                state = StatusState.BLOCKED
                reason = StatusReason.ACCOUNT_DISCONNECTED
                remediation: StatusRemediation | None = StatusRemediation.OPEN_BOT
            elif rejected:
                state = StatusState.BLOCKED
                reason = StatusReason.PERMISSION_INVALID
                remediation = StatusRemediation.OPEN_BOT
            elif copy_state is CopyState.BLOCKED:
                state = StatusState.BLOCKED
                reason = StatusReason.COPY_BLOCKED
                remediation = StatusRemediation.OPEN_BOT
            elif system_state is SystemState.DEGRADED:
                state = StatusState.DEGRADED
                reason = StatusReason.SYSTEM_DEGRADED
                remediation = StatusRemediation.REFRESH
            elif copy_state is CopyState.DEGRADED:
                state = StatusState.DEGRADED
                reason = StatusReason.COPY_DEGRADED
                remediation = StatusRemediation.REFRESH
            elif system_state is SystemState.STALE:
                state = StatusState.STALE
                reason = StatusReason.SYSTEM_STALE
                remediation = StatusRemediation.REFRESH
            elif system_state is not SystemState.READY:
                state = StatusState.STALE
                reason = StatusReason.SYSTEM_NOT_READY
                remediation = StatusRemediation.REFRESH
            elif snapshot is None:
                state = StatusState.STALE
                reason = StatusReason.SNAPSHOT_MISSING
                remediation = StatusRemediation.REFRESH
            elif refresh_failed:
                state = StatusState.STALE
                reason = StatusReason.ASTER_UNAVAILABLE
                remediation = StatusRemediation.REFRESH
            elif cache_expired or captured_at is None:
                state = StatusState.STALE
                reason = StatusReason.SNAPSHOT_STALE
                remediation = StatusRemediation.REFRESH
            elif permissions_state is PermissionsState.UNAVAILABLE:
                state = StatusState.STALE
                reason = StatusReason.PERMISSION_UNAVAILABLE
                remediation = StatusRemediation.REFRESH
            elif permissions_state is PermissionsState.STALE and signed_refresh_failed:
                # A verification that was just attempted and failed is a real
                # answer: say so. Merely *aged* evidence is not — the cached
                # triple (read/perp/withdraw) is still the last proven truth, the
                # row carries its age, and the screen re-verifies itself on open.
                # Painting the whole status amber for it told a user with a
                # healthy account that "data is stale" every time they looked,
                # because the evidence TTL is one minute.
                state = StatusState.STALE
                reason = StatusReason.PERMISSION_STALE
                remediation = StatusRemediation.REFRESH
            elif permissions_state is PermissionsState.BLOCKED:
                state = StatusState.BLOCKED
                reason = StatusReason.PERMISSION_INVALID
                remediation = StatusRemediation.OPEN_BOT
            elif copy_state not in (CopyState.ACTIVE, CopyState.PAUSED):
                state = StatusState.BLOCKED
                reason = StatusReason.COPY_BLOCKED
                remediation = StatusRemediation.OPEN_BOT
            else:
                state = StatusState.READY
                reason = StatusReason.NONE
                remediation = None
            return StatusDTO(
                state=state,
                system_state=system_state,
                copy_state=copy_state,
                aster_state=aster_state,
                permissions_state=permissions_state,
                account_masked=account_masked,
                signer_masked=signer_masked,
                positions_count=self._position_count(snapshot),
                permissions=permission,
                snapshot_captured_at=captured_at,
                last_check_at=last_check_at,
                age_seconds=age_seconds,
                reason=reason,
                remediation=remediation,
                builder_verified=await self._builder_verified(account_id),
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("status is unavailable") from None

    # --- P2.6 Account + Builder attestation -----------------------------------

    def _builder_expected(self) -> tuple[str, str, str] | None:
        """Configured Builder identity (address, name, fee cap), or None."""
        cfg = getattr(self._runtime, "cfg", None)
        if cfg is None:
            return None
        address = str(getattr(cfg, "builder_address", "") or "").strip()
        if not address:
            return None
        return (
            address,
            str(getattr(cfg, "builder_name", "") or ""),
            str(getattr(cfg, "max_fee_rate", "") or ""),
        )

    async def _current_signer(self, account_id: str) -> str | None:
        """Full current signer address for proof binding; never sent masked."""
        credentials = getattr(self._runtime, "credentials", None)
        try:
            result = await self._call_optional(credentials, "identity", account_id)
        except Exception:
            return None
        if result is _MISSING or result is None:
            return None
        signer = _row_value(result, "signer", _row_value(result, "signer_address"))
        if not isinstance(signer, str) or not signer.strip():
            return None
        return signer.strip()

    async def _builder_verified(self, account_id: str) -> bool:
        """Fresh shared Builder verdict for the trust rails (no network).

        True only when the durable proof is complete AND fresh AND bound to
        the current config: builder source, complete snapshot, live
        validation triple, matching signer, valid expiries, a
        ``builder_verified_at`` inside the same freshness TTL the Account
        screen uses, a persisted Builder address equal to the currently
        configured one, and a persisted fee cap Decimal-equivalent to the
        configured cap (never float).  Stale, expiring, invalid and
        config-mismatched proofs all read False here, exactly like the
        Account screen refuses them VERIFIED: an Agent inside the
        ``BUILDER_EXPIRY_WARNING_MS`` window reads EXPIRING on the Account
        screen, so the rails must read False too (not True just because the
        expiry has not elapsed yet).
        """
        if not await self._builder_proof_fresh(account_id):
            return False
        try:
            validation = await self._validation_row(account_id)
            agent_expires = _int_or_none(_row_value(validation, "expired_ms", None))
            if agent_expires is None:
                return False
            now_ms = int(self._now().timestamp() * 1000)
            return agent_expires - now_ms > BUILDER_EXPIRY_WARNING_MS
        except Exception:
            return False

    async def _builder_proof_fresh(self, account_id: str) -> bool:
        """Durable proof + TTL + current config, without the expiry window.

        Shared core of `_builder_verified` (which additionally requires the
        Agent outside the reauthorization warning window) and of the cached
        EXPIRING branch (which requires this core plus an expiry inside the
        window).  Splitting the two keeps the Account screen and the trust
        rails on one truth without re-implementing the checks twice.
        """
        try:
            if not await self._durable_builder_proof_ok(account_id):
                return False
            row = await self._account_row(account_id)
            verified_at = _as_datetime(_row_value(row, "builder_verified_at", None))
            if verified_at is None:
                return False
            if (self._now() - verified_at).total_seconds() > self._permission_ttl:
                return False
            expected = self._builder_expected()
            if expected is None:
                return False
            expected_address, _, expected_fee = expected
            persisted_address = str(_row_value(row, "builder_address", "") or "")
            if persisted_address.strip().lower() != expected_address.strip().lower():
                return False
            try:
                persisted_fee = Decimal(str(_row_value(row, "builder_max_fee_rate", "")))
                configured_fee = Decimal(str(expected_fee))
            except (InvalidOperation, ValueError, TypeError, AttributeError):
                return False
            if not persisted_fee.is_finite() or not configured_fee.is_finite():
                return False
            return persisted_fee == configured_fee
        except Exception:
            return False

    async def _durable_builder_proof_ok(self, account_id: str) -> bool:
        """Structural proof check: complete snapshot bound to live identity.

        No freshness TTL and no config comparison here: a proof that aged
        out reads STALE (not BLOCKED) on the Account screen, while the
        trust rails additionally require freshness via `_builder_verified`.
        """
        try:
            row = await self._account_row(account_id)
            if row is None or str(_row_value(row, "role", "")) != "follower":
                return False
            if not _is_true(_row_value(row, "enabled", 0)):
                return False
            if str(_row_value(row, "identity_source", "direct") or "direct") != "builder":
                return False
            if not _row_value(row, "builder_address", None):
                return False
            if not _row_value(row, "builder_max_fee_rate", None):
                return False
            if not _row_value(row, "builder_verified_at", None):
                return False
            now_ms = int(self._now().timestamp() * 1000)
            try:
                snapshot_expiry = int(_row_value(row, "builder_agent_expires_at_ms", 0))
            except (TypeError, ValueError):
                return False
            if snapshot_expiry <= now_ms:
                return False
            validation = await self._validation_row(account_id)
            if validation is None:
                return False
            if (
                _row_bool(validation, "can_read") is not True
                or _row_bool(validation, "can_perp_trade") is not True
                or _row_bool(validation, "can_withdraw") is not False
            ):
                return False
            try:
                validation_expiry = int(_row_value(validation, "expired_ms", 0))
            except (TypeError, ValueError):
                return False
            if validation_expiry <= now_ms:
                return False
            current = await self._current_signer(account_id)
            stored = _row_value(validation, "signer_address", "")
            if not current or not isinstance(stored, str):
                return False
            return stored.strip().lower() == current.lower()
        except Exception:
            return False

    async def _invalidate_builder_proof(self, account_id: str) -> None:
        """Drop a disproven Builder snapshot; best-effort, never raises."""
        with contextlib.suppress(Exception):
            await self._call_optional(self._accounts, "clear_builder_snapshot", account_id)

    async def _repair_suspect_proof(self, account_id: str) -> None:
        """Drop validation/snapshot rows left by a replaced identity.

        A credential replacement that landed mid-read may leave the previous
        signer's validation behind.  When the stored signer no longer matches
        the current identity, both rows go so no old proof can be served.
        """
        with contextlib.suppress(Exception):
            current = await self._current_signer(account_id)
            validation = await self._validation_row(account_id)
            stored = _row_value(validation, "signer_address", "") if validation is not None else ""
            if (
                not current
                or not isinstance(stored, str)
                or stored.strip().lower() != current.lower()
            ):
                await self._call_optional(self._validations, "delete", account_id)
                await self._call_optional(self._accounts, "clear_builder_snapshot", account_id)

    @staticmethod
    def _builder_reason_for(code: OnboardingErrorCode) -> AccountBuilderReason:
        if code == OnboardingErrorCode.BUILDER_NOT_CONFIRMED:
            return AccountBuilderReason.BUILDER_NOT_CONFIRMED
        if code == OnboardingErrorCode.BUILDER_FEE_MISMATCH:
            return AccountBuilderReason.FEE_MISMATCH
        return AccountBuilderReason.BUILDER_MISMATCH

    async def account_builder(self, user_id: int, refresh: bool = False) -> AccountBuilderDTO:
        """Authoritative Account + Builder attestation for the nested screen."""
        try:
            context, row, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            return await self._account_builder_for(context, row, account_id, refresh=refresh)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("account builder attestation is unavailable") from None

    async def _account_builder_for(
        self, context: UserContext, row: Any, account_id: str, *, refresh: bool
    ) -> AccountBuilderDTO:
        now = self._now()
        account_masked = mask_address(str(_row_value(row, "user_address", "")))
        signer_masked = await self._identity(account_id)
        permission, permission_checked_at = await self._permission(account_id)
        source = str(_row_value(row, "identity_source", "direct") or "direct")
        if source not in {"direct", "builder"}:
            source = "direct"
        # No signed Builder refresh happens on these paths, so the timestamp
        # must be real evidence time (the last permission check), never the
        # moment the screen was opened.  Missing evidence reads None.
        evidence_check_at = permission_checked_at
        evidence_check_age = (
            max(int((now - permission_checked_at).total_seconds()), 0)
            if permission_checked_at is not None
            else 0
        )
        if not _is_true(_row_value(row, "enabled", 0)):
            return AccountBuilderDTO(
                identity_source=source,
                state=AccountBuilderState.BLOCKED,
                reason=AccountBuilderReason.ACCOUNT_DISCONNECTED,
                remediation=None,
                account_masked=account_masked,
                signer_masked=signer_masked,
                permission=permission,
                last_check_at=evidence_check_at,
                age_seconds=evidence_check_age,
            )
        if source == "direct":
            return AccountBuilderDTO(
                identity_source="direct",
                state=AccountBuilderState.NOT_APPLICABLE,
                reason=AccountBuilderReason.DIRECT_ACCOUNT,
                remediation=None,
                account_masked=account_masked,
                signer_masked=signer_masked,
                permission=permission,
                agent_expires_at_ms=_int_or_none(
                    _row_value(await self._validation_row(account_id), "expired_ms", None)
                ),
                last_check_at=evidence_check_at,
                age_seconds=evidence_check_age,
            )
        if not refresh:
            return await self._cached_builder_dto(
                account_id, row, account_masked, signer_masked, permission, now
            )
        async with self._account_lock(account_id):
            return await self._locked_builder_refresh(context, account_id, signer_masked)

    async def _locked_builder_refresh(
        self,
        context: UserContext,
        account_id: str,
        signer_masked: str | None,
    ) -> AccountBuilderDTO:
        """Locked refresh core: a pre-lock row is never trusted (C4).

        Must be called with `_account_lock(account_id)` held.  Re-reads the
        authoritative account row: a direct replacement or disconnect that
        landed after the pre-lock read fails closed here (direct
        re-evaluates to not-applicable, never promoted back to builder).
        Only a still-builder row reaches the signed reads.
        """
        fresh = await self._account_row(account_id)
        if (
            fresh is None
            or str(_row_value(fresh, "role", "")) != "follower"
            or not _is_true(_row_value(fresh, "enabled", 0))
            or str(_row_value(fresh, "identity_source", "direct") or "direct") != "builder"
        ):
            await self._repair_suspect_proof(account_id)
            # Fail closed on the fresh state: direct/disabled re-evaluates
            # to its cheap verdict; a stale pre-lock row can never commit.
            return await self._account_builder_for(context, fresh, account_id, refresh=False)
        fresh_masked = mask_address(str(_row_value(fresh, "user_address", "")))
        live_signer = await self._identity(account_id)
        permission, _ = await self._permission(account_id)
        return await self._refresh_builder_dto(
            account_id, fresh, fresh_masked, live_signer or signer_masked
        )

    async def _cached_builder_dto(
        self,
        account_id: str,
        row: Any,
        account_masked: str | None,
        signer_masked: str | None,
        permission: PermissionSummary,
        now: datetime,
    ) -> AccountBuilderDTO:
        """Cheap cached read: durable evidence only, never a fresh verdict."""
        builder_address = _row_value(row, "builder_address", None)
        builder_name = _row_value(row, "builder_name", None)
        max_fee_rate = _row_value(row, "builder_max_fee_rate", None)
        verified_at = _as_datetime(_row_value(row, "builder_verified_at", None))
        validation = await self._validation_row(account_id)
        agent_expires = _int_or_none(_row_value(validation, "expired_ms", None))
        base = {
            "identity_source": "builder",
            "account_masked": account_masked,
            "signer_masked": signer_masked,
            "builder_address_masked": (
                mask_address(str(builder_address)) if builder_address else None
            ),
            "builder_name": str(builder_name) if builder_name else None,
            "max_fee_rate": str(max_fee_rate) if max_fee_rate else None,
            "permission": permission,
            "agent_expires_at_ms": agent_expires,
            "verified_at": verified_at,
        }
        if not builder_address or not max_fee_rate or verified_at is None:
            return AccountBuilderDTO(
                **base,  # type: ignore[arg-type]
                state=AccountBuilderState.UNAVAILABLE,
                reason=AccountBuilderReason.SNAPSHOT_MISSING,
                remediation=AccountBuilderRemediation.REFRESH,
                # No snapshot, no successful evidence: never invent a check time.
                last_check_at=None,
                age_seconds=0,
            )
        # Evidence time, not wall-clock: a cached read must never make the
        # proof look younger than its last real confirmation.
        evidence_at = verified_at
        evidence_age = max(int((now - verified_at).total_seconds()), 0)
        if await self._builder_verified(account_id):
            return AccountBuilderDTO(
                **base,  # type: ignore[arg-type]
                state=AccountBuilderState.VERIFIED,
                reason=AccountBuilderReason.NONE,
                remediation=None,
                last_check_at=evidence_at,
                age_seconds=evidence_age,
            )
        if (
            agent_expires is not None
            and agent_expires - int(now.timestamp() * 1000) <= BUILDER_EXPIRY_WARNING_MS
            and await self._builder_proof_fresh(account_id)
        ):
            # Same expiring verdict as the fresh path: without this, a
            # just-refreshed expiring proof would read STALE here (the
            # shared rail is False inside the warning window by design)
            # and the reauthorization prepare gate could never open.
            return AccountBuilderDTO(
                **base,  # type: ignore[arg-type]
                state=AccountBuilderState.EXPIRING,
                reason=AccountBuilderReason.AGENT_EXPIRING,
                remediation=AccountBuilderRemediation.REAUTHORIZE,
                last_check_at=evidence_at,
                age_seconds=evidence_age,
            )
        if await self._durable_builder_proof_ok(account_id):
            # Structurally bound but not fresh (aged out) or no longer
            # matching current config: stale, never verified; an
            # authoritative refresh resolves it to VERIFIED or BLOCKED.
            return AccountBuilderDTO(
                **base,  # type: ignore[arg-type]
                state=AccountBuilderState.STALE,
                reason=AccountBuilderReason.TRANSIENT_UNAVAILABLE,
                remediation=AccountBuilderRemediation.REFRESH,
                last_check_at=evidence_at,
                age_seconds=evidence_age,
            )
        # Durable evidence disproves the proof (bad triple/expiry/signer):
        # blocked without spending a signed read.
        return AccountBuilderDTO(
            **base,  # type: ignore[arg-type]
            state=AccountBuilderState.BLOCKED,
            reason=AccountBuilderReason.AGENT_INVALID,
            remediation=AccountBuilderRemediation.REAUTHORIZE,
            last_check_at=evidence_at,
            age_seconds=evidence_age,
        )

    async def _stale_or_unavailable_dto(
        self,
        account_id: str,
        row: Any,
        account_masked: str | None,
        signer_masked: str | None,
        permission: PermissionSummary,
        now: datetime,
    ) -> AccountBuilderDTO:
        """Transient failure: keep a bound snapshot as stale, else unavailable."""
        builder_address = _row_value(row, "builder_address", None)
        builder_name = _row_value(row, "builder_name", None)
        max_fee_rate = _row_value(row, "builder_max_fee_rate", None)
        verified_at = _as_datetime(_row_value(row, "builder_verified_at", None))
        validation = await self._validation_row(account_id)
        agent_expires = _int_or_none(_row_value(validation, "expired_ms", None))
        current = await self._current_signer(account_id)
        stored = _row_value(validation, "signer_address", "") if validation is not None else ""
        bound = (
            bool(builder_address)
            and bool(max_fee_rate)
            and verified_at is not None
            and validation is not None
            and bool(current)
            and isinstance(stored, str)
            and stored.strip().lower() == current.lower()
        )
        if bound:
            age = max(int((now - verified_at).total_seconds()), 0) if verified_at is not None else 0
            return AccountBuilderDTO(
                identity_source="builder",
                state=AccountBuilderState.STALE,
                reason=AccountBuilderReason.TRANSIENT_UNAVAILABLE,
                remediation=AccountBuilderRemediation.REFRESH,
                account_masked=account_masked,
                signer_masked=signer_masked,
                builder_address_masked=mask_address(str(builder_address)),
                builder_name=str(builder_name) if builder_name else None,
                max_fee_rate=str(max_fee_rate),
                permission=permission,
                agent_expires_at_ms=agent_expires,
                verified_at=verified_at,
                # A transient failure is not new evidence: keep the time of
                # the last real confirmation, never wall-clock.
                last_check_at=verified_at,
                age_seconds=age,
            )
        return AccountBuilderDTO(
            identity_source="builder",
            state=AccountBuilderState.UNAVAILABLE,
            reason=AccountBuilderReason.SNAPSHOT_MISSING,
            remediation=AccountBuilderRemediation.REFRESH,
            account_masked=account_masked,
            signer_masked=signer_masked,
            permission=permission,
            agent_expires_at_ms=agent_expires,
            # No bound snapshot, no successful evidence: never invent one.
            last_check_at=None,
            age_seconds=0,
        )

    async def _identity_changed_dto(
        self,
        account_masked: str | None,
        signer_masked: str | None,
        permission: PermissionSummary,
    ) -> AccountBuilderDTO:
        """Fail-closed verdict when the identity moved mid-read (C4).

        No builder fields, no invented evidence time: the caller must
        refresh again against the new identity.
        """
        return AccountBuilderDTO(
            identity_source="builder",
            state=AccountBuilderState.STALE,
            reason=AccountBuilderReason.IDENTITY_CHANGED,
            remediation=AccountBuilderRemediation.REFRESH,
            account_masked=account_masked,
            signer_masked=signer_masked,
            permission=permission,
            last_check_at=None,
            age_seconds=0,
        )

    async def _refresh_builder_dto(
        self,
        account_id: str,
        row: Any,
        account_masked: str | None,
        signer_masked: str | None,
    ) -> AccountBuilderDTO:
        """Authoritative refresh: signed Agent + Builder reads, race-gated store."""
        try:
            version_before = await self._account_version(account_id)
        except ServiceUnavailableError:
            raise
        except _SAFE_ERRORS:
            raise
        try:
            await self._refresh_permission_snapshot(account_id)
        except StaleOperationError:
            # Proven-invalid Agent: the old proof dies here, never served again.
            await self._invalidate_builder_proof(account_id)
            permission, _ = await self._permission(account_id)
            validation = await self._validation_row(account_id)
            return AccountBuilderDTO(
                identity_source="builder",
                state=AccountBuilderState.BLOCKED,
                reason=AccountBuilderReason.AGENT_INVALID,
                remediation=AccountBuilderRemediation.REAUTHORIZE,
                account_masked=account_masked,
                signer_masked=signer_masked,
                permission=permission,
                agent_expires_at_ms=_int_or_none(_row_value(validation, "expired_ms", None)),
                # Proof invalidated, no successful evidence: never invent one.
                last_check_at=None,
                age_seconds=0,
            )
        except ServiceUnavailableError:
            permission, _ = await self._permission(account_id)
            return await self._stale_or_unavailable_dto(
                account_id, row, account_masked, signer_masked, permission, self._now()
            )
        expected = self._builder_expected()
        if expected is None:
            raise ServiceUnavailableError("builder attestation is not configured")
        expected_address, _, expected_fee = expected
        try:
            records = await self._call_required(self._runtime, "refresh_builder_record", account_id)
        except ServiceUnavailableError:
            permission, _ = await self._permission(account_id)
            return await self._stale_or_unavailable_dto(
                account_id, row, account_masked, signer_masked, permission, self._now()
            )
        except AsterError:
            # The exchange answered with an error, not a proof: never verified.
            permission, _ = await self._permission(account_id)
            return await self._stale_or_unavailable_dto(
                account_id, row, account_masked, signer_masked, permission, self._now()
            )
        try:
            record = validate_builder_record(
                records if isinstance(records, list) else [],
                user_address=str(_row_value(row, "user_address", "")),
                builder_address=expected_address,
                max_fee_rate=expected_fee,
            )
        except OnboardingError as exc:
            await self._invalidate_builder_proof(account_id)
            permission, _ = await self._permission(account_id)
            validation = await self._validation_row(account_id)
            return AccountBuilderDTO(
                identity_source="builder",
                state=AccountBuilderState.BLOCKED,
                reason=self._builder_reason_for(exc.code),
                remediation=AccountBuilderRemediation.REAUTHORIZE,
                account_masked=account_masked,
                signer_masked=signer_masked,
                permission=permission,
                agent_expires_at_ms=_int_or_none(_row_value(validation, "expired_ms", None)),
                # Proof invalidated, no successful evidence: never invent one.
                last_check_at=None,
                age_seconds=0,
            )
        try:
            version_after = await self._account_version(account_id)
        except ServiceUnavailableError:
            # Credentials vanished mid-read (disconnect won the race).
            permission, _ = await self._permission(account_id)
            return AccountBuilderDTO(
                identity_source="builder",
                state=AccountBuilderState.BLOCKED,
                reason=AccountBuilderReason.ACCOUNT_DISCONNECTED,
                remediation=None,
                account_masked=account_masked,
                signer_masked=signer_masked,
                permission=permission,
                last_check_at=None,
                age_seconds=0,
            )
        if version_after != version_before:
            await self._repair_suspect_proof(account_id)
            permission, _ = await self._permission(account_id)
            return await self._identity_changed_dto(account_masked, signer_masked, permission)
        # Explicit source-transition binding: the version marker covers
        # credentials/validation, but a builder→direct→builder swing must
        # never let a stale pre-lock row promote direct back to builder.
        # Re-read the authoritative row right before the durable commit.
        commit_row = await self._account_row(account_id)
        if (
            commit_row is None
            or str(_row_value(commit_row, "role", "")) != "follower"
            or not _is_true(_row_value(commit_row, "enabled", 0))
            or str(_row_value(commit_row, "identity_source", "direct") or "direct") != "builder"
            or str(_row_value(commit_row, "user_address", ""))
            != str(_row_value(row, "user_address", ""))
        ):
            await self._repair_suspect_proof(account_id)
            permission, _ = await self._permission(account_id)
            return await self._identity_changed_dto(account_masked, signer_masked, permission)
        # The proof belongs to the current identity: persist it durably.
        moment = self._now()
        moment_ms = int(moment.timestamp() * 1000)
        validation = await self._validation_row(account_id)
        # Just persisted as an int by _refresh_permission_snapshot above; a
        # missing row (None) falls back to 0, which reads as expiring.
        agent_expiry_ms = int(_row_value(validation, "expired_ms", 0) or 0)
        record_builder = _row_value(record, "builder_address", expected_address)
        record_name = _row_value(record, "builder_name", None)
        record_fee = _row_value(record, "max_fee_rate", expected_fee)
        fresh_signer = await self._identity(account_id)
        if fresh_signer is not None:
            signer_masked = fresh_signer
        await self._call_required(
            self._accounts,
            "set_builder_snapshot",
            account_id,
            builder_address=str(record_builder),
            builder_name=str(record_name) if record_name else expected[1],
            builder_max_fee_rate=str(record_fee),
            builder_agent_expires_at_ms=agent_expiry_ms,
            builder_verified_at=moment.isoformat(),
        )
        permission, _ = await self._permission(account_id)
        if agent_expiry_ms - moment_ms <= BUILDER_EXPIRY_WARNING_MS:
            return AccountBuilderDTO(
                identity_source="builder",
                state=AccountBuilderState.EXPIRING,
                reason=AccountBuilderReason.AGENT_EXPIRING,
                remediation=AccountBuilderRemediation.REAUTHORIZE,
                account_masked=account_masked,
                signer_masked=signer_masked,
                builder_address_masked=mask_address(str(record_builder)),
                builder_name=str(record_name) if record_name else expected[1],
                max_fee_rate=str(record_fee),
                permission=permission,
                agent_expires_at_ms=agent_expiry_ms,
                verified_at=moment,
                last_check_at=moment,
                age_seconds=0,
            )
        return AccountBuilderDTO(
            identity_source="builder",
            state=AccountBuilderState.VERIFIED,
            reason=AccountBuilderReason.NONE,
            remediation=None,
            account_masked=account_masked,
            signer_masked=signer_masked,
            builder_address_masked=mask_address(str(record_builder)),
            builder_name=str(record_name) if record_name else expected[1],
            max_fee_rate=str(record_fee),
            permission=permission,
            agent_expires_at_ms=agent_expiry_ms,
            verified_at=moment,
            last_check_at=moment,
            age_seconds=0,
        )

    async def builder_reauthorize_prepare(self, user_id: int) -> BuilderPrepareDTO:
        """Start a same-signer re-approval; allowed only from a typed state.

        Eligibility is determined by an authoritative locked refresh, never
        by a cached read: after an authoritative BLOCKED the old snapshot is
        already invalidated, and only a fresh signed verdict can prove that
        reauthorization is actually required.  Before any wallet payload is
        issued the account is paused through the same safety seam as
        `pause()` (durable flag + runtime drain); a pause failure fails
        closed with no payload.  The account stays paused after submit.
        """
        try:
            await self._ensure_testnet()
            if self._builder_onboarding is None:
                raise ServiceUnavailableError("builder onboarding is unavailable")
            context, row, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            if not _is_true(_row_value(row, "enabled", 0)):
                raise ValidationError("account is disconnected")
            if str(_row_value(row, "identity_source", "direct") or "direct") != "builder":
                raise ValidationError("reauthorization requires a builder account")
            # Fail fast before any mutation when the runtime cannot drain.
            self._require_method(self._runtime, "pause_account")
            async with self._account_lock(account_id):
                signer_masked = await self._identity(account_id)
                current = await self._locked_builder_refresh(context, account_id, signer_masked)
                if current.state is AccountBuilderState.BLOCKED and (
                    current.reason is AccountBuilderReason.ACCOUNT_DISCONNECTED
                ):
                    raise ValidationError("account is disconnected")
                if current.state not in {
                    AccountBuilderState.EXPIRING,
                    AccountBuilderState.BLOCKED,
                }:
                    # VERIFIED, STALE, UNAVAILABLE and NOT_APPLICABLE carry
                    # no proven cause: refresh first, reauthorize never.
                    raise ValidationError("reauthorization is not required")
                await self._call_required(
                    self._settings, "patch_policy", account_id, allow_new_entries=False
                )
                # Same safety semantics as pause(): the durable flag alone
                # cannot stop an admitted generation; drain it first.  A
                # failure here fails closed: entries stay forbidden and no
                # wallet payload is issued below.
                await self._call_required(self._runtime, "pause_account", account_id)
                try:
                    result = await self._call_required(
                        self._builder_onboarding, "reauthorize_prepare", context.user_id
                    )
                except OnboardingError as exc:
                    raise OnboardingApplicationError(exc.code) from None
            return BuilderPrepareDTO(
                pending_id=result.pending_id,
                user_masked=result.masked_user_address,
                agent_masked=result.masked_signer_address,
                builder_masked=result.builder_masked,
                builder_name=result.builder_name,
                max_fee_rate=result.max_fee_rate,
                signature_plan=result.signature_plan,
                can_perp_trade=result.can_perp_trade,
                can_withdraw=result.can_withdraw,
                agent_expires_at_ms=result.agent_expires_at_ms,
                approval_expires_at_ms=result.approval_expires_at_ms,
                eip712=result.eip712,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("builder reauthorization is unavailable") from None

    async def builder_reauthorize_submit(
        self, user_id: int, pending_id: str, signature: str
    ) -> BuilderReauthorizedDTO:
        """Complete a same-signer re-approval; the account stays paused."""
        try:
            await self._ensure_testnet()
            if self._builder_onboarding is None:
                raise ServiceUnavailableError("builder onboarding is unavailable")
            if (
                not isinstance(pending_id, str)
                or not pending_id.strip()
                or not isinstance(signature, str)
                or not signature.strip()
            ):
                raise ValidationError("builder approval is invalid")
            context = await self._context(user_id)
            try:
                result = await self._call_required(
                    self._builder_onboarding,
                    "reauthorize_submit",
                    BuilderSubmitRequest(
                        telegram_user_id=context.user_id,
                        pending_id=pending_id.strip(),
                        signature=signature.strip(),
                    ),
                )
            except OnboardingError as exc:
                raise OnboardingApplicationError(exc.code) from None
            if str(result.account_id) != stable_account_id(context.user_id) or not result.paused:
                raise ValidationError("reauthorization result is invalid")
            return BuilderReauthorizedDTO(
                account_id=str(result.account_id),
                account_masked=str(result.account_masked),
                signer_masked=str(result.signer_masked),
                builder_name=str(result.builder_name),
                max_fee_rate=str(result.max_fee_rate),
                agent_expires_at_ms=int(result.agent_expires_at_ms),
                paused=True,
                message="builder authorization renewed paused",
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("builder reauthorization is unavailable") from None

    async def risk(self, user_id: int) -> RiskDTO:
        try:
            _, _, _ = await self._owned_account(user_id)
            policy = await self._call_required(
                self._settings, "get_policy", str((await self._context(user_id)).account_id)
            )
            return self._risk_dto(policy)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("risk settings are unavailable") from None

    async def activity(
        self,
        user_id: int,
        page: int = 0,
        filter: ActivityFilter | str = ActivityFilter.ALL,
    ) -> ActivityPage:
        """Temporary old-WebView adapter over user events, never commands.

        Remove with the legacy HTTP route once pre-S3 WebViews are retired.
        Numbered pages replay the pinned cursor within this bounded request;
        current clients use activity_feed and retain its cursor directly.
        """
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 50:
            raise ValidationError("activity page is invalid")
        activity_filter = _coerce_activity_filter(filter)
        cursor = None
        feed = await self.activity_feed(user_id, filter=activity_filter, limit=10)
        for _ in range(page):
            cursor = feed.next_cursor
            if cursor is None:
                return ActivityPage(page=page, has_previous=page > 0, filter=activity_filter)
            feed = await self.activity_feed(
                user_id, filter=activity_filter, cursor=cursor, limit=10
            )
        return ActivityPage(
            items=tuple(
                ActivityItem(
                    category=(
                        ActivityCategory.TRADE
                        if item.kind in feed_kinds(ActivityFilter.TRADE)
                        else ActivityCategory.SYSTEM
                    ),
                    title=item.text,
                    status=ActivityStatus.COMPLETED,
                    symbol=_safe_activity_symbol(item.facts),
                    occurred_at=item.occurred_at,
                )
                for item in feed.items
            ),
            page=page,
            has_previous=page > 0,
            has_next=feed.next_cursor is not None,
            filter=activity_filter,
        )

    # --- S1.4 history contracts (plan §10.8 B7, §11) --------------------------

    async def _history_environment(self) -> str:
        """History scope environment derived like the testnet boundary."""
        check = self._testnet_check
        if check is not None:
            result = check() if callable(check) else check
            return "testnet" if _is_true(await _maybe_await(result)) else "mainnet"
        cfg = getattr(self._runtime, "cfg", None)
        if cfg is not None:
            rest = str(getattr(cfg, "rest_base_url", "")).rstrip("/")
            return "testnet" if rest == TESTNET_REST_URL else "mainnet"
        marker = getattr(self._runtime, "testnet", _MISSING)
        return "testnet" if marker is not _MISSING and _is_true(marker) else "mainnet"

    def _require_history(
        self,
    ) -> tuple[EpisodeRepository, EventRepository, HistoryRepository]:
        if self._episodes is None or self._user_events is None or self._history is None:
            raise ServiceUnavailableError("trade history is unavailable")
        return self._episodes, self._user_events, self._history

    async def _quantity_assets(
        self, account_id: str, symbols: Sequence[str]
    ) -> dict[str, str | None]:
        """Per-symbol quantity units; unknown stays None, never guessed."""
        unique = list(dict.fromkeys(symbols))
        if not unique:
            return {}
        result = await self._call_optional(self._runtime, "symbol_assets", account_id, unique)
        if result is _MISSING or not isinstance(result, Mapping):
            return dict.fromkeys(unique)
        assets = quantity_assets(unique, result)
        return {
            symbol: asset if isinstance(asset, str) and asset else None
            for symbol, asset in assets.items()
        }

    async def _trade_money(
        self, scope: HistoryScope, refs: Sequence[str]
    ) -> dict[str, EpisodeMoney]:
        """Proof-driven money for owned refs; empty in, empty out."""
        if not refs:
            return {}
        episodes, _, history = self._require_history()
        batch = await load_episode_money(
            scope, tuple(refs), history_repo=history, episodes_repo=episodes
        )
        return dict(batch.money)

    async def trades(
        self,
        user_id: int,
        *,
        period: str = "7d",
        cursor: str | None = None,
        limit: int | None = None,
        symbol: str | None = None,
    ) -> TradeListPage:
        """Owned closed episodes, newest first, over a stable cursor."""
        try:
            context, _, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            episodes, _, history = self._require_history()
            page_limit = self._trade_page_size if limit is None else limit
            if isinstance(page_limit, bool) or not isinstance(page_limit, int):
                raise ValidationError("trade page limit is invalid")
            if not 1 <= page_limit <= 50:
                raise ValidationError("trade page limit is invalid")
            if not isinstance(period, str) or period not in _TRADE_PERIODS:
                raise ValidationError("trade period is invalid")
            if cursor is not None and not isinstance(cursor, str):
                raise ValidationError("trade cursor is invalid")
            if symbol is not None and (not isinstance(symbol, str) or not symbol):
                raise ValidationError("trade symbol filter is invalid")
            days = _TRADE_PERIODS[period]
            since_ms = None
            if days is not None:
                since_ms = int(self._now().timestamp() * 1000) - days * 86_400_000
            scope = HistoryScope(account_id, await self._history_environment())
            try:
                stored, next_cursor = await episodes.list_closed(
                    scope, symbol=symbol, since_ms=since_ms, cursor=cursor, limit=page_limit
                )
            except ValueError as exc:
                raise ValidationError("trade cursor is invalid") from exc
            assets = await self._quantity_assets(
                account_id, [item.episode.symbol for item in stored]
            )
            coverage = await history.coverage_from(scope)
            now = self._now()
            money = await self._trade_money(scope, [item.episode.public_ref for item in stored])
            return TradeListPage(
                items=tuple(
                    present_trade(
                        item.episode,
                        quantity_asset=assets.get(item.episode.symbol),
                        money=money.get(item.episode.public_ref),
                        revision=item.revision,
                        as_of=now,
                    )
                    for item in stored
                ),
                next_cursor=next_cursor,
                as_of=now,
                coverage_from=datetime.fromtimestamp(coverage / 1000, tz=UTC)
                if coverage is not None
                else None,
                coverage_complete=False,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("trades are unavailable") from None

    async def trade(self, user_id: int, trade_ref: str) -> TradeDetailDTO | TradeResolutionDTO:
        """One owned episode by opaque ref; foreign and missing read alike."""
        try:
            context, _, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            episodes, _, _ = self._require_history()
            if not isinstance(trade_ref, str) or not trade_ref:
                raise ValidationError("trade reference is invalid")
            scope = HistoryScope(account_id, await self._history_environment())
            try:
                owned = await resolve_owned_ref(episodes, scope, trade_ref)
            except ValueError as exc:
                raise ValidationError("trade reference is invalid") from exc
            if owned is None:
                raise TradeUnavailableError("trade is unavailable")
            if isinstance(owned, TradeResolutionDTO):
                return owned
            stored = owned.stored
            assets = await self._quantity_assets(account_id, [stored.episode.symbol])
            now = self._now()
            money = await self._trade_money(scope, [owned.canonical_ref])
            return present_trade_detail(
                stored,
                quantity_asset=assets.get(stored.episode.symbol),
                money=money.get(owned.canonical_ref),
                as_of=now,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("trade is unavailable") from None

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
    ) -> TradeEventsPage:
        """Fills and user events of one owned episode family (C2, plan §23.3).

        Fills come from the allocation index, never a time range: overlapping
        HEDGE legs stay separate and a shared flip fill shows only the
        episode share. Fills and events page independently (default 20, max
        50 each); the legacy bare ``cursor`` pages events only.
        """
        try:
            context, _, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            episodes, events, _ = self._require_history()
            if not isinstance(trade_ref, str) or not trade_ref:
                raise ValidationError("trade reference is invalid")
            if fills_cursor is not None and (not isinstance(fills_cursor, str) or not fills_cursor):
                raise ValidationError("trade events fills cursor is invalid")
            after_seq = events_cursor if events_cursor is not None else cursor
            if isinstance(after_seq, str):
                if not after_seq.isdigit():
                    raise ValidationError("trade events cursor is invalid")
                after_seq = int(after_seq)
            if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
                raise ValidationError("trade events cursor is invalid")
            fills_page = self._trade_page_size if fills_limit is None else fills_limit
            if limit is not None and fills_limit is None:
                fills_page = limit
            events_page = self._trade_page_size if events_limit is None else events_limit
            if limit is not None and events_limit is None:
                events_page = limit
            for page in (fills_page, events_page):
                if isinstance(page, bool) or not isinstance(page, int):
                    raise ValidationError("trade events page limit is invalid")
                if not 1 <= page <= 50:
                    raise ValidationError("trade events page limit is invalid")
            scope = HistoryScope(account_id, await self._history_environment())
            try:
                owned = await resolve_owned_ref(episodes, scope, trade_ref)
            except ValueError as exc:
                raise ValidationError("trade reference is invalid") from exc
            if owned is None:
                raise TradeUnavailableError("trade is unavailable")
            if isinstance(owned, TradeResolutionDTO):
                raise TradeSplitError("trade split into several episodes")
            try:
                allocated, next_fills = await episodes.list_allocated_fills(
                    scope, owned.family_refs, fills_cursor, fills_page
                )
            except EpisodeRevisionConflict as exc:
                raise HistoryPageChangedError("trade history changed under this page") from exc
            except ValueError as exc:
                raise ValidationError("trade events fills cursor is invalid") from exc
            rows = await events.list_by_family(
                scope, owned.family_refs, after_seq=after_seq, limit=events_page + 1
            )
            shown = rows[:events_page]
            next_seq = str(shown[-1].seq) if len(rows) > events_page else None
            return TradeEventsPage(
                fills=tuple(present_allocated_fill(row) for row in allocated),
                events=tuple(present_event(row) for row in shown),
                next_cursor=next_seq,
                fills_cursor=next_fills,
                events_cursor=next_seq,
                next_fills_cursor=next_fills,
                next_events_cursor=next_seq,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("trade events are unavailable") from None

    async def activity_feed(
        self,
        user_id: int,
        *,
        filter: ActivityFilter | str = ActivityFilter.ALL,
        cursor: int | str | None = 0,
        limit: int | None = None,
    ) -> ActivityFeedPage:
        """Structured user-event feed over a pinned snapshot plus pending ops.

        The first page (``cursor`` omitted, ``None``, or a legacy bare int)
        pins ``high_water`` to the current max seq and reads ``seq <=
        high_water`` newest first (``occurred_at DESC, seq DESC``). A
        continuation passes the opaque ``v1:`` cursor holding the scope
        token, the pinned filter, the pinned high water, and the last
        ``(occurred_at, seq)`` tuple; rows above the water never interleave.
        ``latest_seq`` is the live max at response time so the UI can banner
        new events. The cursor is never authorization: ownership is
        re-checked on every request and a foreign scope token fails closed.

        Feed rows are immutable events; a correction arrives as a new
        ``HISTORY_CORRECTED`` row above a fresh high water, so the page
        carries no episode/coverage revision. Facts revisions live
        per-symbol (and coverage per stream/symbol); no single cheap
        account-level revision exists, and inventing a ``MAX()`` aggregate
        would signal change when the feed did not move. A
        correction-model ``history_page_changed`` signal is therefore
        C1-resolution-driven future work, not part of this snapshot.
        """
        try:
            context, _, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            _, events, _ = self._require_history()
            activity_filter = _coerce_activity_filter(filter)
            page_limit = self._trade_page_size if limit is None else limit
            if isinstance(page_limit, bool) or not isinstance(page_limit, int):
                raise ValidationError("feed page limit is invalid")
            if not 1 <= page_limit <= 50:
                raise ValidationError("feed page limit is invalid")
            scope = HistoryScope(account_id, await self._history_environment())
            kinds = feed_kinds(activity_filter)
            if (
                cursor is None
                or isinstance(cursor, int)
                or (isinstance(cursor, str) and cursor.isdigit())
            ):
                # Legacy seq-only callers: any bare int (or digit string, or
                # omitted cursor) is a first-page request that pins now. The
                # numeric value is not a position; continuation needs v1.
                if isinstance(cursor, bool):
                    raise ValidationError("feed cursor is invalid")
                if isinstance(cursor, int) and cursor < 0:
                    raise ValidationError("feed cursor is invalid")
                try:
                    rows, pinned = await events.list_feed_page(
                        scope,
                        kinds=kinds,
                        high_water=None,
                        last_occurred_at=None,
                        last_seq=None,
                        limit=page_limit + 1,
                    )
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                latest = await events.max_seq(scope)
                shown = rows[:page_limit]
                if len(rows) > page_limit:
                    last = shown[-1]
                    next_cursor: str | None = _encode_feed_cursor(
                        scope=scope,
                        filter_value=activity_filter.value,
                        high_water=pinned,
                        last_occurred_at=last.event.occurred_at,
                        last_seq=last.seq,
                    )
                else:
                    next_cursor = None
                return ActivityFeedPage(
                    items=tuple(present_event(row) for row in shown),
                    next_cursor=next_cursor,
                    high_water=pinned,
                    latest_seq=latest,
                    pending=await self._pending_operations(account_id),
                )
            if isinstance(cursor, str) and cursor.startswith(_FEED_CURSOR_PREFIX):
                try:
                    scope_token, cursor_filter, pinned, last_occurred_at, last_seq = (
                        _decode_feed_cursor(cursor)
                    )
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                if scope_token != f"{scope.account_id}:{scope.environment}":
                    raise ValidationError("feed cursor scope mismatch")
                if cursor_filter != activity_filter.value:
                    raise ValidationError("feed cursor filter mismatch")
                try:
                    rows, _ = await events.list_feed_page(
                        scope,
                        kinds=kinds,
                        high_water=pinned,
                        last_occurred_at=last_occurred_at,
                        last_seq=last_seq,
                        limit=page_limit + 1,
                    )
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                latest = await events.max_seq(scope)
                shown = rows[:page_limit]
                if len(rows) > page_limit:
                    last = shown[-1]
                    next_cursor = _encode_feed_cursor(
                        scope=scope,
                        filter_value=activity_filter.value,
                        high_water=pinned,
                        last_occurred_at=last.event.occurred_at,
                        last_seq=last.seq,
                    )
                else:
                    next_cursor = None
                return ActivityFeedPage(
                    items=tuple(present_event(row) for row in shown),
                    next_cursor=next_cursor,
                    high_water=pinned,
                    latest_seq=latest,
                    pending=await self._pending_operations(account_id),
                )
            raise ValidationError("feed cursor is invalid")
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("activity feed is unavailable") from None

    async def _pending_operations(self, account_id: str) -> tuple[PendingOperation, ...]:
        """Unsettled journal commands as safe pending facts, never payloads."""
        if self._commands is None:
            return ()
        rows = await self._commands.in_flight_for(account_id)
        now = self._now()
        pending: list[PendingOperation] = []
        for row in rows:
            symbol = _payload_symbol(_row_value(row, "payload"))
            status = str(_row_value(row, "status", ""))
            moment = _as_datetime(_row_value(row, "updated_at", _row_value(row, "created_at")))
            pending.append(
                PendingOperation(
                    symbol=symbol,
                    operation=_PENDING_OPERATIONS.get(
                        str(_row_value(row, "kind", "")), "operation"
                    ),
                    state="unknown" if status == "UNKNOWN" else "pending",
                    age_seconds=max(int((now - moment).total_seconds()), 0)
                    if moment is not None
                    else 0,
                )
            )
        return tuple(pending)

    async def _overview_for(
        self,
        account_id: str,
        snapshot: Any,
        snapshot_captured_at: datetime | None,
        system_state: SystemState,
    ) -> AccountOverview | None:
        """Dashboard aggregate over one consistent snapshot, or None."""
        if snapshot is None:
            return None
        snapshot_ttl_s = self._snapshot_ttl_seconds()
        cache_expired = snapshot_captured_at is not None and (
            (self._now() - snapshot_captured_at).total_seconds() > snapshot_ttl_s
        )
        fresh = (
            snapshot_captured_at is not None
            and not cache_expired
            and system_state is SystemState.READY
        )
        cfg = getattr(self._runtime, "cfg", None)
        fee_buffer_pct = Decimal(str(getattr(cfg, "fee_buffer_pct", "0.001")))
        parsed: list[PositionSummaryDTO] = []
        for position in self._raw_positions(snapshot):
            summary = self._parse_position_summary(
                position, is_fresh=fresh, fee_buffer_pct=fee_buffer_pct
            )
            if summary is not None:
                parsed.append(summary)
        return build_overview(
            parsed,
            available_usdt=self._available_usdt(snapshot),
            as_of=snapshot_captured_at,
            freshness=SnapshotFreshness.FRESH if fresh else SnapshotFreshness.STALE,
        )

    @staticmethod
    def _raw_positions(snapshot: Any) -> list[Any]:
        positions_map = _row_value(snapshot, "positions", _MISSING)
        if positions_map is _MISSING or positions_map is None:
            return []
        if isinstance(positions_map, Mapping):
            return list(positions_map.values())
        return list(positions_map)

    @staticmethod
    def _available_usdt(snapshot: Any) -> Decimal | None:
        raw = _row_value(snapshot, "available_usdt", None)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return value if value.is_finite() and value >= 0 else None

    async def notification_settings(self, user_id: int) -> NotificationSettingsDTO:
        try:
            context = await self._context(user_id)
            row = await self._user_row(context.user_id)
            summary = await self._call_required(self._outbox, "counts_for_user", context.user_id)
            return NotificationSettingsDTO(
                enabled=bool(_row_value(row, "notifications_enabled", 1)),
                last_delivery_at=_as_datetime(_row_value(summary, "last_sent_at")),
                pending=int(_row_value(summary, "PENDING", 0) or 0),
                failed=int(_row_value(summary, "FAILED", 0) or 0),
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("notification settings are unavailable") from None

    @_control_audit_boundary("set_notifications")
    async def set_notifications(self, user_id: int, enabled: bool) -> NotificationSettingsDTO:
        try:
            if isinstance(enabled, bool) is False:
                raise ValidationError("notification setting is invalid")
            context = await self._context(user_id)
            await self._call_required(
                self._users, "set_notifications_enabled", context.user_id, enabled
            )
            return await self.notification_settings(context.user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("notification setting is unavailable") from None

    @staticmethod
    def _validate_credentials(credentials: DirectCredentialsInput) -> None:
        if not isinstance(credentials, DirectCredentialsInput):
            raise ValidationError("credential input is invalid")
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                credentials.user_address,
                credentials.signer_address,
                credentials.signer_private_key,
            )
        ):
            raise ValidationError("credential input is invalid")

    @staticmethod
    def _audit_account_id(user_id: Any) -> str:
        """Derive only a stable sanitized account id for an audit event."""
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return "unknown"
        try:
            return stable_account_id(user_id)
        except (TypeError, ValueError):
            return "unknown"

    @staticmethod
    def _audit_failure(exc: BaseException) -> tuple[str, Any, str | None]:
        """Map an application failure to safe category/code/type fields."""
        if isinstance(exc, OnboardingApplicationError):
            return "validation", exc.code, None
        if isinstance(exc, OnboardingError):
            return "validation", exc.code, None
        if isinstance(exc, ValidationError):
            return "validation", "invalid_input", None
        if isinstance(exc, AuthorizationError):
            return "authorization", "not_authorized", None
        if isinstance(exc, GlobalEmergencyActiveError):
            return "service_unavailable", "global_emergency_active", None
        if isinstance(exc, GlobalPauseActiveError):
            return "service_unavailable", "global_pause_active", None
        if isinstance(exc, ServiceUnavailableError):
            return "service_unavailable", "service_unavailable", None
        if isinstance(exc, _SAFE_ERRORS):
            return "validation", getattr(exc, "public_code", "application_error"), None
        # The exception text is intentionally discarded: transport/client
        # exceptions may echo request payloads or exchange response bodies.
        return "unexpected", "unexpected", type(exc).__name__

    @staticmethod
    def _audit_rejected(
        *,
        mode: str,
        user_id: Any,
        account_id: Any,
        exc: BaseException,
    ) -> None:
        category, code, exception_type = UserApplicationService._audit_failure(exc)
        log_onboarding_audit(
            log,
            event="rejected",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="rejected",
            paused=None,
            category=category,
            code=code,
            exception_type=exception_type,
        )

    @staticmethod
    def _control_audit(
        *,
        action: str,
        result: str,
        user_id: Any,
        account_id: Any = None,
        target_user_id: Any = None,
        changed_fields: Iterable[Any] | None = None,
        state: Any = None,
        category: Any = None,
        code: Any = None,
        exception_type: Any = None,
    ) -> None:
        """Best-effort audit seam for state-changing application actions."""
        try:
            log_control_audit(
                log,
                action=action,
                result=result,
                user_id=user_id,
                account_id=account_id,
                target_user_id=target_user_id,
                changed_fields=changed_fields,
                state=state,
                category=category,
                code=code,
                exception_type=exception_type,
            )
        except Exception:
            # A diagnostic sink is never allowed to change a control result.
            return

    @staticmethod
    def _control_attempt(
        action: str,
        user_id: Any,
        account_id: Any,
        target_user_id: Any = None,
    ) -> None:
        UserApplicationService._control_audit(
            action=action,
            result="attempt",
            user_id=user_id,
            account_id=account_id,
            target_user_id=target_user_id,
        )

    @staticmethod
    def _control_success(
        value: Any,
        *,
        action: str,
        user_id: Any,
        account_id: Any,
        target_user_id: Any = None,
        changed_fields: Iterable[Any] | None = None,
        state: Any = None,
    ) -> Any:
        UserApplicationService._control_audit(
            action=action,
            result="success",
            user_id=user_id,
            account_id=account_id,
            target_user_id=target_user_id,
            changed_fields=changed_fields,
            state=state,
        )
        return value

    @staticmethod
    def _control_rejected(
        *,
        action: str,
        user_id: Any,
        account_id: Any,
        exc: BaseException,
        target_user_id: Any = None,
    ) -> None:
        category, code, exception_type = UserApplicationService._audit_failure(exc)
        UserApplicationService._control_audit(
            action=action,
            result="rejected",
            user_id=user_id,
            account_id=account_id,
            target_user_id=target_user_id,
            category=category,
            code=code,
            exception_type=exception_type,
        )

    @staticmethod
    def _confirmation_audit_action(value: Any) -> str:
        """Map confirmation actions to fixed audit labels without token data."""
        try:
            action = ConfirmationAction(value)
        except (TypeError, ValueError):
            return "confirm_action"
        return {
            ConfirmationAction.RESUME: "confirm_resume",
            ConfirmationAction.EMERGENCY: "confirm_emergency",
            ConfirmationAction.DISCONNECT: "confirm_disconnect",
            ConfirmationAction.GLOBAL_PAUSE: "confirm_global_pause",
            ConfirmationAction.GLOBAL_RESUME: "confirm_global_resume",
            ConfirmationAction.GLOBAL_EMERGENCY: "confirm_global_emergency",
        }.get(action, "confirm_action")

    async def _onboard(self, request: DirectOnboardingRequest, *, replace: bool) -> Any:
        service = self._onboarding
        if service is None:
            raise ServiceUnavailableError("direct onboarding adapter is unavailable")
        if replace:
            preflight = await self._call_required(service, "preflight", request)
            result = await self._call_required(service, "replace", request, preflight=preflight)
        else:
            result = await self._call_required(service, "onboard", request)
        return result

    @staticmethod
    def _onboarding_result(
        result: Any,
        request: DirectOnboardingRequest,
    ) -> OnboardingResult:
        if isinstance(result, OnboardingResult):
            if (
                result.account_id != stable_account_id(request.telegram_user_id)
                or not result.paused
            ):
                raise ValidationError("onboarding must remain paused")
            return result
        if isinstance(result, DirectOnboardingResult):
            account_id = result.account_id
            account_masked = result.masked_user_address
            signer_masked = result.masked_signer_address
            paused = result.paused
        else:
            raise ValidationError("onboarding result is invalid")
        if str(account_id) != stable_account_id(request.telegram_user_id) or not paused:
            raise ValidationError("onboarding result is invalid")
        return OnboardingResult(
            account_id=str(account_id),
            account_masked=str(account_masked or mask_address(request.user_address)),
            signer_masked=str(signer_masked or mask_address(request.signer_address)),
            paused=True,
            message="account saved paused",
        )

    async def connect_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        mode = "connect"
        account_id: Any = self._audit_account_id(user_id)
        log_onboarding_audit(
            log,
            event="attempt",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="started",
            paused=None,
        )
        try:
            await self._ensure_testnet()
            self._validate_credentials(credentials)
            context = await self._context(user_id)
            if context.account_id:
                account_id = str(context.account_id)
                current = await self._account_row(account_id)
                if current is not None and _is_true(_row_value(current, "enabled", 0)):
                    if await self._runtime_registered(account_id):
                        raise ValidationError("connected account requires replacement")
                    # The encrypted commit may have succeeded immediately
                    # before runtime publication failed.  Retry only that
                    # publication after the adapter proves the submitted
                    # public identity still matches the durable credentials.
                    self._require_method(self._runtime, "recover_account_runtime")
                    async with self._account_lock(account_id):
                        recovered = await self._call_required(
                            self._runtime,
                            "recover_account_runtime",
                            account_id,
                            credentials.user_address,
                            credentials.signer_address,
                        )
                    if not recovered:
                        raise ServiceUnavailableError("account runtime recovery was rejected")
                    result = OnboardingResult(
                        account_id=account_id,
                        account_masked=mask_address(credentials.user_address),
                        signer_masked=mask_address(credentials.signer_address),
                        paused=True,
                        message="account runtime recovered paused",
                    )
                    log_onboarding_audit(
                        log,
                        event="recovered",
                        mode=mode,
                        user_id=context.user_id,
                        account_id=account_id,
                        result="recovered",
                        paused=result.paused,
                    )
                    return result
            request = DirectOnboardingRequest(
                telegram_user_id=context.user_id,
                user_address=credentials.user_address,
                declared_signer_address=credentials.signer_address,
                signer_private_key=credentials.signer_private_key,
            )
            try:
                result = await self._onboard(request, replace=False)
            except OnboardingError as exc:
                # Preserve only the stable, secret-safe classification across
                # the Bot/Mini App boundary.  Exchange messages and payloads
                # never become application errors or Telegram copy.
                raise OnboardingApplicationError(exc.code) from None
            safe_result = self._onboarding_result(result, request)
            log_onboarding_audit(
                log,
                event="success",
                mode=mode,
                user_id=context.user_id,
                account_id=safe_result.account_id,
                result="success",
                paused=safe_result.paused,
            )
            return safe_result
        except _SAFE_ERRORS as exc:
            self._audit_rejected(
                mode=mode,
                user_id=user_id,
                account_id=account_id,
                exc=exc,
            )
            raise
        except Exception as exc:
            self._audit_rejected(
                mode=mode,
                user_id=user_id,
                account_id=account_id,
                exc=exc,
            )
            # Keep the transport-facing error stable while retaining only a
            # safe exception category/type in the audit event above.
            raise ServiceUnavailableError("direct onboarding is unavailable") from None

    async def replace_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        mode = "replace"
        account_id: Any = self._audit_account_id(user_id)
        log_onboarding_audit(
            log,
            event="attempt",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="started",
            paused=None,
        )
        try:
            await self._ensure_testnet()
            self._validate_credentials(credentials)
            context, account_row, _ = await self._owned_account(user_id)
            account_id = str(context.account_id)
            if (
                credentials.user_address.strip().casefold()
                != str(_row_value(account_row, "user_address", "")).strip().casefold()
            ):
                raise ValidationError("replacement user address must match the account")
            request = DirectOnboardingRequest(
                telegram_user_id=context.user_id,
                user_address=credentials.user_address,
                declared_signer_address=credentials.signer_address,
                signer_private_key=credentials.signer_private_key,
            )
            lifecycle_factory = self._require_method(self._runtime, "follower_lifecycle")
            self._require_method(self._runtime, "build_replacement_runtime")
            self._require_method(self._onboarding, "preflight")
            self._require_method(self._onboarding, "replace")
            # Signed preflight is network-bound and must not hold the
            # follower/executor lifecycle lock.  The authoritative lifecycle
            # safety check runs after the durable pause immediately before
            # commit.
            try:
                preflight = await self._call_required(self._onboarding, "preflight", request)
            except OnboardingError as exc:
                raise OnboardingApplicationError(exc.code) from None
            candidate = await self._call_required(
                self._runtime,
                "build_replacement_runtime",
                account_id,
                request,
                preflight,
            )
            async with self._account_lock(account_id), lifecycle_factory(account_id) as lifecycle:
                self._require_method(lifecycle, "assert_safe_to_remove")
                self._require_method(lifecycle, "publish_replacement")
                await self._call_required(
                    self._settings,
                    "patch_policy",
                    account_id,
                    allow_new_entries=False,
                )
                # The lifecycle safety check is authoritative and includes
                # flatness plus idle journal state.  It deliberately runs
                # before any credential/cache mutation.
                await self._call_required(lifecycle, "assert_safe_to_remove")
                try:
                    result = await self._call_required(
                        self._onboarding,
                        "replace",
                        request,
                        preflight=preflight,
                    )
                except OnboardingError as exc:
                    raise OnboardingApplicationError(exc.code) from None
                safe_result = self._onboarding_result(result, request)
                # Runtime CAS is after atomic credential commit.  A stale
                # lifecycle cannot overwrite a newer runtime publication.
                await self._call_required(lifecycle, "publish_replacement", candidate)
                # Explicit refresh boundary confirms the newly published
                # identity without returning any credential material.
                await self._snapshot(account_id, refresh=True)
                log_onboarding_audit(
                    log,
                    event="success",
                    mode=mode,
                    user_id=context.user_id,
                    account_id=safe_result.account_id,
                    result="success",
                    paused=safe_result.paused,
                )
                return safe_result
        except _SAFE_ERRORS as exc:
            self._audit_rejected(
                mode=mode,
                user_id=user_id,
                account_id=account_id,
                exc=exc,
            )
            raise
        except Exception as exc:
            self._audit_rejected(
                mode=mode,
                user_id=user_id,
                account_id=account_id,
                exc=exc,
            )
            raise ServiceUnavailableError("credential replacement is unavailable") from None

    async def miniapp_session(self, user_id: int) -> MiniAppSessionDTO:
        """Server-derived Mini App session: access state plus connected account."""
        normalized = self._numeric_id(user_id)
        if normalized is None:
            raise ValidationError("Telegram user ID is invalid")
        try:
            await self._ensure_testnet()
            try:
                context = await self._context(normalized)
            except AuthorizationError:
                return await self._miniapp_denied_session(normalized)

            is_admin = context.is_admin
            if context.account_id:
                candidate = str(context.account_id)
                row = await self._account_row(candidate)
                if row is not None and _is_true(_row_value(row, "enabled", 0)):
                    registered = await self._runtime_registered(candidate)
                    if not registered:
                        user_address = mask_address(str(row["user_address"]))
                        return MiniAppSessionDTO(
                            user_id=normalized,
                            access_state=MiniAppAccessState.APPROVED,
                            environment="testnet",
                            connection_state=MiniAppConnectionState.RECOVERY_REQUIRED,
                            account_connected=False,
                            account_masked=user_address,
                            copy_state=CopyState.PAUSED,
                            is_admin=is_admin,
                            recovery_reason="Runtime activation is pending",
                        )
                    await self._cleanup_builder_attempt(normalized)
                    dash = await self.dashboard(normalized)
                    return MiniAppSessionDTO(
                        user_id=normalized,
                        access_state=MiniAppAccessState.APPROVED,
                        environment="testnet",
                        connection_state=MiniAppConnectionState.CONNECTED,
                        account_connected=True,
                        account_masked=dash.account_masked,
                        copy_state=dash.copy_state,
                        is_admin=is_admin,
                    )

            if self._builder_onboarding is not None:
                attempt = await self._call_optional(
                    self._builder_onboarding, "get_builder_attempt", normalized
                )
                if (
                    attempt is not None
                    and attempt is not _MISSING
                    and getattr(attempt, "state", None) in {"PREPARED", "APPROVAL_SENT", "APPROVED"}
                ):
                    return MiniAppSessionDTO(
                        user_id=normalized,
                        access_state=MiniAppAccessState.APPROVED,
                        environment="testnet",
                        connection_state=MiniAppConnectionState.ONBOARDING,
                        account_connected=False,
                        account_masked=mask_address(getattr(attempt, "user_address", "")),
                        copy_state=CopyState.EMPTY,
                        is_admin=is_admin,
                        onboarding_state=str(getattr(attempt, "state", "")) or None,
                    )

            return MiniAppSessionDTO(
                user_id=normalized,
                access_state=MiniAppAccessState.APPROVED,
                environment="testnet",
                connection_state=MiniAppConnectionState.EMPTY,
                account_connected=False,
                account_masked=None,
                copy_state=CopyState.EMPTY,
                is_admin=is_admin,
            )
        except _SAFE_ERRORS:
            raise
        except Exception as exc:
            log.error("miniapp_session failed class=%s", type(exc).__name__)
            raise ServiceUnavailableError("mini app session is unavailable") from None

    async def miniapp_recover_runtime(self, user_id: int) -> MiniAppSessionDTO:
        """Recover a committed account whose runtime activation failed."""
        mode = "miniapp_recover_runtime"
        account_id: Any = self._audit_account_id(user_id)
        log_onboarding_audit(
            log,
            event="attempt",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="started",
            paused=True,
        )
        try:
            await self._ensure_testnet()
            context = await self._context(user_id)
            if not context.account_id:
                raise ValidationError("no committed account found for recovery")
            candidate = str(context.account_id)
            row = await self._account_row(candidate)
            if row is None or not _is_true(_row_value(row, "enabled", 0)):
                raise ValidationError("no enabled committed account found for recovery")

            if await self._runtime_registered(candidate):
                await self._cleanup_builder_attempt(context.user_id)
                log_onboarding_audit(
                    log,
                    event="recovered",
                    mode=mode,
                    user_id=context.user_id,
                    account_id=candidate,
                    result="already_registered",
                    paused=True,
                )
                return await self.miniapp_session(context.user_id)

            await self._recover_builder_runtime(candidate, context)
            log_onboarding_audit(
                log,
                event="recovered",
                mode=mode,
                user_id=context.user_id,
                account_id=candidate,
                result="recovered",
                paused=True,
            )
            return await self.miniapp_session(context.user_id)
        except _SAFE_ERRORS as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise
        except Exception as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise ServiceUnavailableError("account runtime recovery is unavailable") from None

    async def _miniapp_denied_session(self, user_id: int) -> MiniAppSessionDTO:
        """Map a denied authorization to its durable access-request state."""
        try:
            request = await self.access_request(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("mini app session is unavailable") from None
        state = MiniAppAccessState.UNKNOWN
        if request is not None:
            if request.status is AccessRequestStatus.PENDING:
                state = MiniAppAccessState.PENDING
            elif request.status is AccessRequestStatus.REJECTED:
                state = MiniAppAccessState.REJECTED
            elif request.status is AccessRequestStatus.APPROVED:
                # Approved but not authorized: access was revoked afterwards.
                state = MiniAppAccessState.REJECTED
        return MiniAppSessionDTO(user_id=user_id, access_state=state, environment="testnet")

    async def builder_prepare(self, user_id: int, user_address: str) -> BuilderPrepareDTO:
        mode = "builder_prepare"
        account_id: Any = self._audit_account_id(user_id)
        log_onboarding_audit(
            log,
            event="attempt",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="started",
            paused=None,
        )
        try:
            await self._ensure_testnet()
            if self._builder_onboarding is None:
                raise ServiceUnavailableError("builder onboarding is unavailable")
            if not isinstance(user_address, str) or not user_address.strip():
                raise ValidationError("wallet address is required")
            context = await self._context(user_id)
            # A previous submit may have committed durably but failed runtime
            # publication.  Retry only that publication before the prepare
            # conflict check would classify the user as already connected.
            if context.account_id:
                candidate = str(context.account_id)
                row = await self._account_row(candidate)
                if row is not None and _is_true(_row_value(row, "enabled", 0)):
                    if await self._runtime_registered(candidate):
                        raise OnboardingApplicationError(OnboardingErrorCode.OWNER_CONFLICT)
                    recovered_dto = await self._recover_builder_runtime(candidate, context)
                    log_onboarding_audit(
                        log,
                        event="recovered",
                        mode=mode,
                        user_id=context.user_id,
                        account_id=candidate,
                        result="recovered",
                        paused=True,
                    )
                    return recovered_dto
            try:
                result = await self._builder_onboarding.prepare(
                    BuilderPrepareRequest(
                        telegram_user_id=context.user_id,
                        user_address=user_address.strip(),
                    )
                )
            except OnboardingError as exc:
                raise OnboardingApplicationError(exc.code) from None
            safe_result = BuilderPrepareDTO(
                pending_id=result.pending_id,
                user_masked=result.masked_user_address,
                agent_masked=result.masked_signer_address,
                builder_masked=result.builder_masked,
                builder_name=result.builder_name,
                max_fee_rate=result.max_fee_rate,
                signature_plan=result.signature_plan,
                can_perp_trade=result.can_perp_trade,
                can_withdraw=result.can_withdraw,
                agent_expires_at_ms=result.agent_expires_at_ms,
                approval_expires_at_ms=result.approval_expires_at_ms,
                eip712=result.eip712,
            )
            log_onboarding_audit(
                log,
                event="success",
                mode=mode,
                user_id=context.user_id,
                account_id=account_id,
                result="prepared",
                paused=None,
            )
            return safe_result
        except _SAFE_ERRORS as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise
        except Exception as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise ServiceUnavailableError("builder onboarding is unavailable") from None

    async def builder_submit(
        self, user_id: int, pending_id: str, signature: str
    ) -> BuilderOnboardedDTO:
        mode = "builder_submit"
        account_id: Any = self._audit_account_id(user_id)
        log_onboarding_audit(
            log,
            event="attempt",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="started",
            paused=None,
        )
        try:
            await self._ensure_testnet()
            if self._builder_onboarding is None:
                raise ServiceUnavailableError("builder onboarding is unavailable")
            if (
                not isinstance(pending_id, str)
                or not pending_id.strip()
                or not isinstance(signature, str)
                or not signature.strip()
            ):
                raise ValidationError("builder approval is invalid")
            context = await self._context(user_id)
            safe_result = await self._submit_builder_attempt(
                context,
                pending_id.strip(),
                signature.strip(),
            )
            log_onboarding_audit(
                log,
                event="success",
                mode=mode,
                user_id=context.user_id,
                account_id=safe_result.account_id,
                result="success",
                paused=safe_result.paused,
            )
            return safe_result
        except _SAFE_ERRORS as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise
        except Exception as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise ServiceUnavailableError("builder onboarding is unavailable") from None

    async def builder_resume_attempt(self, user_id: int) -> BuilderOnboardedDTO:
        """Continue an exchange-submitted attempt without asking the wallet to sign again."""
        mode = "builder_resume_attempt"
        account_id: Any = self._audit_account_id(user_id)
        log_onboarding_audit(
            log,
            event="attempt",
            mode=mode,
            user_id=user_id,
            account_id=account_id,
            result="started",
            paused=None,
        )
        try:
            await self._ensure_testnet()
            if self._builder_onboarding is None:
                raise ServiceUnavailableError("builder onboarding is unavailable")
            context = await self._context(user_id)
            attempt = await self._call_required(
                self._builder_onboarding,
                "get_builder_attempt",
                context.user_id,
            )
            if attempt is None:
                raise OnboardingApplicationError(OnboardingErrorCode.APPROVAL_UNKNOWN)
            state = str(getattr(attempt, "state", ""))
            if state not in {"APPROVAL_SENT", "APPROVED"}:
                raise OnboardingApplicationError(OnboardingErrorCode.APPROVAL_SIGNATURE_REQUIRED)
            safe_result = await self._submit_builder_attempt(
                context,
                str(getattr(attempt, "pending_id", "")),
                "",
            )
            log_onboarding_audit(
                log,
                event="success",
                mode=mode,
                user_id=context.user_id,
                account_id=safe_result.account_id,
                result="success",
                paused=True,
            )
            return safe_result
        except _SAFE_ERRORS as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise
        except Exception as exc:
            self._audit_rejected(mode=mode, user_id=user_id, account_id=account_id, exc=exc)
            raise ServiceUnavailableError("builder onboarding is unavailable") from None

    async def _submit_builder_attempt(
        self,
        context: UserContext,
        pending_id: str,
        signature: str,
    ) -> BuilderOnboardedDTO:
        try:
            result = await self._builder_onboarding.submit(
                BuilderSubmitRequest(
                    telegram_user_id=context.user_id,
                    pending_id=pending_id,
                    signature=signature,
                )
            )
        except OnboardingError as exc:
            raise OnboardingApplicationError(exc.code) from None
        if str(result.account_id) != stable_account_id(context.user_id) or not result.paused:
            raise ValidationError("onboarding result is invalid")
        return BuilderOnboardedDTO(
            account_id=str(result.account_id),
            account_masked=str(result.account_masked),
            signer_masked=str(result.signer_masked),
            builder_name=str(result.builder_name),
            max_fee_rate=str(result.max_fee_rate),
            position_mode=str(result.position_mode),
            available_usdt=result.available_usdt,
            agent_expires_at_ms=int(result.agent_expires_at_ms),
            paused=True,
            message="account connected paused",
        )

    async def _recover_builder_runtime(
        self, account_id: str, context: UserContext
    ) -> BuilderPrepareDTO:
        """Re-publish a committed-but-unpublished Builder account runtime."""
        self._require_method(self._runtime, "recover_account_runtime")
        self._require_method(self._builder_onboarding, "identity_for_recovery")
        identity = await self._call_required(
            self._builder_onboarding,
            "identity_for_recovery",
            account_id,
            context.user_id,
        )
        if identity is None:
            raise OnboardingApplicationError(OnboardingErrorCode.OWNER_CONFLICT)
        user_address, signer_address = identity
        async with self._account_lock(account_id):
            recovered = await self._call_required(
                self._runtime,
                "recover_account_runtime",
                account_id,
                user_address,
                signer_address,
            )
        if not recovered:
            raise ServiceUnavailableError("account runtime recovery was rejected")
        await self._cleanup_builder_attempt(context.user_id)
        return BuilderPrepareDTO(
            pending_id=None,
            already_connected=True,
            user_masked=mask_address(str(user_address)),
        )

    async def _cleanup_builder_attempt(self, user_id: int) -> None:
        if self._builder_onboarding is None:
            return
        try:
            await self._call_optional(
                self._builder_onboarding,
                "delete_builder_attempt",
                user_id,
            )
        except Exception:
            log.warning("builder committed attempt cleanup failed")

    @staticmethod
    def _decimal_input(value: Any, field: str) -> Decimal:
        if isinstance(value, (float, bool)) or value is None:
            raise ValidationError("risk values are invalid")
        try:
            result = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("risk values are invalid") from None
        if not result.is_finite():
            raise ValidationError("risk values are invalid")
        del field
        return result

    @classmethod
    def _risk_dto(cls, policy: Any) -> RiskDTO:
        if policy is None or policy is _MISSING:
            raise ServiceUnavailableError("risk policy is unavailable")
        try:
            risk = cls._decimal_input(_row_value(policy, "risk_percent"), "risk_percent")
            return RiskDTO(
                risk_percent=risk,
                min=RISK_MIN,
                max=RISK_MAX,
                step=RISK_STEP,
                presets=RISK_PRESETS,
                risk_base=str(_row_value(policy, "risk_base", RISK_BASE)),
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ValidationError("risk values are invalid") from None

    @_control_audit_boundary("update_risk")
    async def update_risk(self, user_id: int, values: RiskUpdateInput) -> RiskDTO:
        try:
            context, _, _ = await self._owned_account(user_id)
            if not isinstance(values, RiskUpdateInput):
                raise ValidationError("risk values are invalid")
            # Leverage and entry-deviation remain legacy storage/runtime
            # fields for compatibility, but they are no longer follower
            # controls.  Reject them at the shared Bot/Mini App boundary,
            # including forged callers that bypass Telegram rendering.
            if values.max_leverage is not None or values.max_entry_deviation_pct is not None:
                raise ValidationError("only risk_percent can be changed")
            if values.risk_percent is None:
                raise ValidationError("risk values are invalid")
            value = self._decimal_input(values.risk_percent, "risk_percent")
            if value < RISK_MIN or value > RISK_MAX:
                raise ValidationError("risk values are invalid")
            if (value - RISK_MIN) % RISK_STEP != 0:
                raise ValidationError("risk values are invalid")
            changes: dict[str, Any] = {"risk_percent": value}
            account_id = str(context.account_id)
            async with self._account_lock(account_id):
                previous = await self._previous_risk_percent(account_id)
                policy = await self._call_required(
                    self._settings, "patch_policy", account_id, **changes
                )
            facts: dict[str, str] = {"risk_percent": format(value, "f")}
            if previous is not None:
                facts["previous_percent"] = format(previous, "f")
            await self._record_system_event(account_id, kind="RISK_CHANGED", facts=facts)
            return self._risk_dto(policy)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("risk update is unavailable") from None

    async def risk_preview(self, user_id: int) -> RiskPreviewDTO:
        """Money examples for every allowed risk step over the cached snapshot.

        Cached-only on purpose: the ruler screen loads this once and switches
        ready rows locally. No exchange read, no side effects; a missing
        balance yields empty steps instead of a zero example.
        """
        try:
            context, _, _ = await self._owned_account(user_id, allow_disabled=True)
            account_id = str(context.account_id)
            snapshot, captured_at = await self._snapshot(account_id, refresh=False)
            available = self._available_usdt(snapshot)
            if snapshot is None or captured_at is None:
                freshness = SnapshotFreshness.UNAVAILABLE
            elif (self._now() - captured_at).total_seconds() > self._snapshot_ttl_seconds():
                freshness = SnapshotFreshness.STALE
            else:
                freshness = SnapshotFreshness.FRESH
            if available is None:
                return RiskPreviewDTO(None, (), captured_at, freshness)
            steps = risk_budget_steps(
                available,
                min_percent=RISK_MIN,
                max_percent=RISK_MAX,
                step=RISK_STEP,
            )
            return RiskPreviewDTO(available, steps, captured_at, freshness)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("risk preview is unavailable") from None

    @_control_audit_boundary("pause")
    async def pause(self, user_id: int) -> DashboardDTO:
        try:
            await self._ensure_testnet()
            context, _, _ = await self._owned_account(user_id)
            account_id = str(context.account_id)
            # A pause without this method would only change a flag while an
            # already-admitted generation could still open an entry.
            self._require_method(self._runtime, "pause_account")
            async with self._account_lock(account_id):
                await self._call_required(
                    self._settings,
                    "patch_policy",
                    account_id,
                    allow_new_entries=False,
                )
                await self._call_required(self._runtime, "pause_account", account_id)
            # S1.5: the pause itself is a feed fact; a record failure is
            # logged and never turns the committed pause into an error.
            await self._record_system_event(
                account_id, kind="COPY_PAUSED", facts={"allow_new_entries": False}
            )
            return await self.dashboard(context.user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("pause operation is unavailable") from None

    async def _master_requires_confirmation(self) -> bool:
        getter = getattr(self._runtime, "master_has_open_positions", _MISSING)
        if getter is _MISSING:
            raise ServiceUnavailableError("required application operation is unavailable")
        if callable(getter):
            result = await _maybe_await(getter())
        else:
            result = getter
        return bool(result)

    async def _record_system_event(
        self, account_id: str, *, kind: str, facts: dict[str, str]
    ) -> None:
        """Best-effort feed record for a committed application action.

        The state change already happened; a storage failure is logged and
        never turns it into an operation error. Push delivery stays a
        separate concern (S3 activation); the feed shows the fact now.
        """
        try:
            events = self._user_events
            if events is None:
                return
            scope = HistoryScope(account_id, await self._history_environment())
            now_ms = int(self._now().timestamp() * 1000)
            await events.record(
                scope,
                kind=kind,
                occurred_at=now_ms,
                recorded_at=now_ms,
                facts=facts,
                dedup_key=(f"evt:sys:{kind}:{account_id}:{now_ms}:{secrets.token_hex(4)}"),
                trade_ref=None,
                notify_eligible=True,
            )
        except Exception:  # noqa: BLE001 - feed record must not fail the op
            log.exception("system event record failed (kind=%s)", kind)

    async def _record_entry_skipped(
        self,
        account_id: str,
        *,
        symbol: str,
        reason: str,
        attempt_id: str,
        pos_side: str | None = None,
        occurred_at_ms: int | None = None,
    ) -> None:
        """Best-effort ENTRY_SKIPPED fact for one risk/planner skip decision.

        Production hook: the trading coordinator skip loop
        (``_recompute_follower_locked`` over ``_targets_for`` skips), passing
        the ``SkipRecord`` fields with the stable attempt identity
        (``_skip_episode_token``: ``trade-{id}`` or the hashed leg-state
        digest). The dedup key carries that identity, so one decision is one
        event no matter how many sweeps re-observe it. A storage failure is
        logged and never fails the trading loop.
        """
        if not account_id or not isinstance(account_id, str):
            raise ValueError("entry skip account required")
        if not symbol or not isinstance(symbol, str):
            raise ValueError("entry skip symbol required")
        if not reason or not isinstance(reason, str):
            raise ValueError("entry skip reason required")
        if not attempt_id or not isinstance(attempt_id, str):
            raise ValueError("entry skip attempt identity required")
        if pos_side is not None and (not isinstance(pos_side, str) or not pos_side):
            raise ValueError("entry skip position side must be text or absent")
        if occurred_at_ms is not None and (type(occurred_at_ms) is not int or occurred_at_ms < 0):
            raise ValueError("entry skip time must be a nonnegative integer")
        try:
            events = self._user_events
            if events is None:
                return
            scope = HistoryScope(account_id, await self._history_environment())
            now_ms = int(self._now().timestamp() * 1000)
            facts: dict[str, str] = {
                "symbol": symbol,
                "reason": reason,
                "attempt_id": attempt_id,
            }
            if pos_side is not None:
                facts["pos_side"] = pos_side
            leg = pos_side if pos_side else "unknown"
            await events.record(
                scope,
                kind="ENTRY_SKIPPED",
                occurred_at=occurred_at_ms if occurred_at_ms is not None else now_ms,
                recorded_at=now_ms,
                facts=facts,
                dedup_key=(
                    f"evt:sys:ENTRY_SKIPPED:{account_id}:{symbol}:{leg}:{reason}:{attempt_id}"
                ),
                trade_ref=None,
                notify_eligible=True,
            )
        except Exception:  # noqa: BLE001 - feed record must not fail the loop
            log.exception("entry skipped record failed (account_id=%s)", account_id)

    async def _record_connectivity(self, account_id: str, *, reachable: bool) -> str | None:
        """Idempotent account connectivity transition; None when unchanged.

        Production hook: the exchange-read seam in ``_snapshot(refresh=True)``
        (the concrete account-adapter read), called with ``reachable=True``
        on a response and ``reachable=False`` on a transport-classified
        failure — never per sweep unconditionally. The last-recorded state
        guards emission: an identical sweep records nothing, and a restart
        re-observes from unknown (first success stays silent, first failure
        reports the outage it just proved). Storage failures are logged;
        the last state only advances on a durable write so the next sweep
        retries the report.
        """
        if not account_id or not isinstance(account_id, str):
            raise ValueError("connectivity account required")
        if not isinstance(reachable, bool):
            raise ValueError("connectivity reachability must be a boolean")
        last = self._connectivity.get(account_id)
        if reachable and last is False:
            kind = "CONNECTIVITY_RESTORED"
        elif not reachable and last is not False:
            kind = "CONNECTIVITY_LOST"
        else:
            self._connectivity.setdefault(account_id, reachable)
            return None
        try:
            events = self._user_events
            if events is None:
                return None
            scope = HistoryScope(account_id, await self._history_environment())
            now_ms = int(self._now().timestamp() * 1000)
            await events.record(
                scope,
                kind=kind,
                occurred_at=now_ms,
                recorded_at=now_ms,
                facts={"reachable": reachable},
                dedup_key=f"evt:sys:{kind}:{account_id}:{now_ms}",
                trade_ref=None,
                notify_eligible=True,
            )
        except Exception:  # noqa: BLE001 - feed record must not fail the read
            log.exception("connectivity record failed (account_id=%s)", account_id)
            return None
        self._connectivity[account_id] = reachable
        return kind

    async def _previous_risk_percent(self, account_id: str) -> Decimal | None:
        """Last saved risk percent for feed facts; None when unreadable."""
        try:
            policy = await self._call_required(self._settings, "get_policy", account_id)
            return self._decimal_input(_row_value(policy, "risk_percent"), "risk_percent")
        except Exception:  # noqa: BLE001 - advisory fact only
            return None

    async def _resume_context(self, account_id: str) -> ResumeContextDTO:
        """Cached-only resume facts for the confirmation copy.

        Advisory by design: every read degrades to None (unknown) instead of
        failing begin_confirmation. The enforcing fresh checks still run
        inside the resume workflow itself.
        """
        try:
            master_in_position: bool | None = await self._master_requires_confirmation()
        except Exception:
            master_in_position = None
        snapshot, captured_at = await self._snapshot(account_id, refresh=False)
        if snapshot is None or captured_at is None:
            snapshot_freshness = SnapshotFreshness.UNAVAILABLE
        elif (self._now() - captured_at).total_seconds() > self._snapshot_ttl_seconds():
            snapshot_freshness = SnapshotFreshness.STALE
        else:
            snapshot_freshness = SnapshotFreshness.FRESH
        permission_ok = await self._cached_permission_ok(account_id)
        return ResumeContextDTO(master_in_position, snapshot_freshness, permission_ok)

    async def _cached_permission_ok(self, account_id: str) -> bool | None:
        """Verified-only cached proof; None when no proof exists either way."""
        row = await self._validation_row(account_id)
        if row is None:
            return None
        permission, _ = await self._permission(account_id)
        triple_ok = (
            permission.can_read is True
            and permission.can_perp_trade is True
            and permission.can_withdraw is False
            and permission.whitelist_ok is True
        )
        try:
            expiry_ms = int(_row_value(row, "expired_ms", 0))
        except (TypeError, ValueError):
            return None
        now_ms = int(self._now().timestamp() * 1000)
        warning_ok = permission.warning is None
        return bool(triple_ok and expiry_ms > now_ms and warning_ok)

    async def _resume_account(
        self,
        user_id: int,
        account_id: str,
        *,
        expected_account_version: str | None = None,
    ) -> DashboardDTO:
        await self._ensure_testnet()
        await self._ensure_ready()
        context, row, _ = await self._owned_account(user_id, account_id)
        system_state = await self._system_state()
        if system_state is not SystemState.READY:
            raise StaleOperationError(
                f"system state is not ready for resume (state={system_state.value})"
            )
        if not _is_true(_row_value(row, "enabled", 0)):
            raise AuthorizationError("account is disconnected")
        if _is_true(await self._flag("emergency_active")):
            raise GlobalEmergencyActiveError("global emergency is active")
        if _is_false(await self._flag("allow_new_entries", "true")):
            raise GlobalPauseActiveError("global pause is active")
        if not await self._runtime_registered(account_id):
            raise ServiceUnavailableError("account runtime is unavailable")
        validated_account_version = await self._account_version(account_id)
        if (
            expected_account_version is not None
            and validated_account_version != expected_account_version
        ):
            raise ConfirmationExpiredError("confirmation account has changed")
        permission, checked_at = await self._permission(account_id)
        validation_row = await self._validation_row(account_id)
        try:
            permission_expiry = int(_row_value(validation_row, "expired_ms", 0))
        except (TypeError, ValueError):
            permission_expiry = 0
        permission_stale = (
            checked_at is None
            or (self._now() - checked_at).total_seconds() > self._permission_ttl
            or permission_expiry <= int(self._now().timestamp() * 1000)
        )
        if permission_stale:
            # A stale row is never trusted.  The only valid refresh path is a
            # signed adapter read followed by an explicit repository upsert.
            try:
                await self._refresh_permission_snapshot(account_id)
            except ServiceUnavailableError:
                override_method = self._require_method(self._runtime, "permission_override_allowed")
                override_allowed = bool(await _maybe_await(override_method(account_id)))
                age_seconds = (
                    None
                    if checked_at is None
                    else max((self._now() - checked_at).total_seconds(), 0)
                )
                cached_override_valid = (
                    override_allowed
                    and age_seconds is not None
                    and age_seconds <= TESTNET_PERMISSION_FALLBACK_TTL_SECONDS
                    and permission_expiry > int(self._now().timestamp() * 1000)
                    and permission.can_read is True
                    and permission.can_perp_trade is True
                    and permission.can_withdraw is False
                    and permission.whitelist_ok is True
                    and permission.warning == "permission_check_override"
                )
                if not cached_override_valid:
                    raise
                log.warning(
                    "signed Agent permission refresh unavailable; using bounded Testnet "
                    "override cache account_id=%s age_s=%d",
                    account_id,
                    int(age_seconds),
                )
            else:
                permission, checked_at = await self._permission(account_id)
                validation_row = await self._validation_row(account_id)
                try:
                    permission_expiry = int(_row_value(validation_row, "expired_ms", 0))
                except (TypeError, ValueError):
                    permission_expiry = 0
                if (
                    checked_at is None
                    or (self._now() - checked_at).total_seconds() > self._permission_ttl
                    or permission_expiry <= int(self._now().timestamp() * 1000)
                ):
                    raise StaleOperationError("permission validation is stale")
        override_allowed = False
        if permission.warning == "permission_check_override":
            override_method = self._require_method(self._runtime, "permission_override_allowed")
            override_allowed = bool(await _maybe_await(override_method(account_id)))
        if (
            permission.can_read is not True
            or permission.can_perp_trade is not True
            or permission.can_withdraw is not False
            or (
                permission.warning is not None
                and not (permission.warning == "permission_check_override" and override_allowed)
            )
            or permission.whitelist_ok is not True
        ):
            raise StaleOperationError("permission validation is not sufficient")
        snapshot, _ = await self._snapshot(account_id, refresh=True)
        # The adapter owns the distinction between managed bot state and
        # manual/external positions/orders.  A flatness-only count would reject
        # safe managed state and could accidentally adopt external exposure.
        await self._call_required(self._runtime, "validate_resume_safety", account_id, snapshot)
        self._require_method(self._runtime, "resume_account")
        # Serialize the final enable/reconcile publication with global
        # pause/emergency transitions.  Permission/snapshot reads above stay
        # outside this short mutation section, then global gates are checked
        # again under the same lock used by admin controls.
        async with self._global_mutation_lock, self._account_lock(account_id):
            if _is_true(await self._flag("emergency_active")):
                raise GlobalEmergencyActiveError("global emergency is active")
            if _is_false(await self._flag("allow_new_entries", "true")):
                raise GlobalPauseActiveError("global pause is active")
            # Replacement/disconnect use the same account lock.  A version
            # change proves that the permission/snapshot checks above belong
            # to an older credential runtime and must not be published.
            _, locked_row, _ = await self._owned_account(user_id, account_id)
            if not _is_true(_row_value(locked_row, "enabled", 0)):
                raise StaleOperationError("account changed during resume")
            if not await self._runtime_registered(account_id):
                raise StaleOperationError("account runtime changed during resume")
            if await self._account_version(account_id) != validated_account_version:
                raise StaleOperationError("account credentials changed during resume")
            if (
                expected_account_version is not None
                and validated_account_version != expected_account_version
            ):
                raise ConfirmationExpiredError("confirmation account has changed")
            await self._call_required(
                self._settings,
                "patch_policy",
                account_id,
                enabled=True,
                allow_new_entries=True,
            )
            try:
                await self._call_required(self._runtime, "resume_account", account_id)
                await self._call_required(self._runtime, "reconcile_account", account_id)
            except Exception:
                # Reopen admission is a two-phase operation.  If runtime
                # publication/reconcile fails, durable state must remain
                # paused and therefore cannot create entries on a retry.
                with suppress(Exception):
                    await self._call_required(
                        self._settings,
                        "patch_policy",
                        account_id,
                        allow_new_entries=False,
                    )
                raise
        return await self.dashboard(context.user_id)

    @_control_audit_boundary("resume")
    async def resume(self, user_id: int, confirmation: Confirmation | None = None) -> DashboardDTO:
        try:
            context, _, _ = await self._owned_account(user_id)
            if confirmation is None:
                raise ConfirmationExpiredError("resume confirmation is required")
            record = await self._consume_confirmation(
                context.user_id, confirmation, expected=ConfirmationAction.RESUME
            )
            expected_version = record.account_version
            account_id = str(context.account_id)
            dashboard = await self._resume_account(
                context.user_id,
                account_id,
                expected_account_version=expected_version,
            )
            await self._record_system_event(
                account_id, kind="COPY_RESUMED", facts={"allow_new_entries": True}
            )
            return dashboard
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("resume operation is unavailable") from None

    async def begin_confirmation(
        self,
        user_id: int,
        action: ConfirmationAction,
        account_id: str | None = None,
    ) -> Confirmation:
        try:
            try:
                normalized_action = ConfirmationAction(action)
            except ValueError:
                raise ValidationError("confirmation action is invalid") from None
            if normalized_action is ConfirmationAction.EMERGENCY:
                # Per-follower emergency remains an internal control-core
                # capability, not a Bot/Mini App user operation.  Reject
                # before ownership/version lookups so forged calls cannot
                # reach the destructive workflow.
                raise AuthorizationError("follower emergency is admin-only")
            if normalized_action in {
                ConfirmationAction.GLOBAL_PAUSE,
                ConfirmationAction.GLOBAL_RESUME,
                ConfirmationAction.GLOBAL_EMERGENCY,
            }:
                context = await self._context(user_id, admin=True)
                if account_id is not None:
                    raise ValidationError("global confirmation cannot bind an account")
                bound_account = None
            else:
                context, _, _ = await self._owned_account(user_id, account_id)
                bound_account = str(context.account_id)
            account_version = (
                None if bound_account is None else await self._account_version(bound_account)
            )
            # S1.4: RESUME carries proven facts so the confirmation copy
            # explains the real outcome instead of asserting a master position.
            resume_context = None
            if normalized_action is ConfirmationAction.RESUME and bound_account is not None:
                resume_context = await self._resume_context(bound_account)
            expires = self._now() + timedelta(seconds=self._confirmation_ttl)
            token = _new_confirmation_token()
            record = _ConfirmationRecord(
                user_id=context.user_id,
                action=normalized_action,
                account_id=bound_account,
                account_version=account_version,
                expires_at=expires,
            )
            async with self._confirmation_lock:
                self._confirmations[token] = record
            return Confirmation(
                token=token,
                action=normalized_action,
                account_id=bound_account,
                expires_at=expires,
                resume_context=resume_context,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("confirmation is unavailable") from None

    async def _consume_confirmation(
        self,
        user_id: int,
        confirmation: Confirmation,
        *,
        expected: ConfirmationAction,
    ) -> _ConfirmationRecord:
        context_id = self._numeric_id(user_id)
        if context_id is None or not isinstance(confirmation, Confirmation):
            raise ConfirmationExpiredError("confirmation is invalid or expired")
        if (
            not isinstance(confirmation.token, str)
            or not confirmation.token
            or not confirmation.token.isascii()
            or len(confirmation.token.encode("utf-8")) > CONFIRMATION_TOKEN_MAX_BYTES
        ):
            raise ConfirmationExpiredError("confirmation is invalid or expired")
        try:
            action = ConfirmationAction(confirmation.action)
        except ValueError:
            raise ConfirmationExpiredError("confirmation is invalid or expired") from None
        if action is not expected:
            raise AuthorizationError("confirmation action is not authorized")
        async with self._confirmation_lock:
            record = self._confirmations.get(confirmation.token)
            if record is None:
                raise ConfirmationExpiredError("confirmation is invalid or expired")
            if record.user_id != context_id:
                raise AuthorizationError("confirmation owner is not authorized")
            if record.action is not action or record.account_id != confirmation.account_id:
                raise AuthorizationError("confirmation scope is not authorized")
            if record.expires_at <= self._now():
                self._confirmations.pop(confirmation.token, None)
                raise ConfirmationExpiredError("confirmation is invalid or expired")
            self._confirmations.pop(confirmation.token, None)
        if record.account_id is not None:
            try:
                current_version = await self._account_version(record.account_id)
            except ServiceUnavailableError:
                raise ConfirmationExpiredError(
                    "confirmation account version is unavailable"
                ) from None
            if current_version != record.account_version:
                raise ConfirmationExpiredError("confirmation account has changed")
        return record

    async def _perform_account_emergency(
        self,
        user_id: int,
        account_id: str,
        *,
        expected_account_version: str,
    ) -> DashboardDTO:
        await self._ensure_testnet()
        context, _, _ = await self._owned_account(user_id, account_id)
        controller = self._emergency
        self._require_method(controller, "emergency_account")
        self._require_method(self._runtime, "pause_account")
        operation_id = secrets.token_hex(16)
        async with self._account_lock(account_id):
            if await self._account_version(account_id) != expected_account_version:
                raise ConfirmationExpiredError("confirmation account has changed")
            await self._call_required(
                self._settings,
                "patch_policy",
                account_id,
                allow_new_entries=False,
            )
            # Drain any generation that was admitted before the durable pause.
            # The adapter reopens admission with the per-account policy still
            # false so reductions/protection remain available.
            await self._call_required(self._runtime, "pause_account", account_id)
            await self._set_flag(f"emergency_operation:{account_id}", operation_id)
            report = await self._call_required(
                controller,
                "emergency_account",
                account_id,
                operation_id=operation_id,
            )
            _require_successful_control(report)
        return await self.dashboard(context.user_id)

    async def _perform_disconnect(
        self,
        user_id: int,
        account_id: str,
        *,
        expected_account_version: str,
    ) -> DashboardDTO:
        await self._ensure_testnet()
        context, _, _ = await self._owned_account(user_id, account_id)
        lifecycle_factory = self._require_method(self._runtime, "follower_lifecycle")
        commit_disconnect = self._require_method(self._runtime, "commit_disconnect")
        # Check the lifecycle surface before the durable pause.  A disabled
        # runtime hook must never turn a user action into a partial hard-disable.
        async with self._account_lock(account_id), lifecycle_factory(account_id) as lifecycle:
            if await self._account_version(account_id) != expected_account_version:
                raise ConfirmationExpiredError("confirmation account has changed")
            self._require_method(lifecycle, "assert_safe_to_remove")
            self._require_method(lifecycle, "unpublish_after_commit")
            # Pause is durable first, but accounts.enabled and credentials
            # remain untouched until the authoritative safety check passes.
            await self._call_required(
                self._settings,
                "patch_policy",
                account_id,
                allow_new_entries=False,
            )
            await self._call_required(lifecycle, "assert_safe_to_remove")
            # This is the commit half of the two-phase lifecycle.  The
            # adapter must atomically disable policy/account, delete the
            # credential blob and update its key cache.
            result = await _maybe_await(commit_disconnect(account_id))
            if result is False:
                raise ServiceUnavailableError("disconnect commit was not completed")
            # Runtime unpublish is deliberately after the durable commit;
            # its CAS rejects a stale lifecycle instead of removing a newer
            # replacement.
            await self._call_required(lifecycle, "unpublish_after_commit")
        return await self.dashboard(context.user_id)

    @_control_audit_boundary("confirm_action")
    async def confirm_action(self, user_id: int, confirmation: Confirmation) -> DashboardDTO:
        try:
            if not isinstance(confirmation, Confirmation):
                raise ConfirmationExpiredError("confirmation is invalid or expired")
            action = ConfirmationAction(confirmation.action)
            if action is ConfirmationAction.EMERGENCY:
                # Keep the internal per-account emergency core available to
                # operators, while making the shared user boundary fail closed
                # even for a forged Confirmation object/token.
                raise AuthorizationError("follower emergency is admin-only")
            if action is ConfirmationAction.RESUME:
                context, _, _ = await self._owned_account(user_id, confirmation.account_id)
                record = await self._consume_confirmation(user_id, confirmation, expected=action)
                return await self._resume_account(
                    context.user_id,
                    str(context.account_id),
                    expected_account_version=record.account_version,
                )
            if action is ConfirmationAction.DISCONNECT:
                context, _, _ = await self._owned_account(user_id, confirmation.account_id)
                record = await self._consume_confirmation(user_id, confirmation, expected=action)
                if record.account_version is None:
                    raise ConfirmationExpiredError("confirmation account version is unavailable")
                return await self._perform_disconnect(
                    context.user_id,
                    str(context.account_id),
                    expected_account_version=record.account_version,
                )
            context = await self._context(user_id, admin=True)
            await self._consume_confirmation(user_id, confirmation, expected=action)
            if action is ConfirmationAction.GLOBAL_PAUSE:
                await self._perform_global_pause()
            elif action is ConfirmationAction.GLOBAL_RESUME:
                await self._perform_global_resume()
            elif action is ConfirmationAction.GLOBAL_EMERGENCY:
                await self._perform_global_emergency()
            else:
                raise ValidationError("confirmation action is invalid")
            return await self.dashboard(context.user_id)
        except _SAFE_ERRORS:
            raise
        except (ValueError, TypeError):
            raise ConfirmationExpiredError("confirmation is invalid or expired") from None
        except Exception:
            raise ServiceUnavailableError("confirmed operation is unavailable") from None

    async def _perform_global_pause(self) -> None:
        await self._ensure_testnet()
        async with self._global_mutation_lock:
            self._require_method(self._runtime, "pause_entries")
            self._require_method(self._runtime, "reopen_entries_while_paused")
            # Keep this write explicit at the application boundary even when a
            # concrete Coordinator also persists the same flag internally.
            await self._set_flag("allow_new_entries", "false")
            await self._call_required(self._runtime, "pause_entries")
            # This seam never writes ``true``.  Using resume_entries() here
            # would create a race where a generation observes an enabled gate.
            await self._call_required(self._runtime, "reopen_entries_while_paused")

    async def _perform_global_resume(self) -> None:
        await self._ensure_testnet()
        async with self._global_mutation_lock:
            self._require_method(self._runtime, "resume_entries")
            await self._set_flag("allow_new_entries", "true")
            try:
                await self._call_required(self._runtime, "resume_entries")
            except Exception:
                # Never leave a failed reopen represented as an active global
                # admission flag.  False is the safe durable rollback.
                with suppress(Exception):
                    await self._set_flag("allow_new_entries", "false")
                raise
            await self._set_flag("emergency_active", "false")

    async def _perform_global_emergency(self) -> None:
        await self._ensure_testnet()
        operation_id = secrets.token_hex(16)
        controller = self._emergency
        self._require_method(controller, "emergency_global")
        async with self._global_mutation_lock:
            self._require_method(self._runtime, "pause_entries")
            self._require_method(self._runtime, "reopen_entries_while_paused")
            await self._set_flag("emergency_active", "true")
            await self._set_flag("allow_new_entries", "false")
            await self._set_flag("emergency_operation_id", operation_id)
            await self._call_required(self._runtime, "pause_entries")
            await self._call_required(self._runtime, "reopen_entries_while_paused")
            rows = await self._accounts_for_admin()
            account_ids = tuple(str(_row_value(row, "id")) for row in rows)
            report = await self._call_required(
                controller,
                "emergency_global",
                account_ids,
                operation_id=operation_id,
            )
            _require_successful_control(report)

    async def admin_dashboard(self, user_id: int) -> AdminDashboardDTO:
        try:
            await self._context(user_id, admin=True)
            rows = await self._accounts_for_admin()
            counts = await self._call_required(self._outbox, "counts")
            pending_access = await self._call_required(
                self._access_requests,
                "pending_count",
            )
            masters = await self._call_optional(self._accounts, "get_all", role="master")
            master_row = None if masters is _MISSING else next(iter(masters), None)
            return AdminDashboardDTO(
                system_state=await self._system_state(),
                global_pause=_is_false(await self._flag("allow_new_entries", "true")),
                global_emergency=_is_true(await self._flag("emergency_active")),
                accounts_count=len(rows),
                outbox_pending=int(_row_value(counts, "PENDING", 0) or 0),
                outbox_failed=int(_row_value(counts, "FAILED", 0) or 0),
                pending_access_requests=int(pending_access),
                master_account_masked=(
                    None
                    if master_row is None
                    else mask_address(str(_row_value(master_row, "user_address", "")))
                ),
                message=None,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin dashboard is unavailable") from None

    async def _accounts_for_admin(self) -> list[Any]:
        result = await self._call_optional(self._accounts, "get_all", role="follower")
        if result is not _MISSING:
            return [
                row
                for row in result
                if str(_row_value(row, "role", "follower")) == "follower"
                and _is_true(_row_value(row, "enabled", 0))
            ]
        db = getattr(self._runtime, "db", None)
        fetchall = getattr(db, "fetchall", None)
        if fetchall is None:
            raise ServiceUnavailableError("account list is unavailable")
        return list(
            await fetchall(
                "SELECT id, role, enabled FROM accounts WHERE role='follower' AND enabled=1"
            )
        )

    @staticmethod
    def _page_number(page: int) -> int:
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise ValidationError("admin page is invalid")
        return page

    async def admin_users(self, user_id: int, page: int = 0) -> AdminUserPage:
        try:
            await self._context(user_id, admin=True)
            page = self._page_number(page)
            rows = await self._call_required(
                self._users,
                "get_all",
                limit=ADMIN_PAGE_SIZE + 1,
                offset=page * ADMIN_PAGE_SIZE,
            )
            has_next = len(rows) > ADMIN_PAGE_SIZE
            items = tuple(
                AdminUserItem(
                    user_id=int(_row_value(row, "telegram_user_id")),
                    account_connected=_row_value(row, "account_id") is not None,
                    notifications_enabled=bool(_row_value(row, "notifications_enabled", 1)),
                    access_enabled=_is_true(_row_value(row, "access_enabled", 1)),
                    is_admin=int(_row_value(row, "telegram_user_id")) in self._admin_ids,
                )
                for row in rows[:ADMIN_PAGE_SIZE]
            )
            return AdminUserPage(
                items=items,
                page=page,
                has_previous=page > 0,
                has_next=has_next,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin user list is unavailable") from None

    @_control_audit_boundary("admin_allow_user")
    async def admin_allow_user(self, user_id: int, target_user_id: int) -> AdminUserPage:
        try:
            await self._context(user_id, admin=True)
            target = self._numeric_id(target_user_id)
            if target is None:
                raise ValidationError("Telegram user ID is invalid")
            # Private Telegram chat IDs equal user IDs.  Upsert is idempotent
            # and preserves any existing role, account and notification state.
            await self._call_required(
                self._users,
                "upsert",
                target,
                target,
                access_enabled=True,
            )
            return await self.admin_users(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("Telegram user access could not be granted") from None

    @_control_audit_boundary("admin_set_user_access")
    async def admin_set_user_access(
        self,
        user_id: int,
        target_user_id: int,
        enabled: bool,
    ) -> AdminUserPage:
        try:
            await self._context(user_id, admin=True)
            target = self._numeric_id(target_user_id)
            if target is None or not isinstance(enabled, bool):
                raise ValidationError("Telegram user access setting is invalid")
            if not enabled and target in self._admin_ids:
                raise ValidationError("configured administrator access cannot be revoked")
            if await self._user_row(target) is None:
                raise ValidationError("Telegram user is unknown")
            await self._call_required(self._users, "set_access_enabled", target, enabled)
            return await self.admin_users(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("Telegram user access could not be changed") from None

    async def admin_access_requests(
        self,
        user_id: int,
        page: int = 0,
    ) -> AdminAccessRequestPage:
        try:
            await self._context(user_id, admin=True)
            page = self._page_number(page)
            rows = await self._call_required(
                self._access_requests,
                "pending",
                limit=ADMIN_PAGE_SIZE + 1,
                offset=page * ADMIN_PAGE_SIZE,
            )
            return AdminAccessRequestPage(
                items=tuple(self._access_request_dto(row) for row in rows[:ADMIN_PAGE_SIZE]),
                page=page,
                has_previous=page > 0,
                has_next=len(rows) > ADMIN_PAGE_SIZE,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("access request list is unavailable") from None

    @_control_audit_boundary("admin_decide_access")
    async def admin_decide_access(
        self,
        user_id: int,
        request_id: int,
        *,
        approve: bool,
    ) -> AccessRequestDTO:
        try:
            await self._context(user_id, admin=True)
            normalized_request = self._numeric_id(request_id)
            if normalized_request is None or not isinstance(approve, bool):
                raise ValidationError("access request decision is invalid")
            result = await self._call_required(
                self._access_requests,
                "decide",
                normalized_request,
                user_id,
                approve=approve,
            )
            row, changed = result
            return self._access_request_dto(row, decision_changed=bool(changed))
        except KeyError:
            raise ValidationError("access request is unknown") from None
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("access request decision could not be saved") from None

    async def admin_followers(self, user_id: int, page: int = 0) -> AdminFollowerPage:
        try:
            await self._context(user_id, admin=True)
            page = self._page_number(page)
            rows = await self._accounts_for_admin()
            start = page * ADMIN_PAGE_SIZE
            selected = rows[start : start + ADMIN_PAGE_SIZE + 1]
            global_paused = _is_false(await self._flag("allow_new_entries", "true"))
            global_emergency = _is_true(await self._flag("emergency_active"))
            items: list[AdminFollowerItem] = []
            for row in selected[:ADMIN_PAGE_SIZE]:
                account_id = str(_row_value(row, "id"))
                owner = await self._user_by_account(account_id)
                policy = await self._call_optional(self._settings, "get_policy", account_id)
                registered = await self._runtime_registered(account_id)
                if not registered or policy is _MISSING or policy is None:
                    state = CopyState.DEGRADED
                elif (
                    global_paused
                    or global_emergency
                    or not _is_true(_row_value(policy, "allow_new_entries", 0))
                ):
                    state = CopyState.PAUSED
                else:
                    state = CopyState.ACTIVE
                items.append(
                    AdminFollowerItem(
                        account_id=account_id,
                        owner_user_id=_int_or_none(_row_value(owner, "telegram_user_id")),
                        account_masked=mask_address(str(_row_value(row, "user_address", ""))),
                        copy_state=state,
                        risk_percent=_decimal_or_none(_row_value(policy, "risk_percent")),
                    )
                )
            return AdminFollowerPage(
                items=tuple(items),
                page=page,
                has_previous=page > 0,
                has_next=len(selected) > ADMIN_PAGE_SIZE,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin follower list is unavailable") from None

    @_control_audit_boundary("admin_reconcile")
    async def admin_reconcile(
        self, user_id: int, account_id: str | None = None
    ) -> AdminDashboardDTO:
        try:
            await self._context(user_id, admin=True)
            await self._ensure_testnet()
            if account_id is not None:
                row = await self._account_row(account_id)
                if row is None or str(_row_value(row, "role", "")) != "follower":
                    raise ValidationError("account is invalid")
                await self._call_required(
                    self._runtime,
                    "reconcile_account",
                    account_id,
                )
            else:
                await self._call_required(
                    self._runtime,
                    "reconcile_all",
                )
            return await self.admin_dashboard(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin reconcile is unavailable") from None

    @_control_audit_boundary("admin_pause")
    async def admin_pause(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO:
        try:
            await self._context(user_id, admin=True)
            if confirmation is None:
                raise ConfirmationExpiredError("admin confirmation is required")
            await self._consume_confirmation(
                user_id, confirmation, expected=ConfirmationAction.GLOBAL_PAUSE
            )
            await self._perform_global_pause()
            return await self.admin_dashboard(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin pause is unavailable") from None

    @_control_audit_boundary("admin_resume")
    async def admin_resume(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO:
        try:
            await self._context(user_id, admin=True)
            if confirmation is None:
                raise ConfirmationExpiredError("admin confirmation is required")
            await self._consume_confirmation(
                user_id, confirmation, expected=ConfirmationAction.GLOBAL_RESUME
            )
            await self._perform_global_resume()
            return await self.admin_dashboard(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin resume is unavailable") from None

    @_control_audit_boundary("admin_emergency")
    async def admin_emergency(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO:
        try:
            await self._context(user_id, admin=True)
            if confirmation is None:
                raise ConfirmationExpiredError("admin confirmation is required")
            await self._consume_confirmation(
                user_id, confirmation, expected=ConfirmationAction.GLOBAL_EMERGENCY
            )
            await self._perform_global_emergency()
            return await self.admin_dashboard(user_id)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("admin emergency is unavailable") from None


def _row_bool(row: Any, key: str) -> bool | None:
    value = _row_value(row, key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if _is_true(value):
        return True
    if _is_false(value):
        return False
    return bool(value)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value is _MISSING:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value is _MISSING:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "EmergencyControllerPort",
    "OnboardingPort",
    "Stage2AccountLifecycle",
    "Stage2RuntimeAdapter",
    "UserApplicationService",
]
