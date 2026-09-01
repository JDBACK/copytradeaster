from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import AiogramError, TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, ReplyKeyboardMarkup, WebhookInfo

from copytradeaster.application.models import (
    AccessRequestDTO,
    AccessRequestStatus,
    ActivityPage,
    AdminAccessRequestPage,
    AdminDashboardDTO,
    AdminFollowerItem,
    AdminFollowerPage,
    AdminUserItem,
    AdminUserPage,
    ApplicationError,
    Confirmation,
    ConfirmationAction,
    CopyState,
    DashboardDTO,
    DirectCredentialsInput,
    NotificationSettingsDTO,
    OnboardingApplicationError,
    OnboardingResult,
    PermissionSummary,
    RiskDTO,
    RiskUpdateInput,
    ServiceUnavailableError,
    StatusDTO,
    SystemState,
    UserContext,
    UserRole,
)
from copytradeaster.config import Settings
from copytradeaster.onboarding.errors import OnboardingErrorCode
from copytradeaster.telegram.access import (
    AccessDecisionAction,
    AccessDecisionCallback,
    GuestAccessAction,
    GuestAccessCallback,
)
from copytradeaster.telegram.flow import SingleUseConfirmationStore
from copytradeaster.telegram.menu import (
    MenuAction,
    MenuCallback,
    callback_bytes,
    follower_selection_key,
)
from copytradeaster.telegram.routers.settings import _parse_risk_value
from copytradeaster.telegram.runtime import (
    BoundedTaskTracker,
    TelegramRuntime,
    create_dispatcher,
    ensure_webhook_clean,
    register_commands,
)


class StubSession(BaseSession):
    """Small real-BaseSession Bot API double used through feed_raw_update."""

    def __init__(
        self,
        *,
        webhook_url: str = "",
        delete_fails: bool = False,
        edit_failure: str | None = None,
        send_failure_chat_ids: set[int] | None = None,
    ) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self.messages: dict[int, str] = {}
        self.next_message_id = 100
        self.webhook_url = webhook_url
        self.close_count = 0
        self.delete_fails = delete_fails
        self.edit_failure = edit_failure
        self.send_failure_chat_ids = send_failure_chat_ids or set()

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        del timeout
        self.calls.append(method)
        name = type(method).__name__
        if name == "SendMessage":
            if method.chat_id in self.send_failure_chat_ids:
                raise RuntimeError("simulated send failure")
            self.next_message_id += 1
            message_id = self.next_message_id
            self.messages[message_id] = method.text
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                    "reply_markup": method.reply_markup,
                },
                context={"bot": bot},
            )
        if name == "EditMessageText":
            if self.edit_failure is not None:
                raise TelegramBadRequest(method=method, message=self.edit_failure)
            message_id = method.message_id or self.next_message_id
            self.messages[message_id] = method.text
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id or 42, "type": "private"},
                    "text": method.text,
                    "reply_markup": method.reply_markup,
                },
                context={"bot": bot},
            )
        if name == "GetWebhookInfo":
            return WebhookInfo(
                url=self.webhook_url,
                has_custom_certificate=False,
                pending_update_count=0,
            )
        if name == "DeleteWebhook":
            self.webhook_url = ""
        if name == "DeleteMessage" and self.delete_fails:
            raise TelegramBadRequest(method=method, message="delete failed")
        return True

    async def close(self) -> None:
        self.close_count += 1

    async def stream_content(self, *args: Any, **kwargs: Any):
        del args, kwargs
        yield b""


