from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, WebhookInfo

from copytradeaster.application.models import (
    AsterState,
    Confirmation,
    ConfirmationAction,
    CopyState,
    DashboardDTO,
    PermissionsState,
    StatusDTO,
    StatusReason,
    StatusState,
    SystemState,
    UserContext,
    UserRole,
)
from copytradeaster.config import Settings
from copytradeaster.telegram.menu import MenuAction, MenuCallback
from copytradeaster.telegram.middleware import BoundedTaskTracker
from copytradeaster.telegram.runtime import create_dispatcher


class ScaleBotSession(BaseSession):
    """Real BaseSession double that preserves sent/edit/answer ownership facts."""

    def __init__(self, *, expected_busy: int | None = None) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self.messages: dict[int, str] = {}
        self.next_message_id = 100
        self.close_count = 0
        self.sent_by_chat: dict[int, list[int]] = {}
        self.text_by_message: dict[int, str] = {}
        self.answers: list[str] = []
        self.busy_count = 0
        self._expected_busy = expected_busy
        self.busy_reached = asyncio.Event()

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        del timeout
        self.calls.append(method)
        name = type(method).__name__
        if name == "SendMessage":
            self.next_message_id += 1
            message_id = self.next_message_id
            self.messages[message_id] = method.text
            result = Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                    "reply_markup": method.reply_markup,
                },
                context={"bot": bot},
            )
            self.sent_by_chat.setdefault(method.chat_id, []).append(message_id)
            self.text_by_message[message_id] = method.text
            if method.text == "Сервис занят, повторите чуть позже.":
                self.busy_count += 1
                if self._expected_busy is not None and self.busy_count >= self._expected_busy:
                    self.busy_reached.set()
        elif name == "EditMessageText":
            if method.message_id is None:
                raise TelegramBadRequest(method=method, message="message id is required")
            self.messages[method.message_id] = method.text
            self.text_by_message[method.message_id] = method.text
            result = Message.model_validate(
                {
                    "message_id": method.message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id or 42, "type": "private"},
                    "text": method.text,
                    "reply_markup": method.reply_markup,
                },
                context={"bot": bot},
            )
        elif name == "AnswerCallbackQuery":
            self.answers.append(method.callback_query_id)
            result = True
        elif name == "GetWebhookInfo":
            result = WebhookInfo(
                url="",
                has_custom_certificate=False,
                pending_update_count=0,
            )
        else:
            result = True
        return result

    async def close(self) -> None:
        self.close_count += 1

    async def stream_content(self, *args: Any, **kwargs: Any):
        del args, kwargs
        yield b""


