"""Finite-state input flows and transport-side confirmation lifetime."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from copytradeaster.application.models import (
    ApplicationError,
    ApplicationProtocol,
    Confirmation,
    DirectCredentialsInput,
    OnboardingApplicationError,
    UserContext,
)
from copytradeaster.telegram.menu import (
    MenuAction,
    MenuCallback,
    MenuEngine,
    MenuSession,
    MenuView,
    _is_narrow_edit_failure,
    render_onboarding_error,
    render_onboarding_expired,
    render_onboarding_intro,
    render_prompt,
)

logger = logging.getLogger(__name__)
ONBOARDING_TTL_SECONDS = 15 * 60


class OnboardingFlow(StatesGroup):
    """Linear onboarding; the private key is intentionally not a state value."""

    consent = State()
    user_address = State()
    signer_address = State()
    private_key = State()
    checking = State()


class RiskFlow(StatesGroup):
    """Short risk editor; values are validated before leaving the state."""

    value = State()


class AdminUserFlow(StatesGroup):
    """One-step admin allow-list input; only a numeric Telegram ID is stored."""

    telegram_user_id = State()


class SingleUseConfirmationStore:
    """Process-local TTL and one-shot guard for destructive callback tokens."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("confirmation TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._items: dict[
            str, tuple[Confirmation, datetime, int | None, int | None, int | None]
        ] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        confirmation: Confirmation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> None:
        expires = confirmation.expires_at or (
            datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        )
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        async with self._lock:
            self._items[confirmation.token] = (
                confirmation,
                expires,
                user_id,
                chat_id,
                message_id,
            )

    async def consume(
        self,
        token: str,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> Confirmation | None:
        now = datetime.now(UTC)
        async with self._lock:
            item = self._items.get(token)
            if item is not None:
                _, _, bound_user, bound_chat, bound_message = item
                if bound_user is not None and (user_id, chat_id, message_id) != (
                    bound_user,
                    bound_chat,
                    bound_message,
                ):
                    return None
                self._items.pop(token, None)
        if item is None:
            return None
        confirmation, expires, _, _, _ = item
        if expires <= now:
            return None
        return confirmation

    async def purge(self) -> None:
        now = datetime.now(UTC)
        async with self._lock:
            expired = [
                token for token, (_, expires, _, _, _) in self._items.items() if expires <= now
            ]
            for token in expired:
                self._items.pop(token, None)


def create_flow_router() -> Router:
    router = Router(name="telegram_onboarding")

    @router.callback_query(
        MenuCallback.filter(F.action.in_({MenuAction.CONNECT, MenuAction.REPLACE}))
    )
    async def connect_start(
        query: CallbackQuery,
        callback_data: MenuCallback,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        from copytradeaster.telegram.routers.common import _callback_scope

        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            await state.clear()
            await state.set_state(OnboardingFlow.consent)
            await state.update_data(started_at=time.time())
            replacing = callback_data.action is MenuAction.REPLACE
            await state.update_data(onboarding_mode="replace" if replacing else "connect")
            view = render_onboarding_intro(callback_data.revision + 1, replacement=replacing)
            await _open_callback_view(
                menu,
                bot,
                query,
                user_context,
                view,
                state=state,
            )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.ONBOARDING_RETRY))
    async def onboarding_retry(
        query: CallbackQuery,
        callback_data: MenuCallback,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        from copytradeaster.telegram.routers.common import _callback_scope

        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            await state.clear()
            await state.set_state(OnboardingFlow.consent)
            await state.update_data(
                started_at=time.time(),
                onboarding_mode=_onboarding_mode_from_callback(callback_data),
            )
            replacing = callback_data.key == "replace"
            message = query.message if isinstance(query.message, Message) else None
            if message is not None:
                await state.update_data(
                    anchor_chat_id=message.chat.id,
                    anchor_message_id=message.message_id,
                )
            await _open_state_transition_view(
                menu,
                bot,
                user_context,
                state,
                render_onboarding_intro(callback_data.revision + 1, replacement=replacing),
            )

    @router.callback_query(MenuCallback.filter(F.action == MenuAction.CONSENT))
    async def consent(
        query: CallbackQuery,
        callback_data: MenuCallback,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        from copytradeaster.telegram.routers.common import _callback_scope

        async with _callback_scope(query, callback_data, menu, user_context) as accepted:
            if not accepted:
                return
            if await _expired(state):
                await state.clear()
                await _open_callback_view(
                    menu,
                    bot,
                    query,
                    user_context,
                    await menu.dashboard(user_context.user_id, callback_data.revision + 1),
                    state=state,
                )
                return
            await state.set_state(OnboardingFlow.user_address)
            view = render_prompt(
                "Шаг 1 из 3\n\nПришлите адрес обычного тестового кошелька — "
                "тот, что выбран в верхней панели Aster testnet.",
                callback_data.revision + 1,
            )
            await _open_state_transition_view(menu, bot, user_context, state, view)

    @router.message(StateFilter(OnboardingFlow.user_address))
    async def user_address(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        await _delete_onboarding_input_best_effort(message)
        if await _expired(state):
            await _show_expired_recovery(menu, bot, user_context, state)
            return
        value = _text_value(message)
        if not value:
            await _open_state_view(
                menu,
                bot,
                user_context,
                state,
                render_prompt(
                    "Адрес обычного тестового кошелька не распознан. "
                    "Пришлите его ещё раз или нажмите /cancel.",
                    await _state_revision(menu, user_context, state),
                ),
            )
            return
        await state.update_data(user_address=value)
        await state.set_state(OnboardingFlow.signer_address)
        await _open_state_transition_view(
            menu,
            bot,
            user_context,
            state,
            render_prompt(
                "Шаг 2 из 3\n\nПришлите API wallet address — значение "
                "со страницы Aster API Wallet.",
                await _state_revision(menu, user_context, state),
            ),
        )

    @router.message(StateFilter(OnboardingFlow.signer_address))
    async def signer_address(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        await _delete_onboarding_input_best_effort(message)
        if await _expired(state):
            await _show_expired_recovery(menu, bot, user_context, state)
            return
        value = _text_value(message)
        if not value:
            await _open_state_view(
                menu,
                bot,
                user_context,
                state,
                render_prompt(
                    "API wallet address не распознан. Пришлите его ещё раз или нажмите /cancel.",
                    await _state_revision(menu, user_context, state),
                ),
            )
            return
        await state.update_data(signer_address=value)
        await state.set_state(OnboardingFlow.private_key)
        await _open_state_transition_view(
            menu,
            bot,
            user_context,
            state,
            render_prompt(
                "Шаг 3 из 3\n\nПришлите private key, показанный под API wallet address, "
                "отдельным сообщением. Бот удалит его сразу после получения, до обращения к Aster.",
                await _state_revision(menu, user_context, state),
            ),
        )

    @router.message(StateFilter(OnboardingFlow.private_key))
    async def signer_private_key(
        message: Message,
        state: FSMContext,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
        application: ApplicationProtocol | None,
    ) -> None:
        await _delete_onboarding_input_best_effort(message)
        if await _expired(state):
            await _show_expired_recovery(menu, bot, user_context, state)
            return
        key = _text_value(message)
        if not key:
            await _open_state_view(
                menu,
                bot,
                user_context,
                state,
                render_prompt(
                    "private key не распознан. Пришлите его ещё раз или нажмите /cancel.",
                    await _state_revision(menu, user_context, state),
                ),
            )
            return

        await state.set_state(OnboardingFlow.checking)
        await _open_state_view(
            menu,
            bot,
            user_context,
            state,
            render_prompt(
                "Проверяем подключение…",
                await _state_revision(menu, user_context, state),
            ),
        )
        data = await state.get_data()
        replacement_mode = data.get("onboarding_mode") == "replace"
        try:
            if application is None:
                raise ApplicationError("application protocol is not configured")
            credentials = DirectCredentialsInput(
                user_address=str(data.get("user_address", "")),
                signer_address=str(data.get("signer_address", "")),
                signer_private_key=key,
            )
            if replacement_mode:
                await application.replace_direct(user_context.user_id, credentials)
            else:
                await application.connect_direct(user_context.user_id, credentials)
            revision = 0
            anchor_message_id = data.get("anchor_message_id")
            if isinstance(anchor_message_id, int):
                session = await menu.sessions.get(
                    user_context.user_id,
                    int(data.get("anchor_chat_id", user_context.chat_id)),
                    anchor_message_id,
                )
                if session is not None:
                    revision = session.revision + 1
            view = await menu.dashboard(user_context.user_id, revision=revision)
            # A successful connection is intentionally paused.  Re-render the
            # dashboard from the application so no credential-derived data is
            # kept in FSM context.
            await _replace_anchor(menu, bot, user_context, state, view)
        except OnboardingApplicationError as exc:
            revision = await _state_revision(menu, user_context, state)
            await _open_state_view(
                menu,
                bot,
                user_context,
                state,
                render_onboarding_error(
                    exc.code,
                    revision=revision,
                    replacement=replacement_mode,
                ),
            )
        except ApplicationError:
            revision = await _state_revision(menu, user_context, state)
            await _open_state_view(
                menu,
                bot,
                user_context,
                state,
                render_onboarding_error(
                    None,
                    revision=revision,
                    replacement=replacement_mode,
                ),
            )
        finally:
            # The key is never written to FSM data; clear all onboarding data
            # after the application boundary returns or fails.
            key = ""
            await state.clear()

    return router


def _onboarding_mode_from_callback(callback_data: MenuCallback) -> str:
    return "replace" if callback_data.key == "replace" else "connect"


def _text_value(message: Message) -> str:
    return (message.text or "").strip()


async def _delete_onboarding_input_best_effort(message: Message) -> bool:
    """Delete any credential-flow input without ever logging its contents."""
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 - Telegram/network errors are best effort here.
        logger.warning("onboarding input deletion failed (%s)", type(exc).__name__)
        return False
    return True


async def _delete_onboarding_prompt_best_effort(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
) -> bool:
    """Retire a stale onboarding card; callers provide an edit fallback."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:  # noqa: BLE001 - deletion must not break onboarding.
        logger.warning("onboarding prompt deletion failed (%s)", type(exc).__name__)
        return False
    return True


async def _expired(state: FSMContext) -> bool:
    data = await state.get_data()
    started_at = data.get("started_at")
    return (
        not isinstance(started_at, (int, float))
        or time.time() - started_at > ONBOARDING_TTL_SECONDS
    )


async def _show_expired_recovery(
    menu: MenuEngine,
    bot: Bot,
    context: UserContext,
    state: FSMContext,
) -> None:
    """Replace the stale onboarding card, then clear the expired FSM."""
    data = await state.get_data()
    replacement = data.get("onboarding_mode") == "replace"
    revision = await _state_revision(menu, context, state)
    try:
        await _open_state_view(
            menu,
            bot,
            context,
            state,
            render_onboarding_expired(revision=revision, replacement=replacement),
        )
    finally:
        await state.clear()


async def _forget_anchor(
    state: FSMContext,
    *,
    chat_id: int,
    message_id: int,
) -> None:
    """Remove a retired anchor from FSM without touching onboarding values."""
    data = await state.get_data()
    if data.get("anchor_message_id") != message_id:
        return
    if int(data.get("anchor_chat_id", chat_id)) != chat_id:
        return
    data.pop("anchor_chat_id", None)
    data.pop("anchor_message_id", None)
    await state.set_data(data)


async def _open_callback_view(
    menu: MenuEngine,
    bot: Bot,
    query: CallbackQuery,
    context: UserContext,
    view: MenuView,
    *,
    state: FSMContext,
) -> None:
    message = query.message if isinstance(query.message, Message) else None
    result = await menu.open(
        bot,
        user_id=context.user_id,
        chat_id=context.chat_id,
        view=view,
        message=message,
        query=query,
    )
    if isinstance(result, Message):
        await state.update_data(anchor_chat_id=result.chat.id, anchor_message_id=result.message_id)
    elif message is not None:
        await state.update_data(
            anchor_chat_id=message.chat.id, anchor_message_id=message.message_id
        )


async def _state_revision(menu: MenuEngine, context: UserContext, state: FSMContext) -> int:
    data = await state.get_data()
    message_id = data.get("anchor_message_id")
    if not isinstance(message_id, int):
        return 0
    session = await menu.sessions.get(
        context.user_id,
        int(data.get("anchor_chat_id", context.chat_id)),
        message_id,
    )
    return session.revision + 1 if session is not None else 0


async def _open_state_view(
    menu: MenuEngine,
    bot: Bot,
    context: UserContext,
    state: FSMContext,
    view: MenuView,
) -> None:
    data = await state.get_data()
    chat_id = int(data.get("anchor_chat_id", context.chat_id))
    message_id = data.get("anchor_message_id")
    if isinstance(message_id, int):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=view.text,
                reply_markup=view.markup,
                parse_mode=None,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower() and not _is_narrow_edit_failure(
                exc
            ):
                raise
            if "message is not modified" in str(exc).lower():
                await menu.sessions.remember(
                    MenuSession(
                        user_id=context.user_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        screen=view.screen,
                        revision=view.revision,
                        updated_at=datetime.now(UTC),
                    )
                )
                return
            await menu.sessions.invalidate(context.user_id, chat_id, message_id)
            await _forget_anchor(state, chat_id=chat_id, message_id=message_id)
        else:
            await menu.sessions.remember(
                MenuSession(
                    user_id=context.user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    screen=view.screen,
                    revision=view.revision,
                    updated_at=datetime.now(UTC),
                )
            )
            return
    sent = await bot.send_message(
        chat_id=chat_id,
        text=view.text,
        reply_markup=view.markup,
        parse_mode=None,
    )
    if isinstance(sent, Message):
        await state.update_data(anchor_chat_id=sent.chat.id, anchor_message_id=sent.message_id)
        await menu.sessions.remember(
            MenuSession(
                user_id=context.user_id,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                screen=view.screen,
                revision=view.revision,
                updated_at=datetime.now(UTC),
            )
        )


async def _open_state_transition_view(
    menu: MenuEngine,
    bot: Bot,
    context: UserContext,
    state: FSMContext,
    view: MenuView,
) -> None:
    """Advance onboarding on a fresh card, falling back to an in-place edit."""
    data = await state.get_data()
    chat_id = int(data.get("anchor_chat_id", context.chat_id))
    message_id = data.get("anchor_message_id")
    if isinstance(message_id, int):
        if await _delete_onboarding_prompt_best_effort(
            bot,
            chat_id=chat_id,
            message_id=message_id,
        ):
            await menu.sessions.invalidate(context.user_id, chat_id, message_id)
            await _forget_anchor(state, chat_id=chat_id, message_id=message_id)
        else:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=view.text,
                    reply_markup=view.markup,
                    parse_mode=None,
                )
            except TelegramBadRequest as exc:
                if not _is_narrow_edit_failure(exc):
                    raise
                await menu.sessions.invalidate(context.user_id, chat_id, message_id)
                await _forget_anchor(state, chat_id=chat_id, message_id=message_id)
            else:
                await menu.sessions.remember(
                    MenuSession(
                        user_id=context.user_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        screen=view.screen,
                        revision=view.revision,
                        updated_at=datetime.now(UTC),
                    )
                )
                return
    sent = await bot.send_message(
        chat_id=chat_id,
        text=view.text,
        reply_markup=view.markup,
        parse_mode=None,
    )
    if isinstance(sent, Message):
        await state.update_data(anchor_chat_id=sent.chat.id, anchor_message_id=sent.message_id)
        await menu.sessions.remember(
            MenuSession(
                user_id=context.user_id,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                screen=view.screen,
                revision=view.revision,
                updated_at=datetime.now(UTC),
            )
        )


async def _replace_anchor(
    menu: MenuEngine,
    bot: Bot,
    context: UserContext,
    state: FSMContext,
    view: MenuView,
) -> None:
    data = await state.get_data()
    chat_id = int(data.get("anchor_chat_id", context.chat_id))
    # Onboarding sends its prompt through the same single-anchor API when the
    # original callback message is available.  If it is gone, send a fresh
    # dashboard and let normal menu navigation continue.
    sent: Message | None = None
    message_id = data.get("anchor_message_id")
    if isinstance(message_id, int):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=view.text,
                reply_markup=view.markup,
                parse_mode=None,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower() and not _is_narrow_edit_failure(
                exc
            ):
                raise
            if "message is not modified" in str(exc).lower():
                await menu.sessions.remember(
                    MenuSession(
                        user_id=context.user_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        screen=view.screen,
                        revision=view.revision,
                        updated_at=datetime.now(UTC),
                    )
                )
                return
            await menu.sessions.invalidate(context.user_id, chat_id, message_id)
            await _forget_anchor(state, chat_id=chat_id, message_id=message_id)
        else:
            await menu.sessions.remember(
                MenuSession(
                    user_id=context.user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    screen=view.screen,
                    revision=view.revision,
                    updated_at=datetime.now(UTC),
                )
            )
            return
    sent = await bot.send_message(
        chat_id=chat_id, text=view.text, reply_markup=view.markup, parse_mode=None
    )
    if isinstance(sent, Message):
        await state.update_data(anchor_chat_id=sent.chat.id, anchor_message_id=sent.message_id)
        await menu.sessions.remember(
            MenuSession(
                user_id=context.user_id,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                screen=view.screen,
                revision=view.revision,
                updated_at=datetime.now(UTC),
            )
        )