class FakeApplication:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.copy_state = CopyState.EMPTY
        self.notifications = True
        self.connected: list[DirectCredentialsInput] = []
        self.confirmed: list[Confirmation] = []
        self.updated_risk: list[RiskUpdateInput] = []
        self.pauses: list[int] = []
        self.replaced: list[DirectCredentialsInput] = []
        self.admin_calls: list[str] = []
        self.allowed_users: list[int] = []
        self.disabled_users: set[int] = set()
        self.admin_follower_items = [
            AdminFollowerItem(
                account_id="tg-11",
                owner_user_id=11,
                account_masked="0x1234…abcd",
                copy_state=CopyState.ACTIVE,
                risk_percent=Decimal("0.01"),
            )
        ]
        self.authorized: list[tuple[int, int, str]] = []
        self.status_refreshes: list[bool] = []
        self.access_request_value: AccessRequestDTO | None = None
        self.dashboard_calls: list[int] = []

    async def access_request(self, user_id: int) -> AccessRequestDTO | None:
        return self.access_request_value

    async def submit_access_request(
        self,
        user_id: int,
        chat_id: int,
        *,
        username: str | None,
        display_name: str | None,
    ) -> AccessRequestDTO:
        if self.access_request_value is None:
            self.access_request_value = AccessRequestDTO(
                request_id=1,
                user_id=user_id,
                chat_id=chat_id,
                status=AccessRequestStatus.PENDING,
                username=username,
                display_name=display_name,
                requested_at=datetime.now(UTC),
                created=True,
            )
        return self.access_request_value

    async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext:
        self.authorized.append((user_id, chat_id, chat_type))
        return UserContext(
            user_id=user_id,
            chat_id=chat_id,
            role=UserRole.USER,
            allowed=self.allowed,
        )

    async def dashboard(self, user_id: int) -> DashboardDTO:
        self.dashboard_calls.append(user_id)
        return DashboardDTO(
            copy_state=self.copy_state,
            system_state=SystemState.READY,
            account_id="tg-7" if self.copy_state is not CopyState.EMPTY else None,
            account_masked="0x1234…abcd" if self.copy_state is not CopyState.EMPTY else None,
            signer_masked="0xabcd…1234" if self.copy_state is not CopyState.EMPTY else None,
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            open_positions=0,
        )

    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO:
        self.status_refreshes.append(refresh)
        return StatusDTO(
            system_state=SystemState.READY,
            copy_state=self.copy_state,
            aster_connected=True,
            account_masked="0x1234…abcd",
            positions_count=0,
            permissions=PermissionSummary(can_perp_trade=True, whitelist_ok=True),
        )

    async def risk(self, user_id: int) -> RiskDTO:
        return RiskDTO(
            Decimal("0.01"),
            3,
            Decimal("0.02"),
        )

    async def activity(self, user_id: int, page: int = 0) -> ActivityPage:
        return ActivityPage(page=page)

    async def notification_settings(self, user_id: int) -> NotificationSettingsDTO:
        return NotificationSettingsDTO(enabled=self.notifications)

    async def set_notifications(self, user_id: int, enabled: bool) -> NotificationSettingsDTO:
        self.notifications = enabled
        return NotificationSettingsDTO(enabled=enabled)

    async def connect_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        self.connected.append(credentials)
        self.copy_state = CopyState.PAUSED
        return OnboardingResult("tg-7", "0x1234…abcd", "0xabcd…1234", paused=True)

    async def replace_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        self.replaced.append(credentials)
        self.copy_state = CopyState.PAUSED
        return OnboardingResult("tg-7", "0x1234…abcd", "0xabcd…1234", paused=True)

    async def update_risk(self, user_id: int, values: RiskUpdateInput) -> RiskDTO:
        self.updated_risk.append(values)
        return await self.risk(user_id)

    async def pause(self, user_id: int) -> DashboardDTO:
        self.pauses.append(user_id)
        self.copy_state = CopyState.PAUSED
        return await self.dashboard(user_id)

    async def begin_confirmation(
        self,
        user_id: int,
        action: ConfirmationAction,
        account_id: str | None = None,
    ) -> Confirmation:
        return Confirmation(
            token=f"token-{action.value}",
            action=action,
            account_id=account_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )

    async def resume(self, user_id: int, confirmation: Confirmation | None = None) -> DashboardDTO:
        self.copy_state = CopyState.ACTIVE
        return await self.dashboard(user_id)

    async def confirm_action(self, user_id: int, confirmation: Confirmation) -> DashboardDTO:
        self.confirmed.append(confirmation)
        if confirmation.action is ConfirmationAction.RESUME:
            self.copy_state = CopyState.ACTIVE
        return await self.dashboard(user_id)

    async def admin_dashboard(self, user_id: int):
        return AdminDashboardDTO(system_state=SystemState.READY)

    async def admin_users(self, user_id: int, page: int = 0) -> AdminUserPage:
        return AdminUserPage(
            items=tuple(
                AdminUserItem(
                    target,
                    account_connected=False,
                    access_enabled=target not in self.disabled_users,
                )
                for target in self.allowed_users
            ),
            page=page,
        )

    async def admin_allow_user(self, user_id: int, target_user_id: int) -> AdminUserPage:
        if target_user_id not in self.allowed_users:
            self.allowed_users.append(target_user_id)
        return await self.admin_users(user_id)

    async def admin_set_user_access(
        self, user_id: int, target_user_id: int, enabled: bool
    ) -> AdminUserPage:
        if enabled:
            self.disabled_users.discard(target_user_id)
        else:
            self.disabled_users.add(target_user_id)
        return await self.admin_users(user_id)

    async def admin_access_requests(self, user_id: int, page: int = 0) -> AdminAccessRequestPage:
        items = (
            (self.access_request_value,)
            if self.access_request_value is not None
            and self.access_request_value.status is AccessRequestStatus.PENDING
            else ()
        )
        return AdminAccessRequestPage(items=items, page=page)

    async def admin_decide_access(
        self, user_id: int, request_id: int, *, approve: bool
    ) -> AccessRequestDTO:
        assert self.access_request_value is not None
        current = self.access_request_value
        changed = current.status is AccessRequestStatus.PENDING
        if changed:
            self.access_request_value = AccessRequestDTO(
                request_id=current.request_id,
                user_id=current.user_id,
                chat_id=current.chat_id,
                status=(AccessRequestStatus.APPROVED if approve else AccessRequestStatus.REJECTED),
                username=current.username,
                display_name=current.display_name,
                requested_at=current.requested_at,
                decided_at=datetime.now(UTC),
                decision_changed=True,
            )
        else:
            self.access_request_value = AccessRequestDTO(
                request_id=current.request_id,
                user_id=current.user_id,
                chat_id=current.chat_id,
                status=current.status,
                username=current.username,
                display_name=current.display_name,
                requested_at=current.requested_at,
                decided_at=current.decided_at,
                decision_changed=False,
            )
        return self.access_request_value

    async def admin_followers(self, user_id: int, page: int = 0) -> AdminFollowerPage:
        return AdminFollowerPage(
            items=tuple(self.admin_follower_items),
            page=page,
        )

    async def admin_reconcile(
        self, user_id: int, account_id: str | None = None
    ) -> AdminDashboardDTO:
        self.admin_calls.append(f"reconcile:{account_id or 'all'}")
        return AdminDashboardDTO(system_state=SystemState.READY)

    async def admin_pause(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO:
        self.admin_calls.append("pause")
        return AdminDashboardDTO(system_state=SystemState.READY, global_pause=True)

    async def admin_resume(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO:
        self.admin_calls.append("resume")
        return AdminDashboardDTO(system_state=SystemState.READY, global_pause=False)

    async def admin_emergency(
        self, user_id: int, confirmation: Confirmation | None = None
    ) -> AdminDashboardDTO:
        self.admin_calls.append("emergency")
        return AdminDashboardDTO(system_state=SystemState.DEGRADED, global_emergency=True)


class BrokenApplication(FakeApplication):
    async def status(self, user_id: int, refresh: bool = False) -> StatusDTO:
        raise RuntimeError("opaque internal failure")


class AuthorizationUnavailableApplication(FakeApplication):
    async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext:
        del user_id, chat_id, chat_type
        raise ServiceUnavailableError("authorization backend unavailable")


class BlockingPauseApplication(FakeApplication):
    def __init__(self) -> None:
        super().__init__()
        self.pause_started = asyncio.Event()
        self.release_pause = asyncio.Event()

    async def pause(self, user_id: int) -> DashboardDTO:
        self.pauses.append(user_id)
        self.pause_started.set()
        await self.release_pause.wait()
        self.copy_state = CopyState.PAUSED
        return await self.dashboard(user_id)


class FailingConnectApplication(FakeApplication):
    async def connect_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        raise ApplicationError("private-key-value must never be exposed")


class TypedFailingConnectApplication(FakeApplication):
    def __init__(self, code: OnboardingErrorCode) -> None:
        super().__init__()
        self.code = code

    async def connect_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        del user_id, credentials
        raise OnboardingApplicationError(self.code)


class TypedFailingReplaceApplication(FakeApplication):
    def __init__(self, code: OnboardingErrorCode) -> None:
        super().__init__()
        self.code = code

    async def replace_direct(
        self, user_id: int, credentials: DirectCredentialsInput
    ) -> OnboardingResult:
        del user_id, credentials
        raise OnboardingApplicationError(self.code)


class AdminApplication(FakeApplication):
    async def authorize(self, user_id: int, chat_id: int, chat_type: str) -> UserContext:
        self.authorized.append((user_id, chat_id, chat_type))
        return UserContext(
            user_id=user_id,
            chat_id=chat_id,
            role=UserRole.ADMIN,
            allowed=True,
        )

    async def dashboard(self, user_id: int) -> DashboardDTO:
        dto = await super().dashboard(user_id)
        return DashboardDTO(
            copy_state=dto.copy_state,
            system_state=dto.system_state,
            account_id=dto.account_id,
            account_masked=dto.account_masked,
            signer_masked=dto.signer_masked,
            risk_percent=dto.risk_percent,
            max_leverage=dto.max_leverage,
            open_positions=dto.open_positions,
            is_admin=True,
        )


def settings(*, admin_ids: str = "[]") -> Settings:
    return Settings(
        _env_file=None,
        encryption_key="ab" * 32,
        telegram_bot_token="123456:dummy",
        telegram_admin_ids=admin_ids,
    )


def bot_update_user() -> dict[str, Any]:
    return {"id": 7, "is_bot": False, "first_name": "Test"}


def private_chat() -> dict[str, Any]:
    return {"id": 42, "type": "private"}


def message_update(
    update_id: int, message_id: int, text: str, *, chat: dict[str, Any] | None = None
):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 0,
            "chat": chat or private_chat(),
            "from": bot_update_user(),
            "text": text,
        },
    }


def callback_update(
    update_id: int,
    message_id: int,
    data: str,
    *,
    chat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}",
            "from": bot_update_user(),
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": chat or private_chat(),
                "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                "text": "old",
            },
            "chat_instance": "test-chat-instance",
            "data": data,
        },
    }


def packed_callback(
    action: MenuAction,
    *,
    revision: int = 0,
    key: str = "",
    page: int = 0,
    token: str = "",
) -> str:
    return MenuCallback(
        action=action,
        key=key,
        page=page,
        revision=revision,
        token=token,
    ).pack()


async def make_runtime(
    *,
    application: FakeApplication | None = None,
    settings_obj: Settings | None = None,
    api: StubSession | None = None,
):
    api = api or StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(
        settings_obj or settings(),
        application,
        allow_test_only_application=application is None,
    )
    return bot, dispatcher, api


def lifecycle_runtime(
    api: StubSession,
    dispatcher: Any,
    bot: Bot,
) -> TelegramRuntime:
    return TelegramRuntime(
        settings=settings(),
        bot=bot,
        dispatcher=dispatcher,
        tracker=BoundedTaskTracker(2),
    )


@pytest.mark.asyncio
async def test_feed_raw_update_start_and_only_commands() -> None:
    bot, dispatcher, api = await make_runtime()

    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, message_update(2, 2, "/help"))
    await dispatcher.feed_raw_update(bot, message_update(3, 3, "/menu"))
    await dispatcher.feed_raw_update(bot, message_update(4, 4, "/cancel"))

    send_calls = [call for call in api.calls if type(call).__name__ == "SendMessage"]
    assert len(send_calls) == 4
    assert all(call.reply_markup.inline_keyboard for call in send_calls)
    assert not any(
        type(call).__name__ == "SendMessage" and isinstance(call.reply_markup, ReplyKeyboardMarkup)
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_callback_fresh_render_answer_and_stale_revision() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))

    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:s::0:0:"))
    assert any(type(call).__name__ == "AnswerCallbackQuery" for call in api.calls)
    assert any(
        type(call).__name__ == "EditMessageText" and call.text.startswith("Статус")
        for call in api.calls
    )

    answer_count = len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"])
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:s::0:0:"))
    assert (
        len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"])
        == answer_count + 1
    )
    assert any(type(call).__name__ == "EditMessageReplyMarkup" for call in api.calls)
    assert any(
        type(call).__name__ == "EditMessageText" and call.text.startswith("Aster Copy")
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_duplicate_pause_callback_has_one_application_effect() -> None:
    app = FakeApplication()
    app.copy_state = CopyState.ACTIVE
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:pa::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:pa::0:0:"))

    assert app.pauses == [7]
    assert len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) == 2


