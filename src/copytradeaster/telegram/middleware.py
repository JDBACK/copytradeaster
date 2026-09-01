"""Transport middleware for the Stage 2 Telegram boundary."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import AiogramError
from aiogram.types import CallbackQuery, InaccessibleMessage, Message, Update

from copytradeaster.application.models import ApplicationProtocol, UserContext
from copytradeaster.telegram.access import GuestAccessCallback

Handler = Callable[[Any, dict[str, Any]], Awaitable[Any]]


class BoundedTaskTracker:
    """Small admission/drain gate for update tasks.

    aiogram already provides an update-level concurrency limit.  This tracker
    is intentionally independent so lifecycle code can close admission and
    drain tasks deterministically, and tests can exercise the boundary without
    starting polling.
    """

    def __init__(self, limit: int = 16) -> None:
        if limit <= 0:
            raise ValueError("task tracker limit must be positive")
        self.limit = limit
        self._tasks: set[asyncio.Task[Any]] = set()
        self._guard = asyncio.Lock()
        self._accepting = True

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    @property
    def accepting(self) -> bool:
        return self._accepting

    async def admit(self) -> bool:
        task = asyncio.current_task()
        if task is None:
            return False
        async with self._guard:
            if not self._accepting or len(self._tasks) >= self.limit:
                return False
            self._tasks.add(task)
            return True

    async def release(self) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        async with self._guard:
            self._tasks.discard(task)

    async def run(self, handler: Handler, event: Any, data: dict[str, Any]) -> Any:
        admitted = await self.admit()
        if not admitted:
            await _busy_response(event)
            return None
        try:
            return await handler(event, data)
        finally:
            await self.release()

    def close_admission(self) -> None:
        self._accepting = False

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for admitted tasks, cancelling only after the bounded timeout."""
        if timeout <= 0:
            timeout = 0.001
        while True:
            async with self._guard:
                pending = tuple(task for task in self._tasks if not task.done())
            if not pending:
                return
            done, pending_set = await asyncio.wait(pending, timeout=timeout)
            if not pending_set:
                return
            for task in pending_set:
                task.cancel()
            await asyncio.gather(*pending_set, return_exceptions=True)
            return


class TelegramTaskMiddleware(BaseMiddleware):
    """Apply :class:`BoundedTaskTracker` to message and callback updates."""

    def __init__(self, tracker: BoundedTaskTracker) -> None:
        self.tracker = tracker

    async def __call__(self, handler: Handler, event: Any, data: dict[str, Any]) -> Any:
        return await self.tracker.run(handler, event, data)


class CallbackAnswerMiddleware(BaseMiddleware):
    """Acknowledge callback queries before FSM/event-isolation can wait.

    aiogram's FSM middleware is installed on the update observer and acquires
    ``SimpleEventIsolation`` before a callback reaches router middleware.  A
    slow first callback would otherwise leave a second callback's Telegram
    spinner running until the first application call completes.  The runtime
    installs this middleware immediately before FSM middleware and all router
    paths therefore consume exactly one acknowledgement.
    """

    async def __call__(self, handler: Handler, event: Any, data: dict[str, Any]) -> Any:
        query = event.callback_query if isinstance(event, Update) else event
        if isinstance(query, CallbackQuery):
            text: str | None = None
            if isinstance(query.message, InaccessibleMessage):
                text = "Меню недоступно, откройте /menu"
            elif query.message is None or (
                isinstance(query.message, Message) and query.message.chat.type != "private"
            ):
                text = "Недоступно"
            with contextlib.suppress(AiogramError):
                await query.answer(text=text, cache_time=0)
        return await handler(event, data)


class PrivateChatAuthorizationMiddleware(BaseMiddleware):
    """Reject group/channel updates before any application handler runs."""

    def __init__(self, application: ApplicationProtocol | None = None) -> None:
        self.application = application

    async def __call__(self, handler: Handler, event: Any, data: dict[str, Any]) -> Any:
        user_id, chat_id, chat_type = _event_identity(event)
        if user_id is None or chat_type != "private":
            await _deny_event(event, private=False)
            return None

        try:
            context = (
                await self.application.authorize(user_id, chat_id, chat_type)
                if self.application is not None
                else UserContext(user_id=user_id, chat_id=chat_id, allowed=True)
            )
        except Exception:
            # Authorization details are deliberately not exposed to Telegram.
            await _deny_event(event, private=True)
            return None

        if not context.allowed and not _guest_access_event(event):
            await _deny_event(event, private=True)
            return None
        data["user_context"] = context
        return await handler(event, data)


def _event_identity(event: Any) -> tuple[int | None, int, str]:
    if isinstance(event, Message):
        return event.from_user.id if event.from_user else None, event.chat.id, event.chat.type
    if isinstance(event, CallbackQuery):
        user_id = event.from_user.id if event.from_user else None
        message = event.message
        if isinstance(message, Message):
            return user_id, message.chat.id, message.chat.type
        if isinstance(message, InaccessibleMessage):
            return user_id, message.chat.id, "inaccessible"
        return user_id, 0, "inline"
    return None, 0, "unknown"


def _guest_access_event(event: Any) -> bool:
    """Allow only the narrow private entry flow before full authorization."""
    if isinstance(event, Message):
        text = event.text or ""
        return re.match(r"^/start(?:@[A-Za-z0-9_]+)?(?:\s|$)", text) is not None
    if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
        if event.message.chat.type != "private" or not isinstance(event.data, str):
            return False
        try:
            GuestAccessCallback.unpack(event.data)
        except (TypeError, ValueError):
            return False
        return True
    return False


async def _deny_event(event: Any, *, private: bool) -> None:
    if isinstance(event, CallbackQuery):
        # CallbackAnswerMiddleware has already acknowledged this update before
        # authorization and FSM isolation.  A second answer would be a Bot
        # API error and can mask the actual deny path.
        return
    if private and isinstance(event, Message):
        with contextlib.suppress(AiogramError):
            await event.answer("Доступ пока не открыт. Начните с /start.")


async def _busy_response(event: Any) -> None:
    if isinstance(event, CallbackQuery):
        # The update-level callback acknowledgement already stopped Telegram's
        # spinner before admission was attempted.
        return
    elif isinstance(event, Message) and event.chat.type == "private":
        with contextlib.suppress(AiogramError):
            await event.answer("Сервис занят, повторите чуть позже.")
