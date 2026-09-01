"""Inline dashboard screens and state-changing callbacks."""

from __future__ import annotations

import contextlib
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from copytradeaster.application.models import (
    ApplicationError,
    ApplicationProtocol,
    ConfirmationAction,
    CopyState,
    DashboardDTO,
    RiskUpdateInput,
    UserContext,
    UserRole,
)
from copytradeaster.config import Settings
from copytradeaster.telegram.flow import AdminUserFlow, RiskFlow, SingleUseConfirmationStore
from copytradeaster.telegram.menu import (
    MenuAction,
    MenuCallback,
    MenuEngine,
    MenuView,
    follower_selection_key,
    render_account,
    render_admin_user_confirmation,
    render_confirmation,
    render_error,
    render_prompt,
)
from copytradeaster.telegram.routers.common import _callback_scope, _open_callback_view


def create_settings_router(
    confirmations: SingleUseConfirmationStore | None = None,
) -> Router:
    router = Router(name="telegram_settings")
    confirmation_store = confirmations or SingleUseConfirmationStore()

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.STATUS))
    async def status(
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
                    await menu.status(user_context.user_id, callback_data.revision + 1),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.STATUS_REFRESH))
    async def status_refresh(
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
                    await menu.status(
                        user_context.user_id,
                        callback_data.revision + 1,
                        refresh=True,
                    ),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.RISK))
    async def risk(
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
                    await menu.risk(user_context.user_id, callback_data.revision + 1),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.RISK_EDIT))
    async def risk_edit(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        state: FSMContext,
        user_context: UserContext,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if callback_data.key != "risk":
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    render_error(
                        "Доступно только изменение риска на стоп.", callback_data.revision + 1
                    ),
                )
                return
            await state.update_data(risk_field=callback_data.key)
            await state.set_state(RiskFlow.value)
            await _open_callback_view(
                menu,
                bot,
                query,
                user_context,
                _risk_prompt(callback_data.key, callback_data.revision + 1),
            )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.RISK_PRESET))
    async def risk_preset(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                if application is None:
                    raise ApplicationError("application protocol is not configured")
                if callback_data.key != "risk" or not 0 < callback_data.page < 100:
                    raise ValueError("invalid risk preset")
                await application.update_risk(
                    user_context.user_id,
                    RiskUpdateInput(risk_percent=Decimal(callback_data.page) / Decimal(100)),
                )
                view = await menu.risk(user_context.user_id, callback_data.revision + 1)
            except (ApplicationError, ValueError):
                view = render_error("Это значение риска недоступно.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.message(StateFilter(RiskFlow.value))
    async def risk_value(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        application: ApplicationProtocol | None,
        user_context: UserContext,
    ) -> None:
        data = await state.get_data()
        field = str(data.get("risk_field", ""))
        try:
            values = _parse_risk_value(field, message.text or "")
            if application is None:
                raise ApplicationError("application protocol is not configured")
            await application.update_risk(user_context.user_id, values)
            await state.clear()
            await _send_dashboard_after_message(menu, bot, message, user_context)
        except (ApplicationError, ValueError):
            with contextlib.suppress(AiogramError):
                await message.answer("Значение не прошло проверку. Попробуйте ещё раз или /cancel.")

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ACTIVITY))
    async def activity(
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
                    await menu.activity(user_context.user_id, revision=callback_data.revision + 1),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.PAGE))
    async def activity_page(
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
                    await menu.activity(
                        user_context.user_id,
                        page=max(0, callback_data.page),
                        revision=callback_data.revision + 1,
                    ),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.NOTIFICATIONS))
    async def notifications(
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
                    await menu.notifications(user_context.user_id, callback_data.revision + 1),
                )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.NOTIFICATIONS_TOGGLE))
    async def notifications_toggle(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                # The DTO is intentionally not reconstructed from rendered
                # text; ask the protocol for its authoritative current value.
                if application is None:
                    raise ApplicationError("application protocol is not configured")
                settings = await application.notification_settings(user_context.user_id)
                await application.set_notifications(user_context.user_id, not settings.enabled)
                view = await menu.notifications(user_context.user_id, callback_data.revision + 1)
            except ApplicationError:
                view = render_error("Уведомления временно недоступны.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ACCOUNT))
    async def account(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            dto = DashboardDTO()
            if menu.application is not None:
                dto = await menu.application.dashboard(user_context.user_id)
            if dto.copy_state is CopyState.EMPTY or dto.account_id is None:
                # A stale account callback can survive disconnect/replacement.
                # Never render destructive account actions for an empty owner
                # scope; return to the fresh dashboard instead.
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    await menu.dashboard(user_context.user_id, callback_data.revision + 1),
                )
                return
            await _open_callback_view(
                menu, bot, query, user_context, render_account(dto, callback_data.revision + 1)
            )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.PAUSE))
    async def pause(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                if application is None:
                    raise ApplicationError("application protocol is not configured")
                await application.pause(user_context.user_id)
                view = await menu.dashboard(user_context.user_id, callback_data.revision + 1)
            except ApplicationError:
                view = render_error(
                    "Не удалось поставить копирование на паузу.", callback_data.revision + 1
                )
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(
        MenuCallback.filter(
            F.action.in_(
                {
                    MenuAction.RESUME,
                    MenuAction.DISCONNECT,
                    MenuAction.ADMIN_PAUSE,
                    MenuAction.ADMIN_RESUME,
                    MenuAction.ADMIN_EMERGENCY,
                }
            )
        )
    )
    async def request_confirmation(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                if application is None:
                    raise ApplicationError("application protocol is not configured")
                admin_actions = {
                    MenuAction.ADMIN_PAUSE,
                    MenuAction.ADMIN_RESUME,
                    MenuAction.ADMIN_EMERGENCY,
                }
                if callback_data.action in admin_actions and (
                    user_context.role is not UserRole.ADMIN
                    or user_context.user_id not in settings.telegram_admin_ids
                ):
                    raise ApplicationError("admin access denied")
                action = {
                    MenuAction.RESUME: ConfirmationAction.RESUME,
                    MenuAction.DISCONNECT: ConfirmationAction.DISCONNECT,
                    MenuAction.ADMIN_PAUSE: ConfirmationAction.GLOBAL_PAUSE,
                    MenuAction.ADMIN_RESUME: ConfirmationAction.GLOBAL_RESUME,
                    MenuAction.ADMIN_EMERGENCY: ConfirmationAction.GLOBAL_EMERGENCY,
                }[callback_data.action]
                confirmation = await application.begin_confirmation(user_context.user_id, action)
                message = query.message
                await confirmation_store.put(
                    confirmation,
                    user_id=user_context.user_id,
                    chat_id=message.chat.id if hasattr(message, "chat") else user_context.chat_id,
                    message_id=message.message_id if hasattr(message, "message_id") else None,
                )
                view = render_confirmation(confirmation, callback_data.revision + 1)
            except ApplicationError:
                view = render_error("Операция временно недоступна.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.CONFIRM))
    async def confirm(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            message = query.message
            confirmation = await confirmation_store.consume(
                callback_data.token,
                user_id=user_context.user_id,
                chat_id=message.chat.id if hasattr(message, "chat") else user_context.chat_id,
                message_id=message.message_id if hasattr(message, "message_id") else None,
            )
            if confirmation is None or confirmation.action.value != callback_data.key:
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    render_error(
                        "Подтверждение устарело. Откройте меню заново.", callback_data.revision + 1
                    ),
                )
                return
            try:
                if application is None:
                    raise ApplicationError("application protocol is not configured")
                if confirmation.action in {
                    ConfirmationAction.GLOBAL_PAUSE,
                    ConfirmationAction.GLOBAL_RESUME,
                    ConfirmationAction.GLOBAL_EMERGENCY,
                } and (
                    user_context.role is not UserRole.ADMIN
                    or user_context.user_id not in settings.telegram_admin_ids
                ):
                    raise ApplicationError("admin access denied")
                if confirmation.action is ConfirmationAction.GLOBAL_PAUSE:
                    await application.admin_pause(user_context.user_id, confirmation)
                    view = await menu.admin(user_context.user_id, callback_data.revision + 1)
                elif confirmation.action is ConfirmationAction.GLOBAL_RESUME:
                    await application.admin_resume(user_context.user_id, confirmation)
                    view = await menu.admin(user_context.user_id, callback_data.revision + 1)
                elif confirmation.action is ConfirmationAction.GLOBAL_EMERGENCY:
                    await application.admin_emergency(user_context.user_id, confirmation)
                    view = await menu.admin(user_context.user_id, callback_data.revision + 1)
                else:
                    await application.confirm_action(user_context.user_id, confirmation)
                    view = await menu.dashboard(user_context.user_id, callback_data.revision + 1)
            except ApplicationError:
                view = render_error(
                    "Операция не выполнена. Проверьте статус.", callback_data.revision + 1
                )
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN))
    async def admin(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if (
                user_context.role is not UserRole.ADMIN
                or user_context.user_id not in settings.telegram_admin_ids
            ):
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    render_error("Недоступно.", callback_data.revision + 1),
                )
                return
            await _open_callback_view(
                menu,
                bot,
                query,
                user_context,
                await menu.admin(user_context.user_id, callback_data.revision + 1),
            )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_REQUESTS))
    async def admin_access_requests(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if not _admin_allowed(user_context, settings):
                view = render_error("Недоступно.", callback_data.revision + 1)
            else:
                try:
                    view = await menu.admin_access_requests(
                        user_context.user_id,
                        page=max(0, callback_data.page),
                        revision=callback_data.revision + 1,
                    )
                except ApplicationError:
                    view = render_error(
                        "Список заявок временно недоступен.",
                        callback_data.revision + 1,
                    )
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_USERS))
    async def admin_users(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if not _admin_allowed(user_context, settings):
                view = render_error("Недоступно.", callback_data.revision + 1)
            else:
                try:
                    view = await menu.admin_users(
                        user_context.user_id,
                        page=max(0, callback_data.page),
                        revision=callback_data.revision + 1,
                    )
                except ApplicationError:
                    view = render_error(
                        "Список пользователей временно недоступен.",
                        callback_data.revision + 1,
                    )
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_USER_ADD))
    async def admin_user_add(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        state: FSMContext,
        user_context: UserContext,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if not _admin_allowed(user_context, settings):
                view = render_error("Недоступно.", callback_data.revision + 1)
            else:
                await state.set_state(AdminUserFlow.telegram_user_id)
                view = render_prompt(
                    "Резервная выдача доступа\n\n"
                    "Пришлите числовой Telegram ID пользователя. Обычная регистрация "
                    "проходит автоматически через заявку.\n"
                    "Отмена: /cancel",
                    callback_data.revision + 1,
                )
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.message(StateFilter(AdminUserFlow.telegram_user_id))
    async def admin_user_id(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        settings: Settings,
    ) -> None:
        if not _admin_allowed(user_context, settings):
            await state.clear()
            return
        raw = (message.text or "").strip()
        try:
            target_user_id = int(raw)
            if target_user_id <= 0 or raw != str(target_user_id):
                raise ValueError
        except ValueError:
            with contextlib.suppress(AiogramError):
                await message.answer("Нужен только положительный числовой Telegram ID.")
            return
        await state.clear()
        await menu.open(
            bot,
            user_id=user_context.user_id,
            chat_id=user_context.chat_id,
            view=render_admin_user_confirmation(target_user_id),
        )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_USER_ALLOW))
    async def admin_user_allow(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                if not _admin_allowed(user_context, settings) or application is None:
                    raise ApplicationError("admin access denied")
                target_user_id = int(callback_data.key)
                await application.admin_allow_user(user_context.user_id, target_user_id)
                view = await menu.admin_users(
                    user_context.user_id,
                    revision=callback_data.revision + 1,
                )
            except (ApplicationError, ValueError):
                view = render_error("Не удалось разрешить доступ.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_USER_TOGGLE))
    async def admin_user_toggle(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                if not _admin_allowed(user_context, settings) or application is None:
                    raise ApplicationError("admin access denied")
                target_user_id = int(callback_data.key)
                users = await application.admin_users(
                    user_context.user_id,
                    page=max(0, callback_data.page),
                )
                selected = next(
                    (item for item in users.items if item.user_id == target_user_id),
                    None,
                )
                if selected is None or selected.is_admin:
                    raise ValueError("stale user selection")
                await application.admin_set_user_access(
                    user_context.user_id,
                    target_user_id,
                    not selected.access_enabled,
                )
                view = await menu.admin_users(
                    user_context.user_id,
                    page=users.page,
                    revision=callback_data.revision + 1,
                )
            except (ApplicationError, ValueError):
                view = render_error("Не удалось изменить доступ.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_FOLLOWERS))
    async def admin_followers(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if not _admin_allowed(user_context, settings):
                view = render_error("Недоступно.", callback_data.revision + 1)
            else:
                try:
                    view = await menu.admin_followers(
                        user_context.user_id,
                        page=max(0, callback_data.page),
                        revision=callback_data.revision + 1,
                    )
                except ApplicationError:
                    view = render_error(
                        "Список счетов временно недоступен.",
                        callback_data.revision + 1,
                    )
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_FOLLOWER_RECONCILE))
    async def admin_follower_reconcile(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            try:
                if not _admin_allowed(user_context, settings) or application is None:
                    raise ApplicationError("admin access denied")
                followers = await application.admin_followers(
                    user_context.user_id,
                    page=max(0, callback_data.page),
                )
                selected = next(
                    (
                        item
                        for item in followers.items
                        if follower_selection_key(item.account_id) == callback_data.key
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError("stale follower selection")
                await application.admin_reconcile(
                    user_context.user_id,
                    selected.account_id,
                )
                view = await menu.admin_followers(
                    user_context.user_id,
                    page=followers.page,
                    revision=callback_data.revision + 1,
                )
            except (ApplicationError, ValueError):
                view = render_error("Пересверка не выполнена.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ADMIN_RECONCILE))
    async def admin_reconcile(
        query: CallbackQuery,
        callback_data: MenuCallback,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
        settings: Settings,
    ) -> None:
        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if (
                user_context.role is not UserRole.ADMIN
                or user_context.user_id not in settings.telegram_admin_ids
            ):
                view = render_error("Недоступно.", callback_data.revision + 1)
            elif application is None:
                view = render_error("Админ-сервис временно недоступен.", callback_data.revision + 1)
            else:
                try:
                    await application.admin_reconcile(user_context.user_id)
                    view = await menu.admin(user_context.user_id, callback_data.revision + 1)
                except ApplicationError:
                    view = render_error("Пересверка не выполнена.", callback_data.revision + 1)
            await _open_callback_view(menu, bot, query, user_context, view)

    return router


def _risk_prompt(field: str, revision: int) -> MenuView:
    if field != "risk":
        return render_error("Доступно только изменение риска на стоп.", revision)
    label = "риск на стоп в процентах (например, 1)"
    return render_prompt(f"Введите {label}.\nМожно отменить командой /cancel.", revision)


def _admin_allowed(context: UserContext, settings: Settings) -> bool:
    return context.role is UserRole.ADMIN and context.user_id in settings.telegram_admin_ids


def _parse_risk_value(field: str, raw: str) -> RiskUpdateInput:
    if field != "risk":
        raise ValueError("only risk percent can be changed")
    value_text = raw.strip().replace(",", ".").removesuffix("%").strip()
    if not value_text:
        raise ValueError("empty risk value")
    try:
        value = Decimal(value_text)
    except InvalidOperation as exc:
        raise ValueError("invalid risk value") from exc
    if not value.is_finite() or not 0 < value < 100:
        raise ValueError("percentage must be in (0, 100)")
    fraction = value / Decimal(100)
    return RiskUpdateInput(risk_percent=fraction)


async def _send_dashboard_after_message(
    menu: MenuEngine,
    bot: Bot,
    message: Message,
    context: UserContext,
) -> None:
    await menu.open(
        bot,
        user_id=context.user_id,
        chat_id=context.chat_id,
        view=await menu.dashboard(context.user_id),
    )