@pytest.mark.asyncio
async def test_callback_answers_before_anchor_lock_and_mutates_once() -> None:
    app = BlockingPauseApplication()
    app.copy_state = CopyState.ACTIVE
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))

    first = asyncio.create_task(
        dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:pa::0:0:"))
    )
    await asyncio.wait_for(app.pause_started.wait(), timeout=1)
    second = asyncio.create_task(
        dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:pa::0:0:"))
    )
    try:
        for _ in range(100):
            if (
                len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"])
                == 2
            ):
                break
            await asyncio.sleep(0)
        assert (
            len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) == 2
        )
    finally:
        app.release_pause.set()
        await asyncio.gather(first, second)

    assert app.pauses == [7]
    assert len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) == 2


@pytest.mark.asyncio
async def test_deleted_anchor_falls_back_and_noop_edit_does_not_duplicate_message() -> None:
    app = FakeApplication()
    app.copy_state = CopyState.ACTIVE
    deleted_api = StubSession(edit_failure="message to edit not found")
    deleted_bot = Bot("123456:dummy", session=deleted_api)
    deleted_dispatcher = create_dispatcher(settings(), app)
    await deleted_dispatcher.feed_raw_update(deleted_bot, message_update(1, 1, "/start"))
    sends_before = len([call for call in deleted_api.calls if type(call).__name__ == "SendMessage"])
    await deleted_dispatcher.feed_raw_update(
        deleted_bot,
        callback_update(2, 101, "m:s::0:0:"),
    )
    sends_after = len([call for call in deleted_api.calls if type(call).__name__ == "SendMessage"])
    assert sends_after == sends_before + 1

    noop_api = StubSession(edit_failure="message is not modified")
    noop_bot = Bot("123456:dummy", session=noop_api)
    noop_dispatcher = create_dispatcher(settings(), FakeApplication())
    await noop_dispatcher.feed_raw_update(noop_bot, message_update(1, 1, "/start"))
    sends_before = len([call for call in noop_api.calls if type(call).__name__ == "SendMessage"])
    await noop_dispatcher.feed_raw_update(noop_bot, callback_update(2, 101, "m:s::0:0:"))
    sends_after = len([call for call in noop_api.calls if type(call).__name__ == "SendMessage"])
    assert sends_after == sends_before


@pytest.mark.asyncio
async def test_fsm_prompt_fallback_remembers_new_anchor_for_next_step() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:c::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:co::0:1:"))

    api.edit_failure = "message to edit not found"
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xuser"))

    key = StorageKey(bot_id=bot.id, chat_id=42, user_id=7)
    state_data = await dispatcher.storage.get_data(key=key)
    assert state_data["anchor_message_id"] == 103
    menu = dispatcher.workflow_data["menu"]
    session = await menu.sessions.get(7, 42, 103)
    assert session is not None and session.message_id == 103


@pytest.mark.asyncio
async def test_stale_account_callback_on_empty_scope_returns_safe_dashboard() -> None:
    bot, dispatcher, api = await make_runtime(application=FakeApplication())
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:ac::0:0:"))

    edit = [call for call in api.calls if type(call).__name__ == "EditMessageText"][-1]
    actions = {
        MenuCallback.unpack(button.callback_data).action
        for row in edit.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }
    assert MenuAction.CONNECT in actions
    assert "em" not in {action.value for action in actions}


@pytest.mark.asyncio
async def test_inline_callback_is_denied_and_answered_without_chat_facts() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": 1,
            "callback_query": {
                "id": "inline-query",
                "from": bot_update_user(),
                "chat_instance": "test-chat-instance",
                "inline_message_id": "inline-message",
                "data": "m:h::0:0:",
            },
        },
    )
    assert app.authorized == []
    assert len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) == 1


@pytest.mark.asyncio
async def test_cancel_clears_every_production_fsm_state() -> None:
    bot, dispatcher, api = await make_runtime(application=FakeApplication())
    key = StorageKey(
        bot_id=bot.id,
        chat_id=42,
        user_id=7,
    )
    from copytradeaster.telegram.flow import OnboardingFlow, RiskFlow

    states = [
        OnboardingFlow.consent,
        OnboardingFlow.user_address,
        OnboardingFlow.signer_address,
        OnboardingFlow.private_key,
        OnboardingFlow.checking,
        RiskFlow.value,
    ]
    for index, state in enumerate(states, start=1):
        await dispatcher.storage.set_state(key, state)
        await dispatcher.storage.set_data(key, {"started_at": 1, "secret": "must-not-survive"})
        await dispatcher.feed_raw_update(bot, message_update(index, index, "/cancel"))
        assert await dispatcher.storage.get_state(key) is None
        assert await dispatcher.storage.get_data(key) == {}
    assert not any("must-not-survive" in str(call) for call in api.calls)


@pytest.mark.asyncio
async def test_new_dispatcher_does_not_resume_old_onboarding_state() -> None:
    first_app = FakeApplication()
    first_bot, first_dispatcher, _ = await make_runtime(application=first_app)
    await first_dispatcher.feed_raw_update(first_bot, message_update(1, 1, "/start"))
    await first_dispatcher.feed_raw_update(first_bot, callback_update(2, 101, "m:c::0:0:"))
    await first_dispatcher.feed_raw_update(first_bot, callback_update(3, 101, "m:co::0:1:"))

    second_app = FakeApplication()
    second_bot, second_dispatcher, second_api = await make_runtime(application=second_app)
    await second_dispatcher.feed_raw_update(
        second_bot,
        message_update(4, 10, "0xshould-not-be-treated-as-user-address"),
    )
    assert second_app.connected == []
    assert any(
        "Используйте /menu" in call.text
        for call in second_api.calls
        if type(call).__name__ == "SendMessage"
    )


@pytest.mark.asyncio
async def test_group_and_denied_callbacks_are_answered_without_handler_access() -> None:
    app = FakeApplication(allowed=False)
    bot, dispatcher, api = await make_runtime(application=app)
    group = {"id": -100, "type": "supergroup"}

    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start", chat=group))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:h::0:0:", chat=group))

    assert not any(type(call).__name__ == "SendMessage" for call in api.calls)
    assert len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) == 1

    private_app = FakeApplication(allowed=False)
    private_bot, private_dispatcher, private_api = await make_runtime(application=private_app)
    await private_dispatcher.feed_raw_update(private_bot, message_update(3, 3, "/start"))
    assert any(
        type(call).__name__ == "SendMessage" and "короткая проверка" in call.text
        for call in private_api.calls
    )
    assert private_app.authorized == [(7, 42, "private")]


