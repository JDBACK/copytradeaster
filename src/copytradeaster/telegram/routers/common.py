"""Commands and navigation callbacks shared by all Telegram users."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from copytradeaster.application.models import (
    AccessRequestStatus,
    ApplicationError,
    ApplicationProtocol,
    UserContext,
)
from copytradeaster.telegram.access import CaptchaChallengeStore, CaptchaRateLimitedError
from copytradeaster.telegram.menu import (
    MenuAction,
    MenuCallback,
    MenuEngine,
    MenuView,
    render_access_captcha,
    render_access_pending,
    render_access_rejected,
    render_access_wait,
    render_help,
    render_prompt,
)


def create_common_router() -> Router:
    router = Router(name="telegram_common")

    @router.message(Command("start"))
    async def start(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        access_captcha: CaptchaChallengeStore,
    ) -> None:
        await state.clear()
        if not user_context.allowed:
            if application is None:
                view = render_access_wait("Временная ошибка. Попробуйте /start чуть позже.")
            else:
                try:
                    request = await application.access_request(user_context.user_id)
                    if request is not None and request.status is AccessRequestStatus.PENDING:
                        view = render_access_pending(request)
                    elif request is not None:
                        view = render_access_rejected()
                    else:
                        challenge = await access_captcha.begin(
                            user_context.user_id,
                            user_context.chat_id,
                        )
                        view = render_access_captcha(challenge)
                except CaptchaRateLimitedError:
                    view = render_access_wait(
                        "Слишком много попыток. Повторите /start через минуту."
                    )
                except ApplicationError:
                    view = render_access_wait("Временная ошибка. Попробуйте /start чуть позже.")
            await _send_command_view(message, menu, bot, user_context, view)
            return
        await _send_command_view(
            message, menu, bot, user_context, await menu.dashboard(user_context.user_id)
        )

    @router.message(Command("menu"))
    async def menu_command(
        message: Message,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        await _send_command_view(
            message, menu, bot, user_context, await menu.dashboard(user_context.user_id)
        )

    @router.message(Command("help"))
    async def help_command(
        message: Message,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        await _send_command_view(message, menu, bot, user_context, render_help())

    @router.message(Command("cancel"))
    async def cancel_command(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        await state.clear()
        await _send_command_view(
            message, menu, bot, user_context, await menu.dashboard(user_context.user_id)
        )

    @router.callback_query(MenuCallback.filter(F.action.in_({MenuAction.HOME, MenuAction.BACK})))
    async def home_callback(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            view = await menu.dashboard(user_context.user_id, callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.HELP))
    async def help_callback(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if accepted:
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    render_help(callback_data.revision + 1),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.HOW))
    async def how_callback(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if accepted:
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    render_prompt(
                        "Как это работает\n\n"
                        "Копирование повторяет состояние счёта-источника. "
                        "Новые входы учитывают выбранный риск, а закрытия и защита "
                        "обрабатываются независимо от паузы.",
                        callback_data.revision + 1,
                    ),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.SECURITY))
    async def security_callback(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if accepted:
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    render_help(callback_data.revision + 1, security=True),
                )

    @router.message(StateFilter(None))
    async def neutral_message(message: Message) -> None:
        """Keep non-command private text from becoming a hidden API surface."""
        if message.chat.type == "private":
            with contextlib.suppress(AiogramError):
                await message.answer("Используйте /menu или кнопки меню.")

    return router


@asynccontextmanager
async def _callback_scope(
    query: CallbackQuery,
    callback_data: MenuCallback,
    menu: MenuEngine,
    context: UserContext,
) -> AsyncIterator[bool]:
    message = query.message
    if isinstance(message, InaccessibleMessage):
        yield False
        return
    if not isinstance(message, Message):
        # An inline callback has no private chat anchor.  Authorization
        # middleware normally rejects it; the explicit branch keeps direct
        # router tests and future inline presenters deterministic.
        yield bool(query.inline_message_id)
        return

    # Telegram's callback progress indicator must be stopped before waiting
    # behind another callback for the same anchor.  CallbackAnswerMiddleware
    # does this before FSM event isolation; the revision check below still
    # decides whether this callback may mutate anything.
    async with menu.sessions.serialized(context.user_id, message.chat.id, message.message_id):
        if not await menu.sessions.accept_revision(
            context.user_id,
            message.chat.id,
            message.message_id,
            callback_data.revision,
        ):
            await menu.stale(query, already_answered=True)
            current = await menu.sessions.get(
                context.user_id,
                message.chat.id,
                message.message_id,
            )
            revision = current.revision + 1 if current is not None else callback_data.revision + 1
            await menu.open(
                query.bot,
                user_id=context.user_id,
                chat_id=context.chat_id,
                view=await menu.dashboard(context.user_id, revision=revision),
                message=message,
                query=query,
            )
            yield False
            return
        yield True


async def _send_command_view(
    message: Message,
    menu: MenuEngine,
    bot: Bot,
    context: UserContext,
    view: MenuView,
) -> None:
    await menu.open(bot, user_id=context.user_id, chat_id=context.chat_id, view=view)


async def _open_callback_view(
    menu: MenuEngine,
    bot: Bot,
    query: CallbackQuery,
    context: UserContext,
    view: MenuView,
) -> None:
    message = query.message if isinstance(query.message, Message) else None
    await menu.open(
        bot,
        user_id=context.user_id,
        chat_id=context.chat_id,
        view=view,
        message=message,
        query=query,
    )
