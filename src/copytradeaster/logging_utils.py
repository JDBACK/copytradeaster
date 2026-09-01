"""Logging helpers with a persistent, secret-safe application sink.

Private keys, signatures, tokens, wallet addresses and signed query strings
must never be written by application logging.  The helpers in this module keep
the operational log useful (action, stable account, result and category) while
making the default local sink durable and bounded.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

DEFAULT_LOG_PATH = Path("data/logs/copytradeaster.log")
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_HANDLER_KIND = "_copytradeaster_handler_kind"
_CONSOLE_HANDLER = "console"
_FILE_HANDLER = "file"
_SAFE_FRAGMENT = re.compile(r"[^A-Za-z0-9_.:-]+")
_ADDRESS = re.compile(r"(?i)^(?:0x)?[0-9a-f]{40}$")
_HEX_ADDRESS_IN_MESSAGE = re.compile(r"(?i)(?<![0-9a-f])0x[0-9a-f]{40}(?![0-9a-f])")
_HEX_SECRET_IN_MESSAGE = re.compile(r"(?i)(?<![0-9a-f])0x[0-9a-f]{64}(?![0-9a-f])")
_URL_QUERY = re.compile(r"(?P<base>(?:https?://|/)[^\s?]+)\?[^\s]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?P<key>private[_ -]?key|api[_ -]?key|access[_ -]?token|token|"
    r"signature|secret|listen[_ -]?key)\s*[:=]\s*(?P<value>[^\s,;&]+)"
)

# These values are deliberately closed: a control audit record describes one
# known application use case, not a caller-provided free-form event/message.
_CONTROL_AUDIT_ACTIONS = frozenset(
    {
        "set_notifications",
        "update_risk",
        "pause",
        "resume",
        "confirm_action",
        "confirm_resume",
        "confirm_emergency",
        "confirm_disconnect",
        "confirm_global_pause",
        "confirm_global_resume",
        "confirm_global_emergency",
        "admin_allow_user",
        "admin_set_user_access",
        "admin_decide_access",
        "admin_reconcile",
        "admin_pause",
        "admin_resume",
        "admin_emergency",
    }
)
_CONTROL_AUDIT_RESULTS = frozenset({"attempt", "success", "rejected"})
_CONTROL_AUDIT_STATES = frozenset(
    {
        "enabled",
        "disabled",
        "paused",
        "resumed",
        "approved",
        "rejected",
        "reconciled",
        "emergency",
        "disconnected",
        "global",
        "account",
        "all",
        "unknown",
    }
)
_CONTROL_AUDIT_CATEGORIES = frozenset(
    {
        "validation",
        "authorization",
        "service_unavailable",
        "unexpected",
        "unknown",
    }
)
_CONTROL_AUDIT_CHANGED_FIELDS = frozenset(
    {
        "risk_percent",
        "max_leverage",
        "max_entry_deviation_pct",
        "notifications_enabled",
        "access_enabled",
        "status",
        "allow_new_entries",
        "emergency_active",
        "account_enabled",
    }
)
_CONTROL_AUDIT_MAX_FIELDS = 8


def redact(secret: str) -> str:
    """Mask everything after the prefix (0x or first 2 chars) to last 4."""
    if not secret:
        return secret
    prefix = "0x" if secret.startswith("0x") else secret[:2]
    if len(secret) <= len(prefix) + 4:
        return f"{prefix}***"
    return f"{prefix}***{secret[-4:]}"


def get_logger(name: str = "copytradeaster") -> logging.Logger:
    return logging.getLogger(name)


def _safe_fragment(value: Any, fallback: str = "unknown", limit: int = 80) -> str:
    """Return a bounded non-secret identifier for an audit field."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return str(value) if value > 0 else fallback
    text = str(value or "")
    if _ADDRESS.fullmatch(text.strip()):
        return "redacted"
    result = _SAFE_FRAGMENT.sub("_", text)[:limit].strip("_")
    return result or fallback


def _control_fixed(value: Any, allowed: frozenset[str], fallback: str = "unknown") -> str:
    """Return a value only when it is one of the fixed audit classifications."""
    try:
        candidate = getattr(value, "value", value)
        safe = _safe_fragment(candidate, fallback)
    except Exception:  # pragma: no cover - defensive boundary for logging
        return fallback
    return safe if safe in allowed else fallback


def _control_identifier(value: Any, fallback: str = "unknown") -> str:
    """Normalize a bounded/sanitized user or account identifier."""
    try:
        return _safe_fragment(value, fallback, limit=64)
    except Exception:  # pragma: no cover - defensive boundary for logging
        return fallback