@pytest.mark.asyncio
async def test_authorization_backend_failure_blocks_guest_start_before_captcha() -> None:
    app = AuthorizationUnavailableApplication(allowed=False)
    bot, dispatcher, api = await make_runtime(application=app)

    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))

    assert app.access_request_value is None
    assert any(
        type(call).__name__ == "SendMessage" and "Доступ пока не открыт" in call.text
        for call in api.calls
    )
    assert not any(
        type(call).__name__ == "SendMessage" and "короткая проверка" in call.text
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_unknown_user_captcha_creates_request_and_notifies_admin() -> None:
    app = FakeApplication(allowed=False)
    bot, dispatcher, api = await make_runtime(
        application=app,
        settings_obj=settings(admin_ids="[100]"),
    )
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    captcha_message = [call for call in api.calls if type(call).__name__ == "SendMessage"][-1]
    equation = captcha_message.text.rsplit("Сколько будет ", 1)[1].removesuffix("= ?").strip()
    left, right = (int(value.strip()) for value in equation.split("+"))
    correct = left + right
    answer_data = next(
        button.callback_data
        for row in captcha_message.reply_markup.inline_keyboard
        for button in row
        if int(button.text) == correct
    )
    assert GuestAccessCallback.unpack(answer_data).choice == correct

    await dispatcher.feed_raw_update(bot, callback_update(2, 101, answer_data))

    assert app.access_request_value is not None
    assert app.access_request_value.status is AccessRequestStatus.PENDING
    assert any(
        type(call).__name__ == "SendMessage"
        and call.chat_id == 100
        and "Новая заявка на доступ" in call.text
        for call in api.calls
    )
    assert any(
        type(call).__name__ == "EditMessageText"
        and "Заявка отправлена" in call.text
        and "видна администратору" in call.text
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_access_request_survives_direct_admin_delivery_failure() -> None:
    app = FakeApplication(allowed=False)
    api = StubSession(send_failure_chat_ids={100})
    bot, dispatcher, api = await make_runtime(
        application=app,
        settings_obj=settings(admin_ids="[100]"),
        api=api,
    )
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    captcha_message = [call for call in api.calls if type(call).__name__ == "SendMessage"][-1]
    equation = captcha_message.text.rsplit("Сколько будет ", 1)[1].removesuffix("= ?").strip()
    left, right = (int(value.strip()) for value in equation.split("+"))
    correct = left + right
    answer_data = next(
        button.callback_data
        for row in captcha_message.reply_markup.inline_keyboard
        for button in row
        if int(button.text) == correct
    )

    await dispatcher.feed_raw_update(bot, callback_update(2, 101, answer_data))

    assert app.access_request_value is not None
    assert app.access_request_value.status is AccessRequestStatus.PENDING
    assert any(
        type(call).__name__ == "EditMessageText"
        and "Заявка отправлена" in call.text
        and "видна администратору" in call.text
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_duplicate_admin_decision_does_not_send_dashboard_twice() -> None:
    app = AdminApplication()
    app.access_request_value = AccessRequestDTO(
        request_id=7,
        user_id=8,
        chat_id=800,
        status=AccessRequestStatus.PENDING,
        requested_at=datetime.now(UTC),
    )
    bot, dispatcher, api = await make_runtime(
        application=app,
        settings_obj=settings(admin_ids="[7]"),
    )
    data = AccessDecisionCallback(
        action=AccessDecisionAction.APPROVE,
        request_id=7,
    ).pack()

    await dispatcher.feed_raw_update(bot, callback_update(1, 500, data))
    await dispatcher.feed_raw_update(bot, callback_update(2, 500, data))

    user_menus = [
        call for call in api.calls if type(call).__name__ == "SendMessage" and call.chat_id == 800
    ]
    assert len(user_menus) == 1
    retired = [
        call
        for call in api.calls
        if type(call).__name__ == "EditMessageText" and call.message_id == 500
    ]
    assert len(retired) == 2
    assert all(call.reply_markup is None for call in retired)


@pytest.mark.asyncio
async def test_unknown_user_forged_menu_callback_never_reaches_dashboard() -> None:
    app = FakeApplication(allowed=False)
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    before = list(app.dashboard_calls)

    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:s::0:0:"))

    assert app.dashboard_calls == before == []
    assert not any(
        type(call).__name__ == "EditMessageText" and "Статус" in call.text for call in api.calls
    )


@pytest.mark.asyncio
async def test_guest_status_reuses_captcha_and_retry_cooldown_is_visible() -> None:
    app = FakeApplication(allowed=False)
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    status = GuestAccessCallback(action=GuestAccessAction.STATUS).pack()
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, status))
    assert any(
        type(call).__name__ == "EditMessageText" and "короткая проверка" in call.text
        for call in api.calls
    )

    app.access_request_value = AccessRequestDTO(
        request_id=3,
        user_id=7,
        chat_id=42,
        status=AccessRequestStatus.REJECTED,
        decided_at=datetime.now(UTC),
    )
    retry = GuestAccessCallback(action=GuestAccessAction.RETRY).pack()
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, retry))
    assert any(
        type(call).__name__ == "EditMessageText" and "через минуту" in call.text
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_admin_rejection_notifies_user_and_delivery_failure_is_safe() -> None:
    app = AdminApplication()
    app.access_request_value = AccessRequestDTO(
        request_id=8,
        user_id=8,
        chat_id=800,
        status=AccessRequestStatus.PENDING,
        requested_at=datetime.now(UTC),
    )
    bot, dispatcher, api = await make_runtime(
        application=app,
        settings_obj=settings(admin_ids="[7]"),
    )
    reject = AccessDecisionCallback(
        action=AccessDecisionAction.REJECT,
        request_id=8,
    ).pack()
    await dispatcher.feed_raw_update(bot, callback_update(1, 500, reject))
    assert any(
        type(call).__name__ == "SendMessage"
        and call.chat_id == 800
        and "не подтверждён" in call.text
        for call in api.calls
    )

    approval = AdminApplication()
    approval.access_request_value = AccessRequestDTO(
        request_id=9,
        user_id=9,
        chat_id=900,
        status=AccessRequestStatus.PENDING,
        requested_at=datetime.now(UTC),
    )
    failing_api = StubSession(send_failure_chat_ids={900})
    failing_bot, failing_dispatcher, failing_api = await make_runtime(
        application=approval,
        settings_obj=settings(admin_ids="[7]"),
        api=failing_api,
    )
    approve = AccessDecisionCallback(
        action=AccessDecisionAction.APPROVE,
        request_id=9,
    ).pack()
    await failing_dispatcher.feed_raw_update(failing_bot, callback_update(1, 501, approve))
    assert approval.access_request_value.status is AccessRequestStatus.APPROVED
    assert any(
        type(call).__name__ == "EditMessageText" and "командой /start" in call.text
        for call in failing_api.calls
    )


@pytest.mark.asyncio
async def test_admin_approval_sends_first_dashboard_and_retires_card() -> None:
    app = AdminApplication()
    app.access_request_value = AccessRequestDTO(
        request_id=7,
        user_id=8,
        chat_id=800,
        status=AccessRequestStatus.PENDING,
        username="second_account",
        display_name="Second Account",
        requested_at=datetime.now(UTC),
    )
    bot, dispatcher, api = await make_runtime(
        application=app,
        settings_obj=settings(admin_ids="[7]"),
    )
    data = AccessDecisionCallback(
        action=AccessDecisionAction.APPROVE,
        request_id=7,
    ).pack()

    await dispatcher.feed_raw_update(bot, callback_update(1, 500, data))

    assert app.access_request_value.status is AccessRequestStatus.APPROVED
    assert any(
        type(call).__name__ == "SendMessage"
        and call.chat_id == 800
        and call.text.startswith("Aster Copy")
        for call in api.calls
    )
    assert any(
        type(call).__name__ == "EditMessageText"
        and call.text.startswith("✅ Доступ открыт")
        and call.reply_markup is None
        for call in api.calls
    )


@pytest.mark.asyncio
async def test_inaccessible_callback_is_denied_before_application_facts() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(
        bot,
        {
            "update_id": 1,
            "callback_query": {
                "id": "query-inaccessible",
                "from": bot_update_user(),
                "message": {
                    "message_id": 101,
                    "date": 0,
                    "chat": private_chat(),
                },
                "chat_instance": "test-chat-instance",
                "data": "m:h::0:0:",
            },
        },
    )

    assert app.authorized == []
    assert len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) == 1


@pytest.mark.asyncio
async def test_root_error_handler_is_neutral_and_does_not_echo_exception() -> None:
    bot, dispatcher, api = await make_runtime(application=BrokenApplication())
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:s::0:0:"))

    answers = [call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]
    assert answers
    assert all("opaque internal failure" not in str(call) for call in api.calls)


