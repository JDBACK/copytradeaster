"""Notifier protocol and callable adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from copytradeaster.notifications.models import NotificationEvent

NotificationCallback = Callable[[NotificationEvent], Awaitable[None]]


@runtime_checkable
class Notifier(Protocol):
    async def emit(self, event: NotificationEvent) -> None:
        """Accept an event without waiting for Telegram delivery."""


@dataclass(frozen=True, slots=True)
class CallableNotifier:
    """Adapt an async callable into the structured notifier port."""

    callback: NotificationCallback

    async def emit(self, event: NotificationEvent) -> None:
        await self.callback(event)

    async def __call__(self, event: NotificationEvent) -> None:
        await self.emit(event)


def as_notifier(value: Notifier | NotificationCallback) -> Notifier:
    if isinstance(value, Notifier):
        return value
    if callable(value):
        return CallableNotifier(value)
    raise TypeError("notifier must implement emit() or be an async callable")