class ScaleApplication:
    """Per-user application double with real async interleaving points."""

    def __init__(
        self,
        user_ids: set[int],
        *,
        dashboard_gate: int | None = None,
        status_gate: int | None = None,
    ) -> None:
        self.user_ids = user_ids
        self.copy_states = {user_id: CopyState.ACTIVE for user_id in user_ids}
        self.authorized: list[int] = []
        self.dashboard_calls: list[int] = []
        self.status_calls: list[int] = []
        self.confirmation_calls: list[int] = []
        self.confirmed: list[tuple[int, str | None]] = []
        self.wrong_confirmation_binding = False
        self.dashboard_gate = dashboard_gate
        self.dashboard_started = asyncio.Event()
        self.release_dashboard = asyncio.Event()
        self._dashboard_active = 0
        self.peak_dashboard_active = 0
        self.status_gate = status_gate
        self.status_started = asyncio.Event()
        self.release_status = asyncio.Event()
        self._status_active = 0
        self.peak_status_active = 0

    async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext:
        await asyncio.sleep(0)
        self.authorized.append(user_id)
        return UserContext(
            user_id=user_id,
            chat_id=chat_id,
            role=UserRole.USER,
            allowed=user_id in self.user_ids and chat_type == "private",
        )

    async def dashboard(self, user_id: int) -> DashboardDTO:
        self.dashboard_calls.append(user_id)
        self._dashboard_active += 1
        self.peak_dashboard_active = max(self.peak_dashboard_active, self._dashboard_active)
        if self.dashboard_gate is not None and self._dashboard_active >= self.dashboard_gate:
            self.dashboard_started.set()
        try:
            if self.dashboard_gate is None:
                await asyncio.sleep(0)
            else:
                await self.release_dashboard.wait()
            return DashboardDTO(
                copy_state=self.copy_states[user_id],
                system_state=SystemState.READY,
                account_id=f"acct-{user_id}",
                account_masked=f"acct-{user_id}",
                signer_masked=f"signer-{user_id}",
                risk_percent=Decimal("0.01"),
                max_leverage=3,
                open_positions=0,
            )
        finally:
            self._dashboard_active -= 1

    async def begin_confirmation(
        self,
        user_id: int,
        action: ConfirmationAction,
        account_id: str | None = None,
    ) -> Confirmation:
        await asyncio.sleep(0)
        self.confirmation_calls.append(user_id)
        return Confirmation(
            token=f"confirm-{user_id}",
            action=action,
            account_id=account_id or f"acct-{user_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )

    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO:
        del refresh
        self.status_calls.append(user_id)
        self._status_active += 1
        self.peak_status_active = max(self.peak_status_active, self._status_active)
        if self.status_gate is not None and self._status_active >= self.status_gate:
            self.status_started.set()
        try:
            if self.status_gate is None:
                await asyncio.sleep(0)
            else:
                await self.release_status.wait()
            return StatusDTO(
                state=StatusState.READY,
                system_state=SystemState.READY,
                copy_state=self.copy_states[user_id],
                aster_state=AsterState.OK,
                permissions_state=PermissionsState.VALID,
                account_masked=f"acct-{user_id}",
                reason=StatusReason.NONE,
                remediation=None,
            )
        finally:
            self._status_active -= 1

    async def confirm_action(self, user_id: int, confirmation: Confirmation) -> DashboardDTO:
        await asyncio.sleep(0)
        if confirmation.account_id != f"acct-{user_id}":
            self.wrong_confirmation_binding = True
        self.confirmed.append((user_id, confirmation.account_id))
        self.copy_states[user_id] = CopyState.PAUSED
        return await self.dashboard(user_id)


def _settings(*, limit: int) -> Settings:
    return Settings(
        _env_file=None,
        encryption_key="ab" * 32,
        telegram_bot_token="123456:dummy",
        telegram_tasks_concurrency_limit=limit,
    )


def _chat_id(user_id: int) -> int:
    return 100_000 + user_id


def _message_update(update_id: int, user_id: int, text: str) -> dict[str, Any]:
    chat_id = _chat_id(user_id)
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": f"User {user_id}"},
            "text": text,
        },
    }


def _callback_update(
    update_id: int,
    user_id: int,
    message_id: int,
    data: str,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": f"User {user_id}"},
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": _chat_id(user_id), "type": "private"},
                "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                "text": "Aster Copy",
            },
            "chat_instance": f"chat-{user_id}",
            "data": data,
        },
    }


def _storage_key(bot: Bot, user_id: int) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=_chat_id(user_id), user_id=user_id)