@pytest.mark.asyncio
async def test_onboarding_private_key_is_deleted_and_never_in_fsm_data() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:c::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:co::0:1:"))
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xuser"))
    await dispatcher.feed_raw_update(bot, message_update(5, 11, "0xsigner"))
    await dispatcher.feed_raw_update(bot, message_update(6, 12, "private-key-value"))

    assert app.connected and app.connected[0].signer_private_key == "private-key-value"
    assert any(type(call).__name__ == "DeleteMessage" for call in api.calls)
    state = await dispatcher.storage.get_data(
        key=StorageKey(
            bot_id=bot.id,
            chat_id=42,
            user_id=7,
        )
    )
    assert "signer_private_key" not in state
    assert (
        await dispatcher.storage.get_state(
            key=StorageKey(
                bot_id=bot.id,
                chat_id=42,
                user_id=7,
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_onboarding_deletes_all_inputs_and_uses_fresh_step_cards() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)

    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.CONNECT)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xordinary-wallet"))
    await dispatcher.feed_raw_update(bot, message_update(5, 11, "0xapi-wallet"))
    await dispatcher.feed_raw_update(bot, message_update(6, 12, "private-key-value"))

    deleted_ids = {call.message_id for call in api.calls if type(call).__name__ == "DeleteMessage"}
    assert {10, 11, 12, 101, 102, 103} <= deleted_ids

    step_messages = [
        call
        for call in api.calls
        if type(call).__name__ == "SendMessage"
        and call.chat_id == 42
        and call.text.startswith("Шаг ")
    ]
    assert [call.text.splitlines()[0] for call in step_messages] == [
        "Шаг 1 из 3",
        "Шаг 2 из 3",
        "Шаг 3 из 3",
    ]
    step3 = next(call.text for call in step_messages if call.text.startswith("Шаг 3 из 3"))
    assert "Бот удалит его сразу после получения, до обращения к Aster." in step3
    assert "После проверки" not in step3
    assert any(
        type(call).__name__ == "EditMessageText" and "Проверяем подключение…" in call.text
        for call in api.calls
    )
    outgoing = [
        getattr(call, "text", "") or ""
        for call in api.calls
        if type(call).__name__ in {"SendMessage", "EditMessageText"}
    ]
    assert all(value not in "\n".join(outgoing) for value in ("0xordinary-wallet", "0xapi-wallet"))
    assert "private-key-value" not in "\n".join(outgoing)
    key = StorageKey(bot_id=bot.id, chat_id=42, user_id=7)
    assert await dispatcher.storage.get_state(key=key) is None
    assert await dispatcher.storage.get_data(key=key) == {}


@pytest.mark.asyncio
async def test_onboarding_prompt_delete_failure_edits_without_duplicate_cards() -> None:
    app = FakeApplication()
    api = StubSession(delete_fails=True)
    bot, dispatcher, api = await make_runtime(application=app, api=api)

    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.CONNECT)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xordinary-wallet"))

    sends = [call for call in api.calls if type(call).__name__ == "SendMessage"]
    assert len(sends) == 1
    assert any(
        type(call).__name__ == "EditMessageText"
        and call.message_id == 101
        and "Шаг 1 из 3" in call.text
        for call in api.calls
    )
    assert any(
        type(call).__name__ == "EditMessageText"
        and call.message_id == 101
        and "Шаг 2 из 3" in call.text
        for call in api.calls
    )
    key = StorageKey(bot_id=bot.id, chat_id=42, user_id=7)
    assert (await dispatcher.storage.get_data(key=key))["anchor_message_id"] == 101


@pytest.mark.asyncio
async def test_onboarding_prompt_delete_clears_retired_anchor_before_send_failure() -> None:
    api = StubSession()
    bot, dispatcher, _ = await make_runtime(application=FakeApplication(), api=api)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.CONNECT)),
    )

    api.send_failure_chat_ids.add(42)
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )

    key = StorageKey(bot_id=bot.id, chat_id=42, user_id=7)
    data = await dispatcher.storage.get_data(key=key)
    assert "anchor_message_id" not in data
    assert "anchor_chat_id" not in data
    menu = dispatcher.workflow_data["menu"]
    assert await menu.sessions.get(7, 42, 101) is None


@pytest.mark.asyncio
async def test_onboarding_non_text_and_expired_inputs_are_deleted(caplog) -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.CONNECT)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )

    non_text = message_update(4, 20, "")
    non_text["message"].pop("text")
    await dispatcher.feed_raw_update(bot, non_text)
    assert any(
        type(call).__name__ == "DeleteMessage" and call.message_id == 20 for call in api.calls
    )

    key = StorageKey(bot_id=bot.id, chat_id=42, user_id=7)
    current = await dispatcher.storage.get_data(key=key)
    await dispatcher.storage.update_data(
        key=key,
        data={**current, "started_at": time.time() - 60 * 60},
    )
    calls_before_expired = len(api.calls)
    await dispatcher.feed_raw_update(bot, message_update(5, 21, "expired-input"))
    assert any(
        type(call).__name__ == "DeleteMessage" and call.message_id == 21 for call in api.calls
    )
    expired_calls = api.calls[calls_before_expired:]
    assert not any(type(call).__name__ == "SendMessage" for call in expired_calls)
    assert any(
        type(call).__name__ == "EditMessageText" and "Сессия подключения истекла" in call.text
        for call in expired_calls
    )
    assert await dispatcher.storage.get_state(key=key) is None
    assert await dispatcher.storage.get_data(key=key) == {}
    assert all("expired-input" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_onboarding_failure_clears_state_and_requires_new_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FailingConnectApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:c::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:co::0:1:"))
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xuser"))
    await dispatcher.feed_raw_update(bot, message_update(5, 11, "0xsigner"))
    await dispatcher.feed_raw_update(bot, message_update(6, 12, "private-key-value"))

    key = StorageKey(
        bot_id=bot.id,
        chat_id=42,
        user_id=7,
    )
    assert await dispatcher.storage.get_state(key=key) is None
    assert all("private-key-value" not in str(call) for call in api.calls)
    assert all("private-key-value" not in record.getMessage() for record in caplog.records)
    assert any(
        type(call).__name__ == "EditMessageText"
        and "🔄 Попробовать снова" in str(call.reply_markup)
        and "⌂ На главную" in str(call.reply_markup)
        for call in api.calls
    )


@pytest.mark.parametrize(
    "code",
    [
        OnboardingErrorCode.INVALID_INPUT,
        OnboardingErrorCode.AGENT_NOT_FOUND,
        OnboardingErrorCode.AGENT_READ_PERMISSION,
        OnboardingErrorCode.COMMIT_FAILED,
        OnboardingErrorCode.ACTIVATION_FAILED,
    ],
)
@pytest.mark.asyncio
async def test_typed_onboarding_failure_renders_safe_recovery_without_secret(
    code: OnboardingErrorCode,
) -> None:
    app = TypedFailingConnectApplication(code)
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:c::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:co::0:1:"))
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xuser"))
    await dispatcher.feed_raw_update(bot, message_update(5, 11, "0xsigner"))
    await dispatcher.feed_raw_update(bot, message_update(6, 12, "private-key-value"))

    error_view = next(
        call
        for call in reversed(api.calls)
        if type(call).__name__ == "EditMessageText"
        and "🔄 Попробовать снова" in str(call.reply_markup)
    )
    assert "⌂ На главную" in str(error_view.reply_markup)
    assert code.value not in error_view.text
    assert "private-key-value" not in error_view.text
    assert "0xuser" not in error_view.text
    assert "0xsigner" not in error_view.text
    assert any(type(call).__name__ == "DeleteMessage" for call in api.calls)
    assert (
        await dispatcher.storage.get_state(StorageKey(bot_id=bot.id, chat_id=42, user_id=7)) is None
    )


@pytest.mark.asyncio
async def test_typed_replacement_failure_keeps_replacement_retry_mode() -> None:
    app = TypedFailingReplaceApplication(OnboardingErrorCode.AGENT_READ_PERMISSION)
    app.copy_state = CopyState.PAUSED
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:ac::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:cp::0:1:"))
    await dispatcher.feed_raw_update(bot, callback_update(4, 101, "m:co::0:2:"))
    await dispatcher.feed_raw_update(bot, message_update(5, 10, "0xreplacement-user"))
    await dispatcher.feed_raw_update(bot, message_update(6, 11, "0xreplacement-signer"))
    await dispatcher.feed_raw_update(bot, message_update(7, 12, "replacement-private-key"))

    error_view = next(
        call
        for call in reversed(api.calls)
        if type(call).__name__ == "EditMessageText"
        and "🔄 Попробовать снова" in str(call.reply_markup)
    )
    retry = next(
        MenuCallback.unpack(button.callback_data)
        for row in error_view.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
        and MenuCallback.unpack(button.callback_data).action is MenuAction.ONBOARDING_RETRY
    )
    assert retry.key == "replace"
    assert "Замена подключения не выполнена" in error_view.text

    await dispatcher.feed_raw_update(
        bot,
        callback_update(8, error_view.message_id, retry.pack()),
    )
    assert any(
        type(call).__name__ == "SendMessage" and "Замена подключения к Aster" in call.text
        for call in api.calls
    )
    assert (
        await dispatcher.storage.get_state(StorageKey(bot_id=bot.id, chat_id=42, user_id=7))
        == "OnboardingFlow:consent"
    )


@pytest.mark.asyncio
async def test_secret_delete_failure_is_logged_without_secret_or_fsm_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = StubSession(delete_fails=True)
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication())
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:c::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:co::0:1:"))
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "0xuser"))
    await dispatcher.feed_raw_update(bot, message_update(5, 11, "0xsigner"))
    await dispatcher.feed_raw_update(bot, message_update(6, 12, "private-key-value"))

    assert any("deletion failed" in record.getMessage() for record in caplog.records)
    assert all("private-key-value" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_account_replace_uses_same_secret_safe_fsm_path() -> None:
    app = FakeApplication()
    app.copy_state = CopyState.PAUSED
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:ac::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:cp::0:1:"))
    await dispatcher.feed_raw_update(bot, callback_update(4, 101, "m:co::0:2:"))
    await dispatcher.feed_raw_update(bot, message_update(5, 10, "0xreplacement-user"))
    await dispatcher.feed_raw_update(bot, message_update(6, 11, "0xreplacement-signer"))
    await dispatcher.feed_raw_update(bot, message_update(7, 12, "replacement-private-key"))

    assert app.replaced and app.replaced[0].signer_private_key == "replacement-private-key"
    assert any(type(call).__name__ == "DeleteMessage" for call in api.calls)


@pytest.mark.asyncio
async def test_risk_inline_preset_calls_application_with_decimal_fraction() -> None:
    app = FakeApplication()
    app.copy_state = CopyState.PAUSED
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:r::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:rx:risk:1:1:"))

    assert app.updated_risk
    assert app.updated_risk[-1].risk_percent == Decimal("0.01")
    assert any(type(call).__name__ == "AnswerCallbackQuery" for call in api.calls)


@pytest.mark.asyncio
async def test_admin_actions_have_env_id_gate_and_confirmation_protocol_calls() -> None:
    app = AdminApplication()
    admin_settings = settings(admin_ids="[7]")
    bot, dispatcher, api = await make_runtime(application=app, settings_obj=admin_settings)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "m:ad::0:0:"))
    await dispatcher.feed_raw_update(bot, callback_update(3, 101, "m:ap::0:1:"))
    confirm_data = MenuCallback(
        action=MenuAction.CONFIRM,
        key=ConfirmationAction.GLOBAL_PAUSE.value,
        revision=2,
        token="token-global_pause",
    ).pack()
    await dispatcher.feed_raw_update(bot, callback_update(4, 101, confirm_data))
    await dispatcher.feed_raw_update(bot, callback_update(5, 101, "m:ar::0:3:"))

    assert app.admin_calls == ["pause", "reconcile:all"]
    assert len([call for call in api.calls if type(call).__name__ == "AnswerCallbackQuery"]) >= 4

    denied_app = FakeApplication()
    denied_bot, denied_dispatcher, denied_api = await make_runtime(application=denied_app)
    await denied_dispatcher.feed_raw_update(denied_bot, message_update(10, 1, "/start"))
    await denied_dispatcher.feed_raw_update(denied_bot, callback_update(11, 101, "m:ad::0:0:"))
    assert denied_app.admin_calls == []
    assert any(type(call).__name__ == "AnswerCallbackQuery" for call in denied_api.calls)


