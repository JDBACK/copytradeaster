"""Direct lifecycle and authorization tests for Telegram middleware seams."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Message

from copytradeaster.application.models import UserContext
from copytradeaster.telegram.middleware import (
    BoundedTaskTracker,
    PrivateChatAuthorizationMiddleware,
    _busy_response,
    _event_identity,
)


def make_message(*, chat_type: str = "private", text: str = "hello") -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 42, "type": chat_type},
            "from": {"id": 7, "is_bot": False, "first_name": "Tester"},
            "text": text,
        }
    )


@pytest.mark.asyncio
async def test_tracker_busy_admission_handler_release_and_bounded_drain() -> None:
    with pytest.raises(ValueError, match="positive"):
        BoundedTaskTracker(0)

    tracker = BoundedTaskTracker(1)
    assert await tracker.admit()
    assert not await tracker.admit()
    await tracker.release()

    seen: list[str] = []

    async def handler(event, data):
        seen.append(f"{event}:{data['value']}")
        return "done"

    assert await tracker.run(handler, "event", {"value": 1}) == "done"
    assert seen == ["event:1"]
    tracker.close_admission()
    assert not tracker.accepting
    assert await tracker.run(handler, object(), {"value": 2}) is None

    draining = BoundedTaskTracker(1)
    started = asyncio.Event()

    async def blocked(event, data):
        del event, data
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(draining.run(blocked, object(), {}))
    await started.wait()
    draining.close_admission()
    await draining.drain(timeout=0.001)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert draining.active_count == 0


@pytest.mark.asyncio
async def test_authorization_exception_denies_private_message_without_handler(
    monkeypatch,
) -> None:
    message = make_message(text="/start")
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)

    class BrokenAuthorization:
        async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext:
            del user_id, chat_id, chat_type
            raise RuntimeError("authorization backend unavailable")

    handler = AsyncMock()
    middleware = PrivateChatAuthorizationMiddleware(BrokenAuthorization())
    assert await middleware(handler, message, {}) is None
    handler.assert_not_awaited()
    answer.assert_awaited_once_with("Доступ пока не открыт. Начните с /start.")


@pytest.mark.asyncio
async def test_busy_callback_and_unknown_event_are_noop_boundaries() -> None:
    callback = CallbackQuery.model_validate(
        {
            "id": "q-1",
            "from": {"id": 7, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "instance",
            "data": "m:h::0:0:",
        }
    )
    await _busy_response(callback)
    assert _event_identity(object()) == (None, 0, "unknown")