def _control_changed_fields(values: Any) -> tuple[str, ...]:
    """Keep only known field names and never serialize field values."""
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    try:
        iterator = iter(values)
    except TypeError:
        return ()
    fields: list[str] = []
    for value in iterator:
        try:
            candidate = _safe_fragment(getattr(value, "value", value), "", limit=40)
        except Exception:  # pragma: no cover - defensive boundary for logging
            continue
        if candidate in _CONTROL_AUDIT_CHANGED_FIELDS and candidate not in fields:
            fields.append(candidate)
        if len(fields) >= _CONTROL_AUDIT_MAX_FIELDS:
            break
    return tuple(fields)


def _redact_handler_message(message: str) -> str:
    """Best-effort last line of defence for the persistent local sink.

    Call sites are still required to construct safe messages.  This filter
    catches accidental URL/query and common credential assignment strings in
    third-party or legacy log records before they reach our handlers.
    """
    message = _URL_QUERY.sub(r"\g<base>?<redacted>", message)
    message = _SECRET_ASSIGNMENT.sub(r"\g<key>=<redacted>", message)
    # The application contract must never pass addresses or private keys to a
    # logger.  These two narrow substitutions are only defense-in-depth for
    # legacy/third-party records; they intentionally do not attempt to infer
    # arbitrary secrets from opaque strings.
    message = _HEX_SECRET_IN_MESSAGE.sub("0x<redacted>", message)
    return _HEX_ADDRESS_IN_MESSAGE.sub("0x<redacted>", message)


class _SecretSafeHandlerFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - logging must not break the app
            return True
        safe_message = _redact_handler_message(message)
        if safe_message != message:
            record.msg = safe_message
            record.args = ()
            # An exception traceback can repeat a payload even when the main
            # message was cleaned.  Do not persist it through our sinks.
            record.exc_info = None
            record.exc_text = None
        return True


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """Rotating file handler whose active sink is always owner-readable."""

    def _open(self):
        stream = super()._open()
        try:
            # fchmod applies to the descriptor that was actually opened and
            # therefore also fixes pre-existing files before the first write.
            os.fchmod(stream.fileno(), 0o600)
        except (AttributeError, OSError):
            # fchmod is not available on every supported platform.  chmod is
            # the portable fallback; POSIX uses the descriptor path above.
            os.chmod(self.baseFilename, 0o600)
        return stream


def _set_handler_metadata(handler: logging.Handler, kind: str) -> None:
    setattr(handler, _HANDLER_KIND, kind)
    handler.addFilter(_SecretSafeHandlerFilter())


def _managed_handlers(root: logging.Logger, kind: str) -> list[logging.Handler]:
    return [handler for handler in root.handlers if getattr(handler, _HANDLER_KIND, None) == kind]


def _find_managed_handler(root: logging.Logger, kind: str) -> logging.Handler | None:
    return next(iter(_managed_handlers(root, kind)), None)


def _remove_managed_duplicates(
    root: logging.Logger, kind: str, keep: logging.Handler | None
) -> None:
    for handler in _managed_handlers(root, kind):
        if handler is keep:
            continue
        root.removeHandler(handler)
        handler.close()


def _configure_console(root: logging.Logger, formatter: logging.Formatter) -> None:
    handler = _find_managed_handler(root, _CONSOLE_HANDLER)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        _set_handler_metadata(handler, _CONSOLE_HANDLER)
        root.addHandler(handler)
    _remove_managed_duplicates(root, _CONSOLE_HANDLER, handler)
    handler.setFormatter(formatter)


def _configure_file(
    root: logging.Logger,
    formatter: logging.Formatter,
    log_path: str | Path | None,
    max_bytes: int,
    backup_count: int,
) -> None:
    existing = _find_managed_handler(root, _FILE_HANDLER)
    if log_path is None:
        _remove_managed_duplicates(root, _FILE_HANDLER, None)
        return

    if max_bytes <= 0:
        raise ValueError("log_max_bytes must be positive")
    if backup_count < 0:
        raise ValueError("log_backup_count must be non-negative")
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_path = (
        Path(getattr(existing, "baseFilename", "")).resolve() if existing is not None else None
    )
    if existing is not None and existing_path != path.resolve():
        root.removeHandler(existing)
        existing.close()
        existing = None
    _remove_managed_duplicates(root, _FILE_HANDLER, existing)
    if existing is None:
        existing = _OwnerOnlyRotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        _set_handler_metadata(existing, _FILE_HANDLER)
        root.addHandler(existing)
    else:
        existing.maxBytes = max_bytes
        existing.backupCount = backup_count
    existing.setFormatter(formatter)