@pytest.mark.asyncio
async def test_admin_inline_allowlist_and_targeted_follower_reconcile() -> None:
    app = AdminApplication()
    admin_settings = settings(admin_ids="[7]")
    bot, dispatcher, api = await make_runtime(application=app, settings_obj=admin_settings)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.ADMIN)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.ADMIN_USERS, revision=1)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(4, 101, packed_callback(MenuAction.ADMIN_USER_ADD, revision=2)),
    )
    await dispatcher.feed_raw_update(bot, message_update(5, 10, "not-an-id"))
    assert app.allowed_users == []
    await dispatcher.feed_raw_update(bot, message_update(6, 11, "123456789"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            7,
            103,
            packed_callback(
                MenuAction.ADMIN_USER_ALLOW,
                key="123456789",
            ),
        ),
    )
    assert app.allowed_users == [123456789]

    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            8,
            103,
            packed_callback(
                MenuAction.ADMIN_USER_TOGGLE,
                revision=1,
                key="123456789",
            ),
        ),
    )
    assert app.disabled_users == {123456789}

    await dispatcher.feed_raw_update(
        bot,
        callback_update(9, 103, packed_callback(MenuAction.ADMIN, revision=2)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(10, 103, packed_callback(MenuAction.ADMIN_FOLLOWERS, revision=3)),
    )
    # The list changes after render.  The opaque stable key must still select
    # tg-11, not the new row that shifted into the old index.
    app.admin_follower_items.insert(
        0,
        AdminFollowerItem(
            account_id="tg-10",
            owner_user_id=10,
            account_masked="0xabcd…1234",
            copy_state=CopyState.PAUSED,
        ),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            11,
            103,
            packed_callback(
                MenuAction.ADMIN_FOLLOWER_RECONCILE,
                revision=4,
                key=follower_selection_key("tg-11"),
            ),
        ),
    )
    assert app.admin_calls == ["reconcile:tg-11"]

    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            12,
            103,
            packed_callback(
                MenuAction.ADMIN_FOLLOWER_RECONCILE,
                revision=5,
                key="99",
            ),
        ),
    )
    assert app.admin_calls == ["reconcile:tg-11"]
    assert any(
        "Нужен только положительный" in (getattr(call, "text", "") or "") for call in api.calls
    )


@pytest.mark.asyncio
async def test_malformed_callback_is_answered_and_buttons_retired() -> None:
    bot, dispatcher, api = await make_runtime()
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(bot, callback_update(2, 101, "not-a-valid-callback"))

    assert any(type(call).__name__ == "AnswerCallbackQuery" for call in api.calls)
    assert any(type(call).__name__ == "EditMessageText" for call in api.calls)