@pytest.mark.asyncio
async def test_thirty_users_have_isolated_menu_fsm_confirmations_and_effects() -> None:
    user_ids = set(range(1_000, 1_030))
    application = ScaleApplication(user_ids)
    session = ScaleBotSession()
    bot = Bot("123456:dummy", session=session)
    tracker = BoundedTaskTracker(limit=64)
    dispatcher = create_dispatcher(
        _settings(limit=64),
        application,
        task_tracker=tracker,
    )

    await asyncio.gather(
        *(
            dispatcher.feed_raw_update(bot, _message_update(10_000 + user_id, user_id, "/start"))
            for user_id in user_ids
        )
    )
    assert application.peak_dashboard_active > 1
    anchors = {user_id: session.sent_by_chat[_chat_id(user_id)][0] for user_id in user_ids}

    request_updates = [
        _callback_update(
            20_000 + user_id,
            user_id,
            anchors[user_id],
            MenuCallback(action=MenuAction.DISCONNECT, revision=0).pack(),
        )
        for user_id in user_ids
    ]
    await asyncio.gather(*(dispatcher.feed_raw_update(bot, update) for update in request_updates))

    confirm_updates = [
        _callback_update(
            30_000 + user_id,
            user_id,
            anchors[user_id],
            MenuCallback(
                action=MenuAction.CONFIRM,
                key=ConfirmationAction.DISCONNECT.value,
                revision=1,
                token=f"confirm-{user_id}",
            ).pack(),
        )
        for user_id in user_ids
    ]
    await asyncio.gather(*(dispatcher.feed_raw_update(bot, update) for update in confirm_updates))

    expected_answer_ids = {f"query-{20_000 + user_id}" for user_id in user_ids} | {
        f"query-{30_000 + user_id}" for user_id in user_ids
    }
    assert set(session.answers) == expected_answer_ids
    assert len(session.answers) == len(expected_answer_ids)
    assert len(application.confirmed) == len(user_ids)
    assert set(application.confirmed) == {(user_id, f"acct-{user_id}") for user_id in user_ids}
    assert not application.wrong_confirmation_binding
    assert {user_id: application.dashboard_calls.count(user_id) for user_id in user_ids} == {
        user_id: 3 for user_id in user_ids
    }
    assert set(application.confirmation_calls) == user_ids

    menu = dispatcher.workflow_data["menu"]
    for user_id in user_ids:
        anchor = anchors[user_id]
        session_state = await menu.sessions.get(user_id, _chat_id(user_id), anchor)
        assert session_state is not None
        assert session_state.user_id == user_id
        assert session_state.chat_id == _chat_id(user_id)
        assert session_state.message_id == anchor
        assert session_state.revision == 2
        assert f"Счёт: acct-{user_id}" in session.text_by_message[anchor]
        assert await dispatcher.storage.get_state(_storage_key(bot, user_id)) is None
        assert await dispatcher.storage.get_data(_storage_key(bot, user_id)) == {}

    confirmations = dispatcher.workflow_data["confirmations"]
    await confirmations.purge()
    assert confirmations._items == {}
    assert tracker.active_count == 0


@pytest.mark.asyncio
async def test_thirty_users_route_fresh_inline_actions_while_handlers_are_in_flight() -> None:
    """Exercise real dispatcher ingress with one live callback per private user."""
    user_ids = set(range(1_100, 1_130))
    application = ScaleApplication(
        user_ids,
        dashboard_gate=len(user_ids),
        status_gate=len(user_ids),
    )
    session = ScaleBotSession()
    bot = Bot("123456:dummy", session=session)
    tracker = BoundedTaskTracker(limit=64)
    dispatcher = create_dispatcher(_settings(limit=64), application, task_tracker=tracker)

    async def open_then_route(user_id: int) -> None:
        await dispatcher.feed_raw_update(
            bot,
            _message_update(50_000 + user_id, user_id, "/start"),
        )
        anchor = session.sent_by_chat[_chat_id(user_id)][0]
        await dispatcher.feed_raw_update(
            bot,
            _callback_update(
                60_000 + user_id,
                user_id,
                anchor,
                MenuCallback(action=MenuAction.STATUS, revision=0).pack(),
            ),
        )

    action_tasks = [asyncio.create_task(open_then_route(user_id)) for user_id in user_ids]

    await asyncio.wait_for(application.dashboard_started.wait(), timeout=1)
    assert application.peak_dashboard_active == len(user_ids)
    application.release_dashboard.set()
    await asyncio.wait_for(application.status_started.wait(), timeout=1)
    assert application.peak_status_active == len(user_ids)
    assert tracker.active_count == len(user_ids)
    expected_answer_ids = {f"query-{60_000 + user_id}" for user_id in user_ids}
    # CallbackAnswerMiddleware must stop every Telegram spinner before the
    # requester-specific application call is released.
    assert set(session.answers) == expected_answer_ids
    assert len(session.answers) == len(expected_answer_ids)

    application.release_status.set()
    await asyncio.gather(*action_tasks)

    anchors = {user_id: session.sent_by_chat[_chat_id(user_id)][0] for user_id in user_ids}
    assert set(application.status_calls) == user_ids
    assert len(application.status_calls) == len(user_ids)
    menu = dispatcher.workflow_data["menu"]
    for user_id in user_ids:
        anchor = anchors[user_id]
        state = await menu.sessions.get(user_id, _chat_id(user_id), anchor)
        assert state is not None
        assert (state.user_id, state.chat_id, state.message_id, state.revision) == (
            user_id,
            _chat_id(user_id),
            anchor,
            1,
        )
        assert session.text_by_message[anchor].startswith("Статус")
        storage_key = _storage_key(bot, user_id)
        assert await dispatcher.storage.get_state(storage_key) is None
        assert await dispatcher.storage.get_data(storage_key) == {}

    assert tracker.active_count == 0
    await dispatcher.emit_shutdown()


