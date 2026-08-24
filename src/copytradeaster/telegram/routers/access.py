"""Guest captcha, durable access request, and administrator decision handlers."""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from copytradeaster.application.models import (
    AccessRequestStatus,
    ApplicationError,
    ApplicationProtocol,
    UserContext,
    UserRole,
)
from copytradeaster.config import Settings
from copytradeaster.telegram.access import (
    AccessDecisionAction,
    AccessDecisionCallback,
    CaptchaChallengeStore,
    CaptchaRateLimitedError,
    CaptchaResult,
    GuestAccessAction,
    GuestAccessCallback,
)
from copytradeaster.telegram.menu import (
    MenuEngine,
    Screen,
    access_decision_text,
    access_request_card,
    render_access_captcha,
    render_access_pending,
    render_access_rejected,
    render_access_wait,
)

RETRY_COOLDOWN_SECONDS = 60
logger = logging.getLogger(__name__)


def create_access_router(captcha: CaptchaChallengeStore) -> Router:
    router = Router(name="telegram_access")

    @router.callback_query(GuestAccessCallback.filter())
    async def guest_access_callback(
        query: CallbackQuery,
        callback_data: GuestAccessCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        message = query.message if isinstance(query.message, Message) else None
        if message is None:
            return
        if user_context.allowed:
            await menu.open(
                bot,
                user_id=user_context.user_id,
                chat_id=user_context.chat_id,
                view=await menu.dashboard(user_context.user_id),
                message=message,
                query=query,
            )
            return
        if application is None:
            await _open_access_wait(menu, bot, query, user_context)
            return

        if callback_data.action is GuestAccessAction.STATUS:
            await _show_current_request(
                application,
                captcha,
                menu,
                bot,
                query,
                user_context,
            )
            return

        if callback_data.action is GuestAccessAction.RETRY:
            try:
                current = await application.access_request(user_context.user_id)
                if current is not None and current.status is AccessRequestStatus.PENDING:
                    view = render_access_pending(current)
                elif (
                    current is not None
                    and current.decided_at is not None
                    and (datetime.now(UTC) - current.decided_at).total_seconds()
                    < RETRY_COOLDOWN_SECONDS
                ):
                    view = render_access_wait(
                        "Повторную заявку можно отправить через минуту. Затем снова нажмите /start."
                    )
                else:
                    challenge = await captcha.begin(
                        user_context.user_id,
                        user_context.chat_id,
                        force_new=True,
                    )
                    view = render_access_captcha(challenge)
            except (ApplicationError, CaptchaRateLimitedError):
                view = render_access_wait("Повторите /start через минуту.")
            await menu.open(
                bot,
                user_id=user_context.user_id,
                chat_id=user_context.chat_id,
                view=view,
                message=message,
                query=query,
            )
            return

        try:
            current = await application.access_request(user_context.user_id)
        except ApplicationError:
            current = None
        if current is not None and current.status is AccessRequestStatus.PENDING:
            await menu.open(
                bot,
                user_id=user_context.user_id,
                chat_id=user_context.chat_id,
                view=render_access_pending(current),
                message=message,
                query=query,
            )
            return

        result = await captcha.verify(
            user_context.user_id,
            user_context.chat_id,
            callback_data.token,
            callback_data.choice,
        )
        if result is CaptchaResult.BLOCKED:
            view = render_access_wait("Слишком много попыток. Повторите /start через минуту.")
        elif result in {CaptchaResult.WRONG, CaptchaResult.EXPIRED}:
            try:
                challenge = await captcha.begin(
                    user_context.user_id,
                    user_context.chat_id,
                    force_new=True,
                )
                notice = (
                    "Ответ не подошёл — вот новый пример."
                    if result is CaptchaResult.WRONG
                    else "Проверка устарела — вот новый пример."
                )
                view = render_access_captcha(challenge, notice=notice)
            except CaptchaRateLimitedError:
                view = render_access_wait("Слишком много попыток. Повторите /start через минуту.")
        else:
            try:
                request = await application.submit_access_request(
                    user_context.user_id,
                    user_context.chat_id,
                    username=query.from_user.username,
                    display_name=query.from_user.full_name,
                )
                view = render_access_pending(request)
                # ``created`` comes from the same SQLite transaction as the
                # pending-row uniqueness check.  A duplicate captcha callback
                # may still arrive outside aiogram's event-isolation window,
                # but it must not fan out duplicate approval cards.
                if request.created:
                    card_text, card_markup = access_request_card(request)
                    for admin_id in settings.telegram_admin_ids:
                        try:
                            await bot.send_message(
                                chat_id=admin_id,
                                text=card_text,
                                reply_markup=card_markup,
                                parse_mode=None,
                            )
                        except Exception as exc:
                            # The durable request remains visible in the
                            # admin fallback screen.  A Bot API/network
                            # failure must never roll it back or expose a
                            # transport exception to the user.
                            logger.warning(
                                "access request admin delivery failed (%s)",
                                type(exc).__name__,
                            )
            except ApplicationError:
                view = render_access_wait("Заявка не отправлена. Повторите /start чуть позже.")

        await menu.open(
            bot,
            user_id=user_context.user_id,
            chat_id=user_context.chat_id,
            view=view,
            message=message,
            query=query,
        )

    @router.callback_query(AccessDecisionCallback.filter())
    async def access_decision_callback(
        query: CallbackQuery,
        callback_data: AccessDecisionCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        if (
            application is None
            or user_context.role is not UserRole.ADMIN
            or user_context.user_id not in settings.telegram_admin_ids
        ):
            return
        try:
            decision = await application.admin_decide_access(
                user_context.user_id,
                callback_data.request_id,
                approve=callback_data.action is AccessDecisionAction.APPROVE,
            )
        except ApplicationError:
            return

        menu_sent: bool | None = None
        if decision.decision_changed and decision.status is AccessRequestStatus.APPROVED:
            try:
                await menu.open(
                    bot,
                    user_id=decision.user_id,
                    chat_id=decision.chat_id,
                    view=await menu.dashboard(decision.user_id),
                )
                menu_sent = True
            except Exception as exc:
                logger.warning(
                    "approved user menu delivery failed (%s)",
                    type(exc).__name__,
                )
                menu_sent = False
        elif decision.decision_changed and decision.status is AccessRequestStatus.REJECTED:
            try:
                await bot.send_message(
                    chat_id=decision.chat_id,
                    text=(
                        "Доступ пока не подтверждён. Если это ошибка, через минуту "
                        "снова откройте /start."
                    ),
                    parse_mode=None,
                )
            except Exception as exc:
                logger.warning(
                    "rejected user notification delivery failed (%s)",
                    type(exc).__name__,
                )

        message = query.message if isinstance(query.message, Message) else None
        if message is None:
            return
        session = await menu.sessions.get(
            user_context.user_id,
            message.chat.id,
            message.message_id,
        )
        if session is not None and session.screen is Screen.ADMIN_REQUESTS:
            view = await menu.admin_access_requests(
                user_context.user_id,
                revision=session.revision + 1,
            )
            await menu.open(
                bot,
                user_id=user_context.user_id,
                chat_id=user_context.chat_id,
                view=view,
                message=message,
                query=query,
            )
            return
        with contextlib.suppress(TelegramBadRequest):
            try:
                await message.edit_text(
                    access_decision_text(decision, menu_sent=menu_sent),
                    reply_markup=None,
                    parse_mode=None,
                )
            except Exception as exc:
                logger.warning(
                    "access decision card retirement failed (%s)",
                    type(exc).__name__,
                )

    return router


async def _show_current_request(
    application: ApplicationProtocol,
    captcha: CaptchaChallengeStore,
    menu: MenuEngine,
    bot: Bot,
    query: CallbackQuery,
    context: UserContext,
) -> None:
    message = query.message if isinstance(query.message, Message) else None
    if message is None:
        return
    try:
        current = await application.access_request(context.user_id)
        if current is not None and current.status is AccessRequestStatus.PENDING:
            view = render_access_pending(current)
        elif current is not None:
            view = render_access_rejected()
        else:
            challenge = await captcha.begin(context.user_id, context.chat_id)
            view = render_access_captcha(challenge)
    except (ApplicationError, CaptchaRateLimitedError):
        view = render_access_wait("Временная ошибка. Повторите /start чуть позже.")
    await menu.open(
        bot,
        user_id=context.user_id,
        chat_id=context.chat_id,
        view=view,
        message=message,
        query=query,
    )


async def _open_access_wait(
    menu: MenuEngine,
    bot: Bot,
    query: CallbackQuery,
    context: UserContext,
) -> None:
    message = query.message if isinstance(query.message, Message) else None
    if message is None:
        return
    await menu.open(
        bot,
        user_id=context.user_id,
        chat_id=context.chat_id,
        view=render_access_wait("Временная ошибка. Повторите /start чуть позже."),
        message=message,
        query=query,
    )


__all__ = ["create_access_router"]