def test_callback_data_budget_and_no_scene_registration() -> None:
    callback = MenuCallback(action=MenuAction.CONFIRM, token="x" * 40, revision=1)
    assert callback_bytes(callback) <= 64
    with pytest.raises(ValueError):
        MenuCallback(action=MenuAction.CONFIRM, token="x" * 200).pack()

    source_root = Path(__file__).parents[3] / "src" / "copytradeaster" / "telegram"
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))
    assert "SceneRegistry" not in source
    assert "aiogram.fsm.scene" not in source
    assert "Scene(" not in source

    empty = MenuCallback(action=MenuAction.CONFIRM, revision=1, token="")
    prefix_bytes = len(empty.pack().encode("utf-8"))
    remaining = 64 - prefix_bytes
    exact_token = "é" * (remaining // 2) + ("x" if remaining % 2 else "")
    exact = MenuCallback(action=MenuAction.CONFIRM, revision=1, token=exact_token)
    assert len(exact.pack().encode("utf-8")) == 64
    with pytest.raises(ValueError):
        MenuCallback(action=MenuAction.CONFIRM, revision=1, token=f"{exact_token}é").pack()


def test_risk_input_is_percent_decimal_and_presets_are_not_float() -> None:
    assert _parse_risk_value("risk", "1").risk_percent == Decimal("0.01")
    assert _parse_risk_value("risk", "0.5").risk_percent == Decimal("0.005")
    assert _parse_risk_value("risk", "1%").risk_percent == Decimal("0.01")
    with pytest.raises(ValueError):
        _parse_risk_value("deviation", "2,5")
    with pytest.raises(ValueError):
        _parse_risk_value("leverage", "3")
    with pytest.raises(ValueError):
        _parse_risk_value("risk", "100")
    with pytest.raises(ValueError):
        _parse_risk_value("risk", "NaN")


@pytest.mark.asyncio
async def test_confirmation_store_is_ttl_and_single_use() -> None:
    store = SingleUseConfirmationStore(ttl_seconds=0.01)
    confirmation = Confirmation("once", ConfirmationAction.EMERGENCY, "tg-7")
    await store.put(confirmation)
    assert await store.consume("once") == confirmation
    assert await store.consume("once") is None

    expired = Confirmation(
        "expired",
        ConfirmationAction.DISCONNECT,
        "tg-7",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await store.put(expired)
    assert await store.consume("expired") is None

    bound = Confirmation("bound", ConfirmationAction.EMERGENCY, "tg-7")
    await store.put(bound, user_id=7, chat_id=42, message_id=101)
    assert await store.consume("bound", user_id=8, chat_id=42, message_id=101) is None
    assert await store.consume("bound", user_id=7, chat_id=42, message_id=101) == bound
    assert await store.consume("bound", user_id=7, chat_id=42, message_id=101) is None


@pytest.mark.asyncio
async def test_webhook_cleanup_rechecks_and_commands_set_menu_button() -> None:
    api = StubSession(webhook_url="https://example.invalid/hook")
    bot = Bot("123456:dummy", session=api)

    before, after = await ensure_webhook_clean(bot)
    await register_commands(bot)

    assert before.endswith("hook")
    assert after == ""
    assert type(api.calls[0]).__name__ == "GetWebhookInfo"
    assert any(type(call).__name__ == "DeleteWebhook" for call in api.calls)
    assert any(type(call).__name__ == "SetMyCommands" for call in api.calls)
    assert any(type(call).__name__ == "SetChatMenuButton" for call in api.calls)


@pytest.mark.asyncio
async def test_register_commands_installs_webapp_menu_button_when_configured() -> None:
    api = StubSession()
    bot = Bot("123456:dummy", session=api)

    await register_commands(bot, web_app_url="https://mini.example.org/miniapp/")

    menu_calls = [call for call in api.calls if type(call).__name__ == "SetChatMenuButton"]
    assert len(menu_calls) == 1
    button = menu_calls[0].menu_button
    assert type(button).__name__ == "MenuButtonWebApp"
    assert button.text == "ASTER COPY"
    assert button.web_app.url == "https://mini.example.org/miniapp/"
    commands = [call for call in api.calls if type(call).__name__ == "SetMyCommands"]
    assert len(commands) == 1


def test_production_runtime_and_dispatcher_fail_closed_without_application() -> None:
    with pytest.raises(ValueError, match="application protocol"):
        create_dispatcher(settings())
    with pytest.raises(ValueError, match="application protocol"):
        TelegramRuntime.build(settings(), None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_stop_event_awaits_async_stop_and_closes_session_once() -> None:
    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication(), allow_test_only_application=False)
    runtime = lifecycle_runtime(api, dispatcher, bot)
    started = asyncio.Event()
    release = asyncio.Event()

    async def polling(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        started.set()
        await release.wait()

    async def stop_polling() -> None:
        release.set()

    dispatcher.start_polling = polling  # type: ignore[method-assign]
    dispatcher.stop_polling = AsyncMock(side_effect=stop_polling)  # type: ignore[method-assign]
    stop_event = asyncio.Event()

    async def request_stop() -> None:
        await started.wait()
        stop_event.set()

    task = asyncio.create_task(runtime.run(stop_event))
    await request_stop()
    await task

    assert dispatcher.stop_polling.await_count == 1
    assert api.close_count == 1
    assert runtime._transport_closed
    assert runtime._session_closed


@pytest.mark.asyncio
async def test_runtime_polling_failure_sets_engine_stop_event_and_closes_once() -> None:
    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication())
    runtime = lifecycle_runtime(api, dispatcher, bot)
    stop_event = asyncio.Event()

    async def polling(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("polling conflict")

    dispatcher.start_polling = polling  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="polling conflict"):
        await runtime.run(stop_event)

    assert stop_event.is_set()
    assert api.close_count == 1
    assert runtime._transport_closed
    assert runtime._session_closed


@pytest.mark.asyncio
async def test_runtime_cancellation_cancels_and_awaits_polling_before_close() -> None:
    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication())
    runtime = lifecycle_runtime(api, dispatcher, bot)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def polling(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    dispatcher.start_polling = polling  # type: ignore[method-assign]
    task = asyncio.create_task(runtime.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert api.close_count == 1
    assert runtime._transport_closed
    assert runtime._session_closed


@pytest.mark.asyncio
async def test_runtime_shared_session_stays_open_until_worker_drain_then_closes_once() -> None:
    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication())
    runtime = lifecycle_runtime(api, dispatcher, bot)
    started = asyncio.Event()
    release = asyncio.Event()

    async def polling(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        started.set()
        await release.wait()

    async def stop_polling() -> None:
        release.set()

    dispatcher.start_polling = polling  # type: ignore[method-assign]
    dispatcher.stop_polling = AsyncMock(side_effect=stop_polling)  # type: ignore[method-assign]
    stop_event = asyncio.Event()

    task = asyncio.create_task(runtime.run(stop_event, close_session=False))
    await started.wait()
    stop_event.set()
    await task

    # The outbox worker still owns the shared session during its drain window.
    assert runtime._transport_closed
    assert not runtime._session_closed
    assert api.close_count == 0
    await asyncio.sleep(0)
    assert api.close_count == 0

    await runtime.close_session()
    await runtime.close_session()
    assert runtime._session_closed
    assert api.close_count == 1


@pytest.mark.asyncio
async def test_runtime_shutdown_is_concurrent_and_idempotent() -> None:
    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication())
    runtime = lifecycle_runtime(api, dispatcher, bot)

    await asyncio.gather(*(runtime.shutdown() for _ in range(8)))
    await asyncio.gather(
        runtime.shutdown(close_session=False),
        runtime.close_session(),
        runtime.close_session(),
    )

    assert not runtime.tracker.accepting
    assert runtime._transport_closed
    assert runtime._session_closed
    assert api.close_count == 1


@pytest.mark.asyncio
async def test_settings_status_refresh_risk_text_and_notification_toggle() -> None:
    app = FakeApplication()
    app.copy_state = CopyState.PAUSED
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))

    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.STATUS_REFRESH)),
    )
    assert app.status_refreshes == [True]

    # Risk text input is a real FSM transition, not a direct parser call.
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.RISK, revision=1)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            4,
            101,
            packed_callback(MenuAction.RISK_EDIT, revision=2, key="risk"),
        ),
    )
    await dispatcher.feed_raw_update(bot, message_update(5, 10, "1,5"))
    assert app.updated_risk[-1].risk_percent == Decimal("0.015")

    # The dashboard is a fresh anchor after a state message; use its new
    # message ID/revision for the notification screen and toggle.
    await dispatcher.feed_raw_update(
        bot,
        callback_update(6, 102, packed_callback(MenuAction.NOTIFICATIONS)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            7,
            102,
            packed_callback(MenuAction.NOTIFICATIONS_TOGGLE, revision=1),
        ),
    )
    assert app.notifications is False
    assert any(type(call).__name__ == "AnswerCallbackQuery" for call in api.calls)


@pytest.mark.asyncio
async def test_settings_invalid_risk_text_preset_and_status_application_errors() -> None:
    class BrokenSettingsApplication(FakeApplication):
        async def update_risk(self, user_id: int, values: RiskUpdateInput) -> RiskDTO:
            raise ApplicationError("risk unavailable")

        async def set_notifications(self, user_id: int, enabled: bool) -> NotificationSettingsDTO:
            raise ApplicationError("notifications unavailable")

        async def pause(self, user_id: int) -> DashboardDTO:
            raise ApplicationError("pause unavailable")

    app = BrokenSettingsApplication()
    app.copy_state = CopyState.ACTIVE
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))

    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.RISK)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            3,
            101,
            packed_callback(MenuAction.RISK_EDIT, revision=1, key="leverage"),
        ),
    )
    assert app.updated_risk == []
    assert any("Доступно только изменение риска" in (call.text or "") for call in api.calls)

    await dispatcher.feed_raw_update(
        bot,
        callback_update(5, 101, packed_callback(MenuAction.RISK, revision=2)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            6,
            101,
            packed_callback(MenuAction.RISK_PRESET, revision=3, key="wrong", page=1),
        ),
    )
    assert any(
        "Это значение риска недоступно." in (getattr(call, "text", "") or "") for call in api.calls
    )

    # Error branches of notifications and pause remain neutral to Telegram.
    await dispatcher.feed_raw_update(
        bot,
        callback_update(7, 101, packed_callback(MenuAction.NOTIFICATIONS, revision=4)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            8,
            101,
            packed_callback(MenuAction.NOTIFICATIONS_TOGGLE, revision=5),
        ),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(9, 101, packed_callback(MenuAction.HOME, revision=6)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(10, 101, packed_callback(MenuAction.PAUSE, revision=7)),
    )
    assert any(
        "Уведомления временно недоступны." in (getattr(call, "text", "") or "")
        for call in api.calls
    )
    assert any(
        "Не удалось поставить копирование на паузу." in (call.text or "") for call in api.calls
    )


@pytest.mark.asyncio
async def test_user_confirmation_success_and_admin_authorization_failures() -> None:
    app = FakeApplication()
    app.copy_state = CopyState.PAUSED
    bot, dispatcher, api = await make_runtime(application=app, settings_obj=settings())
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.RESUME)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(
            3,
            101,
            packed_callback(
                MenuAction.CONFIRM,
                revision=1,
                key=ConfirmationAction.RESUME.value,
                token="token-resume",
            ),
        ),
    )
    assert app.confirmed and app.confirmed[-1].action is ConfirmationAction.RESUME

    denied = FakeApplication()
    denied_bot, denied_dispatcher, denied_api = await make_runtime(
        application=denied,
        settings_obj=settings(admin_ids="[7]"),
    )
    await denied_dispatcher.feed_raw_update(denied_bot, message_update(10, 1, "/start"))
    await denied_dispatcher.feed_raw_update(
        denied_bot,
        callback_update(11, 101, packed_callback(MenuAction.ADMIN_PAUSE)),
    )
    confirmation_store = denied_dispatcher.workflow_data["confirmations"]
    await confirmation_store.put(
        Confirmation("admin-denied", ConfirmationAction.GLOBAL_PAUSE, None),
        user_id=7,
        chat_id=42,
        message_id=101,
    )
    await denied_dispatcher.feed_raw_update(
        denied_bot,
        callback_update(
            12,
            101,
            packed_callback(
                MenuAction.CONFIRM,
                revision=1,
                key=ConfirmationAction.GLOBAL_PAUSE.value,
                token="admin-denied",
            ),
        ),
    )
    assert denied.admin_calls == []
    assert any(
        "Операция не выполнена" in (getattr(call, "text", "") or "") for call in denied_api.calls
    )
    assert any(type(call).__name__ == "AnswerCallbackQuery" for call in api.calls)