def setup_logging(
    level: str = "INFO",
    *,
    log_path: str | Path | None = DEFAULT_LOG_PATH,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Configure idempotent console + rotating local-file application logs.

    ``log_path=None`` is an explicit opt-out for narrow tests/embedders.  The
    application settings use :data:`DEFAULT_LOG_PATH`, so a normal restart
    always opens ``data/logs/copytradeaster.log`` without a manual env change.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    formatter = logging.Formatter(LOG_FORMAT)
    _configure_console(root, formatter)
    _configure_file(root, formatter, log_path, max_bytes, backup_count)
    # httpx logs complete request URLs at INFO.  Signed Aster USER_DATA
    # requests carry credentials and signatures in that query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_onboarding_audit(
    logger: logging.Logger,
    *,
    event: str,
    mode: str,
    user_id: Any,
    account_id: Any = None,
    result: str,
    paused: bool | None,
    category: str | None = None,
    code: Any = None,
    exception_type: Any = None,
) -> None:
    """Write one transport-neutral, credential-free onboarding audit event.

    Only bounded identifiers and fixed classifications are accepted here.  In
    particular, callers do not pass credential/address values or exception
    objects, which keeps this helper usable by a future Mini App adapter too.
    """
    safe_event = _safe_fragment(event)
    safe_mode = _safe_fragment(mode)
    safe_user = _safe_fragment(user_id)
    safe_account = _safe_fragment(account_id, "unknown")
    safe_result = _safe_fragment(result)
    fields = [
        f"onboarding_audit event={safe_event}",
        f"mode={safe_mode}",
        f"user_id={safe_user}",
        f"account_id={safe_account}",
        f"result={safe_result}",
        f"paused={'unknown' if paused is None else str(bool(paused)).lower()}",
    ]
    if category is not None:
        fields.append(f"category={_safe_fragment(category)}")
    if code is not None:
        value = getattr(code, "value", code)
        fields.append(f"code={_safe_fragment(value)}")
    if exception_type is not None:
        fields.append(f"exception_type={_safe_fragment(exception_type)}")
    message = " ".join(fields)
    if safe_event == "rejected":
        logger.warning(message)
    else:
        logger.info(message)


def log_control_audit(
    logger: logging.Logger,
    *,
    action: Any,
    result: Any,
    user_id: Any,
    account_id: Any = None,
    target_user_id: Any = None,
    changed_fields: Iterable[Any] | None = None,
    state: Any = None,
    category: Any = None,
    code: Any = None,
    exception_type: Any = None,
) -> None:
    """Write one bounded, transport-neutral audit event for a control action.

    The helper accepts classifications and bounded/sanitized identifiers only.  It has no
    message/exception/credential arguments, and a logger failure is swallowed
    so observability can never change the application result.
    """
    try:
        safe_action = _control_fixed(action, _CONTROL_AUDIT_ACTIONS)
        safe_result = _control_fixed(result, _CONTROL_AUDIT_RESULTS)
        fields = [
            f"control_audit action={safe_action}",
            f"result={safe_result}",
            f"user_id={_control_identifier(user_id)}",
            f"account_id={_control_identifier(account_id)}",
        ]
        if target_user_id is not None:
            fields.append(f"target_user_id={_control_identifier(target_user_id)}")
        safe_fields = _control_changed_fields(changed_fields)
        if safe_fields:
            fields.append(f"changed_fields={','.join(safe_fields)}")
        if state is not None:
            fields.append(f"state={_control_fixed(state, _CONTROL_AUDIT_STATES)}")
        if category is not None:
            fields.append(f"category={_control_fixed(category, _CONTROL_AUDIT_CATEGORIES)}")
        if code is not None:
            value = getattr(code, "value", code)
            fields.append(f"code={_control_identifier(value)}")
        if exception_type is not None:
            fields.append(f"exception_type={_control_identifier(exception_type)}")
        message = " ".join(fields)
        if safe_result == "rejected":
            logger.warning(message)
        else:
            logger.info(message)
    except Exception:
        # Logging is diagnostic only.  In particular, a custom handler or
        # malformed value must not add a new failure path to application code.
        return


__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_MAX_BYTES",
    "DEFAULT_LOG_PATH",
    "get_logger",
    "log_control_audit",
    "log_onboarding_audit",
    "redact",
    "setup_logging",
]
