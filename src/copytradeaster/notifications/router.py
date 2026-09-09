"""Recipient routing and durable SQLite enqueue boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from copytradeaster.domain.models import ActionKind, ExecutionAction
from copytradeaster.logging_utils import get_logger
from copytradeaster.notifications.console import ConsoleNotifier
from copytradeaster.notifications.formatter import format_confirmed_action
from copytradeaster.notifications.models import (
    NotificationEvent,
    NotificationSeverity,
    clip_plain_text,
)
from copytradeaster.notifications.protocol import Notifier, as_notifier
from copytradeaster.notifications.trade_cards import (
    event_notification_action,
    format_user_event,
)
from copytradeaster.storage.events import StoredEvent
from copytradeaster.storage.repositories import (
    CommandRepo,
    NotificationOutboxRepo,
    TelegramUserRepo,
)

log = get_logger(__name__)
_SAFE_FRAGMENT = re.compile(r"[^A-Za-z0-9_.:-]+")
_RECOVERY_WARN_KINDS = frozenset({"PROTECTION_UNCONFIRMED", "CONNECTIVITY_LOST"})


def _recovery_severity(kind: str) -> NotificationSeverity:
    """Lost protection and lost connectivity warn; trade facts inform."""
    if kind in _RECOVERY_WARN_KINDS:
        return NotificationSeverity.WARN
    return NotificationSeverity.INFO


def _safe_fragment(value: Any, fallback: str = "unknown") -> str:
    text = _SAFE_FRAGMENT.sub("_", str(value or ""))[:80].strip("_")
    return text or fallback


def _safe_created_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class NotificationRouter:
    """Structured notifier that fans events into console and SQLite outbox.

    The router only performs a short database enqueue.  It never calls a Bot
    API and therefore cannot make a trading result wait for Telegram.
    """

    def __init__(
        self,
        db: Any,
        *,
        admin_ids: Iterable[int] = (),
        console: Notifier | Any | None = None,
        users: TelegramUserRepo | None = None,
        outbox: NotificationOutboxRepo | None = None,
        user_events_only: bool = False,
    ) -> None:
        self._user_events_only = user_events_only
        self._users = users or TelegramUserRepo(db)
        self._outbox = outbox or NotificationOutboxRepo(db)
        self._admin_ids = frozenset(int(value) for value in admin_ids)
        self._console = as_notifier(console or ConsoleNotifier())

    @staticmethod
    def _deliverable(row: Any | None) -> bool:
        return row is not None and bool(row["notifications_enabled"]) and row["blocked_at"] is None

    async def _admin_recipients(self) -> list[Any]:
        # The configured ID allow-list is the sole authority.  Database role
        # is deliberately not filtered: one identity may be both the owner of
        # a follower account and an administrator.
        if not self._admin_ids:
            return []
        return await self._users.notification_recipients(
            telegram_user_ids=self._admin_ids,
        )

    async def _recipients(self, event: NotificationEvent) -> list[Any]:
        recipients: dict[int, Any] = {}
        if event.account_id is not None:
            owner = await self._users.by_account(event.account_id)
            if self._deliverable(owner):
                recipients[int(owner["telegram_user_id"])] = owner
            if event.severity != NotificationSeverity.CRITICAL:
                return list(recipients.values())

        for admin in await self._admin_recipients():
            recipients[int(admin["telegram_user_id"])] = admin
        return list(recipients.values())

    @staticmethod
    def plan_outbox_rows(
        event: NotificationEvent, recipients: Iterable[Any]
    ) -> list[dict[str, Any]]:
        """Build one durable outbox row per recipient without touching the DB.

        Shared by the live ``emit`` path and the durable delivery sweep so
        both persist the identical row shape.
        """
        return [
            {
                "event_key": event.event_key,
                "account_id": event.account_id,
                "telegram_user_id": int(row["telegram_user_id"]),
                "chat_id": int(row["chat_id"]),
                "kind": event.kind,
                "severity": str(event.severity),
                "text": clip_plain_text(event.text),
                "created_at": event.created_at,
                "updated_at": event.created_at,
                "trade_ref": event.trade_ref,
                "action": event.action,
            }
            for row in recipients
        ]

    async def prepare_user_event_notification(
        self, stored: StoredEvent
    ) -> tuple[NotificationEvent, list[Any]] | None:
        """Format one stored event and resolve recipients before any commit.

        Returns ``None`` for an explicit deliberate skip: no deliverable
        recipient exists, so enabling notifications later must never
        retroactively send it. Raises ``ValueError`` for an unrenderable
        eligible row (unknown kind, action/ref mismatch); the durable sweep
        keeps such rows blocking instead of advancing past them silently.
        """
        record = stored.event
        text = format_user_event(record)
        event = NotificationEvent(
            event_key=f"user-event:{record.event_ref}",
            account_id=stored.scope.account_id,
            kind=f"user.{record.kind}",
            severity=_recovery_severity(record.kind),
            text=text,
            created_at=datetime.fromtimestamp(record.occurred_at / 1000, tz=UTC),
            trade_ref=record.trade_ref,
            action=event_notification_action(record.kind),
        )
        recipients = await self._recipients(event)
        if not recipients:
            return None
        return (event, recipients)

    async def emit(self, event: NotificationEvent) -> None:
        # Console is deliberately best-effort: a broken local sink must not
        # prevent durable outbox enqueue.
        try:
            await self._console.emit(event)
        except Exception:  # noqa: BLE001 - local sink must not affect trading
            log.exception("console notification sink failed")

        if self._user_events_only and event.kind == "execution.confirmed":
            return  # Execution acknowledgement is not a position fact.
        recipients = await self._recipients(event)
        if not recipients:
            return
        await self._outbox.enqueue_many(self.plan_outbox_rows(event, recipients))

    async def __call__(self, event: NotificationEvent) -> None:
        await self.emit(event)

    async def backlog(self) -> dict[str, int]:
        return await self._outbox.counts()

    async def recover_confirmed_commands(
        self,
        commands: CommandRepo,
        *,
        since: datetime | str,
        limit: int = 1000,
    ) -> int:
        """Recover recent confirmations after a durable caller-supplied floor."""
        if self._user_events_only:
            return 0
        recovered = 0
        for row in await commands.confirmed(since=since, limit=limit):
            command_key = str(row["command_key"])
            event_key = f"command:{command_key}"
            if await self._outbox.has_event(event_key):
                continue
            account_id = str(row["account_id"])
            payload: dict[str, Any] = {}
            raw_payload = (
                row.get("payload") if isinstance(row, dict) else getattr(row, "payload", None)
            )
            if raw_payload:
                try:
                    payload = (
                        json.loads(raw_payload)
                        if isinstance(raw_payload, str)
                        else dict(raw_payload)
                    )
                except Exception:
                    payload = {}
            symbol = str(payload.get("symbol") or "symbol")
            try:
                kind = ActionKind(str(row["kind"]))
            except Exception:
                kind = ActionKind.MARKET_OPEN
            coid = (
                row.get("client_order_id")
                if isinstance(row, dict)
                else getattr(row, "client_order_id", None)
            )
            raw_prio = (
                row.get("priority", 1) if isinstance(row, dict) else getattr(row, "priority", 1)
            )
            action = ExecutionAction(
                kind=kind,
                symbol=symbol,
                payload=payload,
                client_order_id=coid,
                replaces_client_order_id=None,
                priority=int(raw_prio),
            )
            text = format_confirmed_action(action, account_id=account_id)
            await self.emit(
                NotificationEvent(
                    event_key=event_key,
                    account_id=account_id,
                    kind="execution.confirmed",
                    severity=NotificationSeverity.INFO,
                    text=text,
                    created_at=_safe_created_at(row["updated_at"] or row["created_at"]),
                )
            )
            recovered += 1
        return recovered