@pytest.mark.asyncio
async def test_onboarding_expiry_invalid_inputs_and_missing_application() -> None:
    app = FakeApplication()
    bot, dispatcher, api = await make_runtime(application=app)
    await dispatcher.feed_raw_update(bot, message_update(1, 1, "/start"))
    await dispatcher.feed_raw_update(
        bot,
        callback_update(2, 101, packed_callback(MenuAction.CONNECT)),
    )
    await dispatcher.feed_raw_update(
        bot,
        callback_update(3, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )
    await dispatcher.feed_raw_update(bot, message_update(4, 10, "   "))
    assert any(
        "Адрес обычного тестового кошелька не распознан" in (getattr(call, "text", "") or "")
        for call in api.calls
    )
    await dispatcher.feed_raw_update(bot, message_update(5, 11, "0xuser"))
    await dispatcher.feed_raw_update(bot, message_update(6, 12, "   "))
    assert any(
        "API wallet address не распознан" in (getattr(call, "text", "") or "") for call in api.calls
    )
    await dispatcher.feed_raw_update(bot, message_update(7, 13, "0xsigner"))
    await dispatcher.feed_raw_update(bot, message_update(8, 14, "   "))
    assert any(
        "private key не распознан" in (getattr(call, "text", "") or "") for call in api.calls
    )

    # Expiry is checked at consent and input boundaries.
    expired_bot, expired_dispatcher, expired_api = await make_runtime(application=FakeApplication())
    await expired_dispatcher.feed_raw_update(expired_bot, message_update(20, 1, "/start"))
    await expired_dispatcher.feed_raw_update(
        expired_bot,
        callback_update(21, 101, packed_callback(MenuAction.CONNECT)),
    )
    expired_key = StorageKey(bot_id=expired_bot.id, chat_id=42, user_id=7)
    await expired_dispatcher.storage.update_data(
        key=expired_key,
        data={"started_at": time.time() - 60 * 60, "onboarding_mode": "connect"},
    )
    await expired_dispatcher.feed_raw_update(
        expired_bot,
        callback_update(22, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )
    assert await expired_dispatcher.storage.get_state(expired_key) is None
    assert any(type(call).__name__ == "EditMessageText" for call in expired_api.calls)

    # A process without the application composition fails closed after the
    # private key message and clears all FSM data.
    missing_bot, missing_dispatcher, missing_api = await make_runtime()
    await missing_dispatcher.feed_raw_update(missing_bot, message_update(30, 1, "/start"))
    await missing_dispatcher.feed_raw_update(
        missing_bot,
        callback_update(31, 101, packed_callback(MenuAction.CONNECT)),
    )
    await missing_dispatcher.feed_raw_update(
        missing_bot,
        callback_update(32, 101, packed_callback(MenuAction.CONSENT, revision=1)),
    )
    await missing_dispatcher.feed_raw_update(missing_bot, message_update(33, 10, "0xuser"))
    await missing_dispatcher.feed_raw_update(missing_bot, message_update(34, 11, "0xsigner"))
    await missing_dispatcher.feed_raw_update(
        missing_bot,
        message_update(35, 12, "private-key-value"),
    )
    assert all("private-key-value" not in str(call) for call in missing_api.calls)


@pytest.mark.asyncio
async def test_runtime_startup_metadata_error_and_private_message_error_boundary(
    monkeypatch,
) -> None:
    from copytradeaster.telegram import runtime as runtime_module

    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    dispatcher = create_dispatcher(settings(), FakeApplication())
    runtime = lifecycle_runtime(api, dispatcher, bot)
    monkeypatch.setattr(
        runtime_module,
        "register_commands",
        AsyncMock(side_effect=AiogramError("metadata rejected")),
    )
    await runtime.startup()
    assert api.close_count == 0
    await runtime.close_session()

    class BrokenDashboardApplication(FakeApplication):
        async def dashboard(self, user_id: int) -> DashboardDTO:
            raise ApplicationError("dashboard failed")

    error_bot, error_dispatcher, error_api = await make_runtime(
        application=BrokenDashboardApplication()
    )
    await error_dispatcher.feed_raw_update(error_bot, message_update(1, 1, "/start"))
    assert any(
        "Временная ошибка. Попробуйте /menu ещё раз." in (getattr(call, "text", "") or "")
        for call in error_api.calls
    )


@pytest.mark.asyncio
async def test_runtime_webhook_recheck_and_stop_polling_startup_race(monkeypatch) -> None:
    from copytradeaster.telegram import runtime as runtime_module

    api = StubSession(webhook_url="https://example.invalid/hook")
    bot = Bot("123456:dummy", session=api)

    async def sticky_webhook_info():
        return WebhookInfo(
            url="https://example.invalid/still-present",
            has_custom_certificate=False,
            pending_update_count=0,
        )

    bot.get_webhook_info = sticky_webhook_info  # type: ignore[method-assign]
    bot.delete_webhook = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="webhook remains"):
        await runtime_module.ensure_webhook_clean(bot)
    await bot.session.close()

    race_api = StubSession()
    race_bot = Bot("123456:dummy", session=race_api)
    race_dispatcher = create_dispatcher(settings(), FakeApplication())
    runtime = lifecycle_runtime(race_api, race_dispatcher, race_bot)
    started = asyncio.Event()

    async def polling(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        started.set()
        await asyncio.Event().wait()

    race_dispatcher.start_polling = polling  # type: ignore[method-assign]
    race_dispatcher.stop_polling = AsyncMock(side_effect=RuntimeError("startup race"))  # type: ignore[method-assign]
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop_event, close_session=False))
    await started.wait()
    stop_event.set()
    await task
    assert race_api.close_count == 0
    await runtime.close_session()
    assert race_api.close_count == 1


@pytest.mark.asyncio
async def test_runtime_build_override_and_startup_failure_cleanup(monkeypatch) -> None:
    override_settings = Settings(
        _env_file=None,
        encryption_key="ab" * 32,
        telegram_bot_token="123456:dummy",
        testnet_ip_whitelist_override=True,
    )
    dispatcher = create_dispatcher(override_settings, FakeApplication())
    built = TelegramRuntime.build(override_settings, FakeApplication())
    assert dispatcher is not None
    await built.close_session()

    api = StubSession()
    bot = Bot("123456:dummy", session=api)
    runtime = lifecycle_runtime(api, create_dispatcher(settings(), FakeApplication()), bot)
    monkeypatch.setattr(
        TelegramRuntime,
        "startup",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        await runtime.run()
    assert api.close_count == 1
