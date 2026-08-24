"""Transport-neutral notification value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class NotificationSeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


def _created_at(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """Immutable safe event emitted independently of Telegram delivery."""

    event_key: str
    kind: str
    severity: NotificationSeverity | str
    text: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    account_id: str | None = None

    def __post_init__(self) -> None:
        if not self.event_key:
            raise ValueError("notification event_key must not be empty")
        if not self.kind:
            raise ValueError("notification kind must not be empty")
        if not isinstance(self.text, str):
            raise TypeError("notification text must be a string")
        try:
            severity = NotificationSeverity(str(self.severity).upper())
        except ValueError as exc:
            raise ValueError(f"unsupported notification severity: {self.severity!r}") from exc
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "created_at", _created_at(self.created_at))


MAX_NOTIFICATION_TEXT = 4096


def clip_plain_text(text: str, limit: int = MAX_NOTIFICATION_TEXT) -> str:
    """Bound plain text before Bot API send; parse_mode is deliberately None."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"
