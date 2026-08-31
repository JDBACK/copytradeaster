"""Telegram runtime assembly and bounded polling lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import AiogramError
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    ErrorEvent,
    MenuButtonCommands,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from copytradeaster.application.models import ApplicationProtocol, UserContext
from copytradeaster.config import Settings
from copytradeaster.telegram.access import CaptchaChallengeStore
from copytradeaster.telegram.flow import SingleUseConfirmationStore, create_flow_router
from copytradeaster.telegram.menu import MenuEngine, MenuSessionStore
from copytradeaster.telegram.middleware import (
    BoundedTaskTracker,
    CallbackAnswerMiddleware,
    PrivateChatAuthorizationMiddleware,
    TelegramTaskMiddleware,
)
from copytradeaster.telegram.routers.access import create_access_router
from copytradeaster.telegram.routers.common import create_common_router
from copytradeaster.telegram.routers.settings import create_settings_router

logger = logging.getLogger(__name__)


def create_bot(settings: Settings) -> Bot:
    """Create a Bot with safe plain-text defaults and no token logging."""
    return Bot(
        token=settings.telegram_token_value(),
        default=DefaultBotProperties(parse_mode=None, protect_content=True),
    )


def create_dispatcher(
    settings: Settings,
    application: ApplicationProtocol | None = None,
    *,
    task_tracker: BoundedTaskTracker | None = None,
    menu: MenuEngine | None = None,
    allow_test_only_application: bool = False,
) -> Dispatcher:
    """Compose the private-chat dispatcher without importing trading services."""
    if application is None and not allow_test_only_application:
        raise ValueError("Telegram dispatcher requires an application protocol")
    if settings.testnet_ip_whitelist_override:
        logger.warning("testnet IP whitelist override is enabled for this Telegram runtime")
    tracker = task_tracker or BoundedTaskTracker(settings.telegram_tasks_concurrency_limit)
    menu_engine = menu or MenuEngine(
        application,
        MenuSessionStore(),
        admin_ids=frozenset(settings.telegram_admin_ids),
    )
    # Dispatcher normally installs FSM first; move it behind an update-level
    # callback acknowledgement so a second callback cannot wait on
    # SimpleEventIsolation before Telegram gets its mandatory answer.
    access_captcha = CaptchaChallengeStore()
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
        application=application,
        menu=menu_engine,
        task_tracker=tracker,
        confirmations=SingleUseConfirmationStore(),
        access_captcha=access_captcha,
        settings=settings,
    )
    dispatcher.update.outer_middleware.unregister(dispatcher.fsm)
    dispatcher.update.outer_middleware(CallbackAnswerMiddleware())
    dispatcher.update.outer_middleware(dispatcher.fsm)

    access = create_access_router(access_captcha)
    common = create_common_router()
    flow = create_flow_router()
    settings_router = create_settings_router(dispatcher.workflow_data["confirmations"])
    fallback = _create_fallback_router()
    dispatcher.include_router(access)
    dispatcher.include_router(common)
    dispatcher.include_router(flow)
    dispatcher.include_router(settings_router)
    dispatcher.include_router(fallback)

    dispatcher.message.outer_middleware(TelegramTaskMiddleware(tracker))
    dispatcher.callback_query.outer_middleware(TelegramTaskMiddleware(tracker))
    auth = PrivateChatAuthorizationMiddleware(application)
    dispatcher.message.outer_middleware(auth)
    dispatcher.callback_query.outer_middleware(auth)
    dispatcher.errors.register(_safe_error_handler)
    return dispatcher


def _create_fallback_router() -> Router:
    router = Router(name="telegram_fallback")

    @router.callback_query()
    async def malformed_callback(
        query: CallbackQuery,
        menu: MenuEngine,
        bot: Bot,
        user_context: UserContext,
    ) -> None:
        """Answer malformed/unknown callback data and retire old buttons."""
        message = query.message if isinstance(query.message, Message) else None
        session = (
            await menu.sessions.get(user_context.user_id, message.chat.id, message.message_id)
            if message is not None
            else None
        )
        revision = session.revision + 1 if session is not None else 0
        await menu.open(
            bot,
            user_id=user_context.user_id,
            chat_id=user_context.chat_id,
            view=await menu.dashboard(user_context.user_id, revision=revision),
            message=message,
            query=query,
        )

    return router


async def _safe_error_handler(event: ErrorEvent, bot: Bot, **_: Any) -> None:
    """Root error boundary: neutral user copy, never raw exception text."""
    logger.error("telegram handler failed (%s)", type(event.exception).__name__)
    update = event.update
    query = update.callback_query
    if isinstance(query, CallbackQuery):
        return
    message = update.message
    if isinstance(message, Message) and message.chat.type == "private":
        with contextlib.suppress(AiogramError):
            await message.answer("Временная ошибка. Попробуйте /menu ещё раз.")


def normalize_webapp_url(url: str | None) -> str | None:
    """Ensure the Mini App URL terminates in /miniapp/."""
    if not url:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    cleaned_root = cleaned.rstrip("/")
    if not cleaned_root.endswith("/miniapp"):
        return f"{cleaned_root}/miniapp/"
    return f"{cleaned_root}/"


async def register_commands(bot: Bot, *, web_app_url: str | None = None) -> None:
    """Install the narrow Stage 2 command surface and private menu button.

    When the Mini App is deployed (``CT_MINIAPP_PUBLIC_URL``), the private
    menu button launches it instead of the command list — the command surface
    stays available through ``/`` as before.
    """
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="menu", description="Открыть меню"),
            BotCommand(command="cancel", description="Отменить ввод"),
            BotCommand(command="help", description="Помощь"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    target_url = normalize_webapp_url(web_app_url)
    if target_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="ASTER COPY",
                web_app=WebAppInfo(url=target_url),
            )
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def install_admin_webapp_buttons(bot: Bot, settings: Settings) -> None:
    """Install the WebApp button explicitly in every admin chat.

    A stale per-chat override (for example from an earlier BotFather setup)
    beats the bot default and would hide the Mini App for exactly the people
    who test it first.  Best effort: failures never block startup.
    """
    target_url = normalize_webapp_url(settings.miniapp_public_url)
    if not target_url:
        return
    for admin_id in settings.telegram_admin_ids:
        try:
            await bot.set_chat_menu_button(
                chat_id=admin_id,
                menu_button=MenuButtonWebApp(
                    text="ASTER COPY",
                    web_app=WebAppInfo(url=target_url),
                ),
            )
        except AiogramError as exc:
            logger.warning(
                "Mini App menu button for chat %s failed (%s)", admin_id, type(exc).__name__
            )


async def ensure_webhook_clean(bot: Bot) -> tuple[str, str]:
    """Delete a configured webhook and re-check that polling can own updates."""
    before = await bot.get_webhook_info()
    before_url = before.url or ""
    if before_url:
        await bot.delete_webhook(drop_pending_updates=False)
    after = await bot.get_webhook_info()
    after_url = after.url or ""
    if after_url:
        raise RuntimeError("Telegram webhook remains configured after deletion")
    return before_url, after_url


@dataclass(slots=True)
class TelegramRuntime:
    """Lifecycle wrapper usable by a future App/CLI integration seam."""

    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    tracker: BoundedTaskTracker
    _transport_closed: bool = False
    _session_closed: bool = False
    _shutdown_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @classmethod
    def build(
        cls,
        settings: Settings,
        application: ApplicationProtocol,
    ) -> TelegramRuntime:
        if application is None:
            raise ValueError("Telegram runtime requires an application protocol")
        tracker = BoundedTaskTracker(settings.telegram_tasks_concurrency_limit)
        bot = create_bot(settings)
        dispatcher = create_dispatcher(settings, application, task_tracker=tracker)
        return cls(settings=settings, bot=bot, dispatcher=dispatcher, tracker=tracker)

    async def startup(self) -> None:
        await ensure_webhook_clean(self.bot)
        try:
            await register_commands(
                self.bot,
                web_app_url=self.settings.miniapp_public_url or None,
            )
            await install_admin_webapp_buttons(self.bot, self.settings)
        except AiogramError as exc:
            # Commands/menu-button metadata is best effort; handlers remain
            # usable when Telegram temporarily rejects configuration calls.
            logger.warning("Telegram command registration failed (%s)", type(exc).__name__)

    async def run(
        self,
        stop_event: asyncio.Event | None = None,
        *,
        close_session: bool = True,
    ) -> None:
        """Run polling and shut down transport-owned tasks.

        ``close_session`` defaults to ``True`` for standalone polling.  A
        larger composition that shares this Bot session (for example with an
        outbox worker) can leave it open, drain that worker, and call the
        public :meth:`close_session` seam afterwards.
        """
        polling: asyncio.Task[None] | None = None
        stop_wait: asyncio.Task[bool] | None = None
        try:
            await self.startup()
            polling = asyncio.create_task(
                self.dispatcher.start_polling(
                    self.bot,
                    polling_timeout=self.settings.telegram_polling_timeout_s,
                    handle_as_tasks=True,
                    allowed_updates=self.dispatcher.resolve_used_update_types(),
                    handle_signals=True,
                    close_bot_session=False,
                    tasks_concurrency_limit=self.settings.telegram_tasks_concurrency_limit,
                )
            )
            if stop_event is None:
                await polling
            else:
                stop_wait = asyncio.create_task(stop_event.wait())
                done, _ = await asyncio.wait(
                    (polling, stop_wait),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if polling in done:
                    # A polling failure/conflict is an engine-level stop
                    # signal, not a silent Telegram-only failure.
                    stop_event.set()
                    await polling
                else:
                    try:
                        await self.dispatcher.stop_polling()
                    except RuntimeError:
                        # A startup race can finish before aiogram owns its
                        # running lock; cancellation remains deterministic.
                        polling.cancel()
                        await asyncio.gather(polling, return_exceptions=True)
                    else:
                        await polling
        finally:
            if stop_wait is not None and not stop_wait.done():
                stop_wait.cancel()
            if stop_wait is not None:
                await asyncio.gather(stop_wait, return_exceptions=True)
            if polling is not None and not polling.done():
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
            await self.shutdown(close_session=close_session)

    async def shutdown(self, *, close_session: bool = True) -> None:
        """Close transport admission and drain tasks, optionally close Bot session.

        Transport shutdown is independent from Bot-session ownership so an
        outbox worker can finish using the shared session before the outer
        composition closes it.  The lock makes concurrent lifecycle calls
        deterministic and keeps session close strictly one-shot.
        """
        async with self._shutdown_lock:
            if not self._transport_closed:
                self._transport_closed = True
                self.tracker.close_admission()
                await self.tracker.drain(timeout=10.0)
            if close_session:
                await self._close_session_unlocked()

    async def close_session(self) -> None:
        """Close the Bot session exactly once after shared workers have drained."""
        async with self._shutdown_lock:
            await self._close_session_unlocked()

    async def _close_session_unlocked(self) -> None:
        if self._session_closed:
            return
        self._session_closed = True
        await self.bot.session.close()


async def run_polling(
    settings: Settings,
    application: ApplicationProtocol,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Build and run the Telegram transport; no core/App integration is implied."""
    runtime = TelegramRuntime.build(settings, application)
    await runtime.run(stop_event=stop_event)