@pytest.mark.asyncio
async def test_hundred_user_burst_has_deterministic_backpressure_and_drains() -> None:
    user_ids = set(range(2_000, 2_100))
    limit = 8
    application = ScaleApplication(user_ids, dashboard_gate=limit)
    session = ScaleBotSession(expected_busy=len(user_ids) - limit)
    bot = Bot("123456:dummy", session=session)
    tracker = BoundedTaskTracker(limit=limit)
    dispatcher = create_dispatcher(_settings(limit=limit), application, task_tracker=tracker)

    burst = [
        asyncio.create_task(
            dispatcher.feed_raw_update(bot, _message_update(40_000 + user_id, user_id, "/start"))
        )
        for user_id in user_ids
    ]
    await asyncio.wait_for(application.dashboard_started.wait(), timeout=1)
    await asyncio.wait_for(session.busy_reached.wait(), timeout=1)
    assert tracker.active_count == limit

    application.release_dashboard.set()
    await asyncio.gather(*burst)
    await tracker.drain(timeout=0.2)

    success_users = set(application.dashboard_calls)
    assert len(success_users) == limit
    assert session.busy_count == len(user_ids) - limit
    assert tracker.active_count == 0
    assert all(user_id in user_ids for user_id in application.authorized)
    assert set(application.authorized) == success_users

    for user_id in user_ids:
        sent = session.sent_by_chat[_chat_id(user_id)]
        assert len(sent) == 1
        text = session.text_by_message[sent[0]]
        if user_id in success_users:
            assert text.startswith("Aster Copy")
            assert f"Счёт: acct-{user_id}" in text
        else:
            assert text == "Сервис занят, повторите чуть позже."
        assert await dispatcher.storage.get_state(_storage_key(bot, user_id)) is None
        assert await dispatcher.storage.get_data(_storage_key(bot, user_id)) == {}

    confirmations = dispatcher.workflow_data["confirmations"]
    await confirmations.purge()
    assert confirmations._items == {}
    tracker.close_admission()
    await tracker.drain(timeout=0.2)
    assert not tracker.accepting
    assert tracker.active_count == 0
    await dispatcher.emit_shutdown()

    # A process restart gets a fresh admission gate and must not inherit the
    # closed tracker or any per-user FSM/session state from the drained one.
    restart_user = 3_000
    restart_application = ScaleApplication({restart_user})
    restart_session = ScaleBotSession()
    restart_bot = Bot("123456:dummy", session=restart_session)
    restart_tracker = BoundedTaskTracker(limit=limit)
    restart_dispatcher = create_dispatcher(
        _settings(limit=limit),
        restart_application,
        task_tracker=restart_tracker,
    )
    await restart_dispatcher.feed_raw_update(
        restart_bot,
        _message_update(70_000, restart_user, "/start"),
    )
    assert restart_application.dashboard_calls == [restart_user]
    assert restart_session.text_by_message
    assert restart_tracker.active_count == 0
    restart_tracker.close_admission()
    await restart_tracker.drain(timeout=0.2)
    await restart_dispatcher.emit_shutdown()
    await restart_bot.session.close()
