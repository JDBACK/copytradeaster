"""Transport-neutral application service for Telegram and future clients.

The service deliberately depends on small repository/runtime ports.  The
current :class:`copytradeaster.app.App` can be wrapped by composition code,
while tests and a future Mini App can provide faithful adapters without
importing Telegram types here.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Protocol

from copytradeaster.application.models import (
    AccessRequestDTO,
    AccessRequestStatus,
    ActivityCategory,
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
    AuthorizationError,
    BuilderOnboardedDTO,
    BuilderPrepareDTO,
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    CopyState,
    DashboardDTO,
    DirectCredentialsInput,
    MiniAppAccessState,
    MiniAppConnectionState,
    MiniAppSessionDTO,
    NotificationSettingsDTO,
    OnboardingApplicationError,
    OnboardingResult,
    PermissionSummary,
    PositionPageDTO,
    PositionProtectionState,
    PositionSide,
    PositionSummaryDTO,
    RiskDTO,
    RiskUpdateInput,
    ServiceUnavailableError,
    StaleOperationError,
    StatusDTO,
    SystemState,
    UserContext,
    UserRole,
    ValidationError,
)
from copytradeaster.logging_utils import get_logger, log_control_audit, log_onboarding_audit
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderPrepareRequest,
    BuilderSubmitRequest,
    DirectOnboardingRequest,
    DirectOnboardingResult,
    mask_address,
    stable_account_id,
)
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    NotificationOutboxRepo,
    SettingsRepo,
    TelegramAccessRequestRepo,
    TelegramUserRepo,
)

TESTNET_REST_URL = "https://fapi.asterdex-testnet.com"
TESTNET_WS_URL = "wss://fstream.asterdex-testnet.com/ws"
PERMISSION_TTL_SECONDS = 60
TESTNET_PERMISSION_FALLBACK_TTL_SECONDS = 86_400
ACTIVITY_PAGE_SIZE = 10
ADMIN_PAGE_SIZE = 10
ACCESS_REQUEST_RETRY_COOLDOWN_SECONDS = 60
# The token is embedded together with action/revision in callback_data by the
# presenter.  Keep a 16-byte ASCII budget so the longest action still remains
# comfortably below Telegram's 64-byte callback_data ceiling.
CONFIRMATION_TOKEN_MAX_BYTES = 16
_MISSING = object()
_SAFE_CODE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAFE_ERRORS = (
    AuthorizationError,
    ValidationError,
    OnboardingApplicationError,
    ConfirmationExpiredError,
    StaleOperationError,
    ServiceUnavailableError,
)

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

    async def read_activity(
        self,
        account_id: str,
        limit: int,
        offset: int,
        kind_filter: ActivityKindFilter | None = None,
    ) -> Sequence[Any]: ...

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


_TRADE_KINDS = frozenset(
    {
        "MARKET_OPEN",
        "MARKET_CLOSE",
        "PLACE_STOP",
        "PLACE_TP",
        "CANCEL_ORDER",
    }
)
_SYSTEM_KINDS = frozenset({"SET_LEVERAGE"})
# Deterministic ordering keeps the generated SQL and its bound parameters
# stable between the two code paths that build the same predicate.
_TRADE_KINDS_ORDERED: tuple[str, ...] = tuple(sorted(_TRADE_KINDS))


@dataclass(frozen=True, slots=True)
class ActivityKindFilter:
    """Category predicate pushed down into the activity SQL.

    The predicate must be applied **before** ``ORDER BY/LIMIT/OFFSET``.
    Filtering in Python after the database already truncated the journal to
    ``page_size + 1`` rows hides every matching row that sits further down the
    journal and makes ``has_next`` report ``False`` for a page that is merely
    empty because its slice contained no matching category.  Page N also has
    to offset the *filtered* set, otherwise rows are skipped.

    ``system`` is expressed as the exact complement of the trade allow-list so
    an unrecognised journal kind keeps landing in the system bucket exactly as
    ``_safe_activity_category`` classifies it, instead of silently vanishing
    from both filters.
    """

    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def clause(self) -> tuple[str, tuple[str, ...]]:
        """Return a ``(sql_fragment, bound_params)`` pair for the WHERE clause."""
        if self.included:
            placeholders = ", ".join("?" for _ in self.included)
            return f" AND kind IN ({placeholders})", self.included
        if self.excluded:
            placeholders = ", ".join("?" for _ in self.excluded)
            return f" AND kind NOT IN ({placeholders})", self.excluded
        return "", ()


def _activity_kind_filter(activity_filter: ActivityFilter) -> ActivityKindFilter:
    if activity_filter is ActivityFilter.TRADE:
        return ActivityKindFilter(included=_TRADE_KINDS_ORDERED)
    if activity_filter is ActivityFilter.SYSTEM:
        return ActivityKindFilter(excluded=_TRADE_KINDS_ORDERED)
    return ActivityKindFilter()


def _safe_activity_category(kind: str) -> ActivityCategory:
    if kind in _TRADE_KINDS:
        return ActivityCategory.TRADE
    if kind in _SYSTEM_KINDS:
        return ActivityCategory.SYSTEM
    return ActivityCategory.SYSTEM


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


def _safe_activity_leverage(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("leverage")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 1000:
        return None
    return value


def _safe_activity_title(kind: str, payload: Mapping[str, Any], symbol: str | None) -> str:
    if kind == "MARKET_OPEN":
        if symbol:
            return f"Открыта позиция {symbol}"
        return "Открыта позиция"
    if kind == "MARKET_CLOSE":
        if symbol:
            return f"Закрыта позиция {symbol}"
        return "Закрыта позиция"
    if kind == "PLACE_STOP":
        if symbol:
            return f"Поставлен защитный Stop · {symbol}"
        return "Поставлен защитный Stop"
    if kind == "PLACE_TP":
        if symbol:
            return f"Поставлен Take Profit · {symbol}"
        return "Поставлен Take Profit"
    if kind == "CANCEL_ORDER":
        if symbol:
            return f"Отменён ордер · {symbol}"
        return "Отменён ордер"
    if kind == "SET_LEVERAGE":
        leverage = _safe_activity_leverage(payload)
        if leverage is not None and symbol:
            return f"Плечо подготовлено: {leverage}× · {symbol}"
        if leverage is not None:
            return f"Плечо подготовлено: {leverage}×"
        if symbol:
            return f"Плечо обновлено · {symbol}"
        return "Плечо обновлено"
    return "Действие выполнено"


def _safe_activity_status(
    raw_status: Any,
    occurred_at: datetime | None,
    now: datetime,
) -> tuple[ActivityStatus, int | None, str | None]:
    status = _safe_code(raw_status, "UNKNOWN").upper()
    if status in {"PENDING", "SENDING", "SUBMITTED"}:
        age: int | None = None
        if occurred_at is not None:
            try:
                age = max(int((now - occurred_at).total_seconds()), 0)
            except (TypeError, ValueError, OverflowError):
                age = None
        return ActivityStatus.PENDING, age, None
    if status == "CONFIRMED":
        return ActivityStatus.COMPLETED, None, None
    if status in {"UNKNOWN", "FAILED"}:
        remediation = (
            "Проверьте фактические позиции и при необходимости нажмите «Обновить»."
            if status == "UNKNOWN"
            else "Действие не выполнено. Повторите позже или обновите страницу."
        )
        return ActivityStatus.FAILED, None, remediation
    return ActivityStatus.FAILED, None, "Действие в неизвестном состоянии."


def _coerce_activity_filter(value: Any) -> ActivityFilter:
    if isinstance(value, ActivityFilter):
        return value
    if isinstance(value, str):
        try:
            return ActivityFilter(value.strip().lower())
        except ValueError:
            pass
    raise ValidationError("activity filter is invalid")


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
        testnet_check: bool | Callable[[], bool | Awaitable[bool]] | None = None,
        clock: Callable[[], datetime] | None = None,
        confirmation_ttl_seconds: float = 60.0,
        permission_ttl_seconds: int = PERMISSION_TTL_SECONDS,
        activity_page_size: int = ACTIVITY_PAGE_SIZE,
    ) -> None:
        if runtime is None:
            raise ValueError("runtime adapter is required")
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation TTL must be positive")
        if permission_ttl_seconds <= 0:
            raise ValueError("permission TTL must be positive")
        if not 1 <= activity_page_size <= 100:
            raise ValueError("activity page size must be between 1 and 100")

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

        self._admin_ids = frozenset(self._parse_admin_ids(admin_ids, runtime))
        self._testnet_check = testnet_check
        self._clock = clock or (lambda: datetime.now(UTC))
        self._confirmation_ttl = float(confirmation_ttl_seconds)
        self._permission_ttl = int(permission_ttl_seconds)
        self._activity_page_size = int(activity_page_size)
        self._confirmations: dict[str, _ConfirmationRecord] = {}
        self._confirmation_lock = asyncio.Lock()
        self._mutation_locks: dict[str, asyncio.Lock] = {}
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
            ready = await self._flag("ready")
            if _is_true(ready):
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
            if _is_false(ready):
                return SystemState.UNKNOWN
            marker = getattr(self._runtime, "ready", _MISSING)
            if marker is True:
                return SystemState.READY
            return SystemState.UNKNOWN
        except Exception:
            return SystemState.UNKNOWN

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
        except ServiceUnavailableError:
            raise
        except Exception:
            raise StaleOperationError("permission refresh was rejected") from None

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
        method = "refresh_snapshot" if refresh else "cached_snapshot"
        result = await self._call_optional(self._runtime, method, account_id)
        if result is _MISSING and refresh:
            raise ServiceUnavailableError("account snapshot is unavailable")
        if result is _MISSING or result is None:
            return None, None
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], datetime):
            return result[0], result[1]
        return result, self._now() if refresh else None

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

    async def _activity_rows(
        self, account_id: str, offset: int, kind_filter: ActivityKindFilter
    ) -> list[Any]:
        limit = self._activity_page_size + 1
        reader = getattr(self._runtime, "read_activity", None)
        if callable(reader):
            result = await _maybe_await(reader(account_id, limit, offset, kind_filter=kind_filter))
            return list(result or ())
        db = getattr(self._runtime, "db", None)
        fetchall = getattr(db, "fetchall", None)
        if fetchall is None:
            raise ServiceUnavailableError("activity read is unavailable")
        clause, params = kind_filter.clause()
        return list(
            await fetchall(
                "SELECT command_key, kind, status, payload, created_at, updated_at "
                "FROM execution_commands WHERE account_id=?"
                f"{clause} "
                "ORDER BY updated_at DESC, command_key DESC LIMIT ? OFFSET ?",
                (account_id, *params, limit, offset),
            )
        )

    @staticmethod
    def _map_activity_item(row: Any, now: datetime) -> ActivityItem:
        """Map a raw ``execution_commands`` row into the safe Activity DTO.

        The mapping is deliberately the only place that knows about journal
        payload internals.  No ``command_key``, ``account_id``,
        ``client_order_id``, raw Aster code, payload JSON, address, nonce, or
        internal exception text ever leaves this helper.
        """
        kind = _safe_code(_row_value(row, "kind"), "command")
        raw_status = _row_value(row, "status")
        payload = _row_value(row, "payload")
        parsed_payload = _parse_activity_payload(payload)
        symbol = _safe_activity_symbol(parsed_payload)
        occurred_at = _as_datetime(_row_value(row, "updated_at")) or _as_datetime(
            _row_value(row, "created_at")
        )
        category = _safe_activity_category(kind)
        title = _safe_activity_title(kind, parsed_payload, symbol)
        status, pending_age, remediation = _safe_activity_status(raw_status, occurred_at, now)
        return ActivityItem(
            category=category,
            title=title,
            status=status,
            symbol=symbol,
            occurred_at=occurred_at,
            pending_age=pending_age,
            remediation=remediation,
        )

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

    async def _dashboard_for(
        self,
        context: UserContext,
        account_row: Any | None,
        *,
        account_id: str | None,
        snapshot: Any = None,
        snapshot_captured_at: datetime | None = None,
    ) -> DashboardDTO:
        system_state = await self._system_state()
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
        policy = await self._call_optional(self._settings, "get_policy", account_id)
        permission, _ = await self._permission(account_id)
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
            system_state = await self._system_state()
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

            positions_map = _row_value(snapshot, "positions", _MISSING)
            raw_positions = (
                (
                    positions_map.values()
                    if isinstance(positions_map, Mapping)
                    else (positions_map or ())
                )
                if positions_map is not _MISSING and positions_map is not None
                else ()
            )

            parsed_list: list[PositionSummaryDTO] = []
            for p in raw_positions:
                summary = self._parse_position_summary(
                    p, is_fresh=is_fresh, fee_buffer_pct=fee_buffer_pct
                )
                if summary is not None:
                    parsed_list.append(summary)

            parsed_list.sort(key=lambda item: item.symbol)

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

    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO:
        try:
            context = await self._context(user_id)
            account_id = str(context.account_id) if context.account_id else None
            if account_id is None:
                return StatusDTO(
                    system_state=await self._system_state(),
                    message="account is not connected",
                )
            context, row, _ = await self._owned_account(
                user_id,
                account_id,
                allow_disabled=True,
            )
            snapshot = None
            message = None
            refreshed_at = None
            if refresh:
                try:
                    snapshot, refreshed_at = await self._snapshot(account_id, refresh=True)
                except _SAFE_ERRORS:
                    raise
                except Exception:
                    message = "Aster status is temporarily unavailable"
            else:
                # Default status is a cheap cache read; only the explicit
                # refresh action crosses the bounded Aster read boundary.
                snapshot, refreshed_at = await self._snapshot(account_id, refresh=False)
            permission, checked_at = await self._permission(account_id, refresh=refresh)
            policy = await self._call_optional(self._settings, "get_policy", account_id)
            connected = _is_true(_row_value(row, "enabled", 0)) and await self._runtime_registered(
                account_id
            )
            if not _is_true(_row_value(row, "enabled", 0)):
                message = "account is disconnected; connect credentials to continue"
            global_pause = _is_false(await self._flag("allow_new_entries", "true"))
            global_emergency = _is_true(await self._flag("emergency_active"))
            copy_state = CopyState.ACTIVE
            if not _is_true(_row_value(row, "enabled", 0)):
                copy_state = CopyState.EMPTY
            elif (
                policy is _MISSING
                or policy is None
                or not _is_true(_row_value(policy, "allow_new_entries", 0))
                or global_pause
                or global_emergency
            ):
                copy_state = CopyState.PAUSED
            return StatusDTO(
                system_state=await self._system_state(),
                copy_state=copy_state,
                aster_connected=connected,
                account_masked=mask_address(str(_row_value(row, "user_address", ""))),
                positions_count=self._position_count(snapshot),
                permissions=permission,
                last_refresh_at=refreshed_at or checked_at,
                age_seconds=(
                    None
                    if checked_at is None
                    else max(int((self._now() - checked_at).total_seconds()), 0)
                ),
                message=message,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("status is unavailable") from None

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
        try:
            _, _, _ = await self._owned_account(user_id, allow_disabled=True)
            if isinstance(page, bool) or not isinstance(page, int) or page < 0:
                raise ValidationError("activity page is invalid")
            activity_filter = _coerce_activity_filter(filter)
            context = await self._context(user_id)
            # The category predicate is applied inside the SQL query, so the
            # LIMIT/OFFSET window and has_next describe the *filtered* set.
            rows = await self._activity_rows(
                str(context.account_id),
                page * self._activity_page_size,
                _activity_kind_filter(activity_filter),
            )
            now = self._now()
            items = [self._map_activity_item(row, now) for row in rows]
            has_next = len(items) > self._activity_page_size
            return ActivityPage(
                items=tuple(items[: self._activity_page_size]),
                page=page,
                has_previous=page > 0,
                has_next=has_next,
                filter=activity_filter,
            )
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("activity is unavailable") from None

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
            deviation = cls._decimal_input(
                _row_value(policy, "max_entry_deviation_pct"), "max_entry_deviation_pct"
            )
            leverage = _row_value(policy, "max_leverage")
            if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage <= 0:
                raise ValidationError("risk values are invalid")
            return RiskDTO(
                risk_percent=risk,
                max_leverage=int(leverage),
                max_entry_deviation_pct=deviation,
                risk_base=str(_row_value(policy, "risk_base", "available_balance")),
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
            changes: dict[str, Any] = {}
            if values.risk_percent is not None:
                value = self._decimal_input(values.risk_percent, "risk_percent")
                if not Decimal(0) < value <= Decimal(1):
                    raise ValidationError("risk values are invalid")
                changes["risk_percent"] = value
            if not changes:
                return await self.risk(context.user_id)
            async with self._account_lock(str(context.account_id)):
                policy = await self._call_required(
                    self._settings, "patch_policy", str(context.account_id), **changes
                )
            return self._risk_dto(policy)
        except _SAFE_ERRORS:
            raise
        except Exception:
            raise ServiceUnavailableError("risk update is unavailable") from None

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
            raise ServiceUnavailableError("global emergency is active")
        if _is_false(await self._flag("allow_new_entries", "true")):
            raise ServiceUnavailableError("global pause is active")
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
                raise ServiceUnavailableError("global emergency is active")
            if _is_false(await self._flag("allow_new_entries", "true")):
                raise ServiceUnavailableError("global pause is active")
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
            return await self._resume_account(
                context.user_id,
                str(context.account_id),
                expected_account_version=expected_version,
            )
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
