"""Structured notifications and SQLite-backed Telegram delivery."""

from copytradeaster.notifications.console import ConsoleNotifier, notify
from copytradeaster.notifications.formatter import (
    format_confirmed_action,
    format_skip_reason,
)
from copytradeaster.notifications.models import (
    MAX_NOTIFICATION_TEXT,
    NotificationEvent,
    NotificationSeverity,
    clip_plain_text,
)
from copytradeaster.notifications.protocol import CallableNotifier, Notifier, as_notifier
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.notifications.worker import TelegramOutboxWorker

__all__ = [
    "CallableNotifier",
    "ConsoleNotifier",
    "MAX_NOTIFICATION_TEXT",
    "NotificationEvent",
    "NotificationRouter",
    "NotificationSeverity",
    "Notifier",
    "TelegramOutboxWorker",
    "as_notifier",
    "clip_plain_text",
    "format_confirmed_action",
    "format_skip_reason",
    "notify",
]
