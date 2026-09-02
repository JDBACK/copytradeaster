"""Small inline-menu engine.

The engine is deliberately presenter-only: it calls the transport-neutral
application protocol and turns DTOs into one compact, editable Telegram view.
It does not know about Aster, SQLite, or trading plans.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from copytradeaster.application.models import (
    AccessRequestDTO,
    AccessRequestStatus,
    ActivityPage,
    AdminAccessRequestPage,
    AdminDashboardDTO,
    AdminFollowerPage,
    AdminUserPage,
    ApplicationProtocol,
    Confirmation,
    ConfirmationAction,
    CopyState,
    DashboardDTO,
    NotificationSettingsDTO,
    RiskDTO,
    StatusDTO,
    SystemState,
)
from copytradeaster.onboarding.errors import OnboardingErrorCode
from copytradeaster.telegram.access import (
    AccessDecisionAction,
    AccessDecisionCallback,
    CaptchaChallenge,
    GuestAccessAction,
    GuestAccessCallback,
)


class Screen(StrEnum):
    ACCESS = "access"
    HOME = "home"
    STATUS = "status"
    RISK = "risk"
    ACTIVITY = "activity"
    NOTIFICATIONS = "notifications"
    HELP = "help"
    SECURITY = "security"
    ACCOUNT = "account"
    ADMIN = "admin"
    ADMIN_USERS = "admin_users"
    ADMIN_FOLLOWERS = "admin_followers"
    ADMIN_REQUESTS = "admin_requests"
    CONFIRM = "confirm"
    ONBOARDING_ERROR = "onboarding_error"


class MenuAction(StrEnum):
    HOME = "h"
    BACK = "b"
    CONNECT = "c"
    CONSENT = "co"
    STATUS = "s"
    STATUS_REFRESH = "sr"
    RISK = "r"
    RISK_EDIT = "ri"
    RISK_PRESET = "rx"
    ACTIVITY = "a"
    PAGE = "p"
    NOTIFICATIONS = "n"
    NOTIFICATIONS_TOGGLE = "nt"
    HELP = "he"
    HOW = "hw"
    SECURITY = "sec"
    ACCOUNT = "ac"
    REPLACE = "cp"
    PAUSE = "pa"
    RESUME = "rs"
    DISCONNECT = "dc"
    ADMIN = "ad"
    ADMIN_RECONCILE = "ar"
    ADMIN_PAUSE = "ap"
    ADMIN_RESUME = "av"
    ADMIN_EMERGENCY = "ae"
    ADMIN_USERS = "au"
    ADMIN_USER_ADD = "aa"
    ADMIN_USER_ALLOW = "al"
    ADMIN_USER_TOGGLE = "at"
    ADMIN_FOLLOWERS = "af"
    ADMIN_REQUESTS = "aq"
    ADMIN_FOLLOWER_RECONCILE = "fr"
    ONBOARDING_RETRY = "or"
    CONFIRM = "x"


class MenuCallback(CallbackData, prefix="m", sep=":"):
    """One compact, typed callback family for all menu screens."""

    action: MenuAction
    key: str = ""
    page: int = 0
    revision: int = 0
    token: str = ""


def callback_bytes(callback: MenuCallback) -> int:
    """Return the exact Telegram callback-data byte length."""
    return len(callback.pack().encode("utf-8"))


def follower_selection_key(account_id: str) -> str:
    """Return a stable opaque callback key without exposing an internal account ID."""
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class MenuView:
    screen: Screen
    text: str
    markup: InlineKeyboardMarkup
    revision: int

    def __post_init__(self) -> None:
        # Telegram applies the 4096-character limit after entity parsing.  All
        # Stage 2 views use plain text, so a deterministic clip is sufficient
        # and prevents a dynamic activity/detail value from breaking a render.
        if len(self.text) > 4096:
            object.__setattr__(self, "text", f"{self.text[:4093]}…")


@dataclass(frozen=True, slots=True)
class MenuSession:
    user_id: int
    chat_id: int
    message_id: int
    screen: Screen
    revision: int
    updated_at: datetime


class MenuSessionStore:
    """Process-local revision and per-anchor serialization for 10–100 users."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int, int], MenuSession] = {}
        self._locks: dict[tuple[int, int, int], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: tuple[int, int, int]) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(key, asyncio.Lock())

    @asynccontextmanager
    async def serialized(self, user_id: int, chat_id: int, message_id: int) -> AsyncIterator[None]:
        key = (user_id, chat_id, message_id)
        lock = await self._lock_for(key)
        async with lock:
            yield

    async def remember(self, session: MenuSession) -> None:
        async with self._guard:
            self._sessions[(session.user_id, session.chat_id, session.message_id)] = session

    async def get(self, user_id: int, chat_id: int, message_id: int) -> MenuSession | None:
        async with self._guard:
            return self._sessions.get((user_id, chat_id, message_id))

    async def invalidate(self, user_id: int, chat_id: int, message_id: int) -> None:
        async with self._guard:
            self._sessions.pop((user_id, chat_id, message_id), None)

    async def accept_revision(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        revision: int,
    ) -> bool:
        session = await self.get(user_id, chat_id, message_id)
        return session is not None and session.revision == revision


class MenuEngine:
    """Render and update the one-anchor dashboard menu."""

    def __init__(
        self,
        application: ApplicationProtocol | None = None,
        sessions: MenuSessionStore | None = None,
        admin_ids: set[int] | frozenset[int] | None = None,
    ) -> None:
        self.application = application
        self.sessions = sessions or MenuSessionStore()
        self.admin_ids = admin_ids

    async def dashboard(self, user_id: int, revision: int = 0) -> MenuView:
        dto = DashboardDTO()
        if self.application is not None:
            dto = await self.application.dashboard(user_id)
        if self.admin_ids is not None:
            dto = replace(dto, is_admin=dto.is_admin and user_id in self.admin_ids)
        return render_dashboard(dto, revision)

    async def status(self, user_id: int, revision: int = 0, refresh: bool = False) -> MenuView:
        dto = StatusDTO()
        if self.application is not None:
            dto = await self.application.status(user_id, refresh=refresh)
        return render_status(dto, revision)

    async def risk(self, user_id: int, revision: int = 0) -> MenuView:
        dto = RiskDTO(
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=Decimal("0.02"),
        )
        if self.application is not None:
            dto = await self.application.risk(user_id)
        return render_risk(dto, revision)

    async def activity(self, user_id: int, page: int = 0, revision: int = 0) -> MenuView:
        dto = ActivityPage(page=page)
        if self.application is not None:
            dto = await self.application.activity(user_id, page=max(0, page))
        return render_activity(dto, revision)

    async def notifications(self, user_id: int, revision: int = 0) -> MenuView:
        dto = NotificationSettingsDTO()
        if self.application is not None:
            dto = await self.application.notification_settings(user_id)
        return render_notifications(dto, revision)

    async def admin(self, user_id: int, revision: int = 0) -> MenuView:
        dto = AdminDashboardDTO()
        if self.application is not None:
            dto = await self.application.admin_dashboard(user_id)
        return render_admin(dto, revision)

    async def admin_users(self, user_id: int, page: int = 0, revision: int = 0) -> MenuView:
        dto = AdminUserPage(page=page)
        if self.application is not None:
            dto = await self.application.admin_users(user_id, page=max(0, page))
        return render_admin_users(dto, revision)

    async def admin_followers(self, user_id: int, page: int = 0, revision: int = 0) -> MenuView:
        dto = AdminFollowerPage(page=page)
        if self.application is not None:
            dto = await self.application.admin_followers(user_id, page=max(0, page))
        return render_admin_followers(dto, revision)

    async def admin_access_requests(
        self,
        user_id: int,
        page: int = 0,
        revision: int = 0,
    ) -> MenuView:
        dto = AdminAccessRequestPage(page=page)
        if self.application is not None:
            dto = await self.application.admin_access_requests(user_id, page=max(0, page))
        return render_admin_access_requests(dto, revision)

    async def open(
        self,
        bot: Bot,
        *,
        user_id: int,
        chat_id: int,
        view: MenuView,
        message: Message | None = None,
        query: CallbackQuery | None = None,
    ) -> Message | bool | None:
        """Send a new anchor or edit a regular/inline anchor."""
        result: Message | bool | None
        if isinstance(message, Message):
            result = await self._edit_message(message, view)
            if result is False:
                # Deleted/inaccessible regular anchors cannot be edited.  A
                # fresh private message is the safe recovery path.
                await self.sessions.invalidate(user_id, chat_id, message.message_id)
                result = await bot.send_message(
                    chat_id=chat_id,
                    text=view.text,
                    reply_markup=view.markup,
                    parse_mode=None,
                )
        elif query is not None and query.inline_message_id:
            result = await self._edit_inline(bot, query.inline_message_id, view)
        else:
            result = await bot.send_message(
                chat_id=chat_id,
                text=view.text,
                reply_markup=view.markup,
                parse_mode=None,
            )

        if isinstance(result, Message):
            await self.sessions.remember(
                MenuSession(
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=result.message_id,
                    screen=view.screen,
                    revision=view.revision,
                    updated_at=datetime.now(UTC),
                )
            )
        elif isinstance(message, Message):
            await self.sessions.remember(
                MenuSession(
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=message.message_id,
                    screen=view.screen,
                    revision=view.revision,
                    updated_at=datetime.now(UTC),
                )
            )
        return result

    async def stale(self, query: CallbackQuery, *, already_answered: bool = False) -> None:
        """Answer an old callback and remove its buttons when possible."""
        if not already_answered:
            await query.answer("Меню устарело", show_alert=False, cache_time=0)
        if isinstance(query.message, Message):
            with contextlib.suppress(TelegramBadRequest):
                await query.message.edit_reply_markup(reply_markup=None)
        if query.message is None and not query.inline_message_id:
            # The caller can open a fresh menu after this neutral response.
            return

    async def _edit_message(self, message: Message, view: MenuView) -> Message | bool | None:
        try:
            return await message.edit_text(
                text=view.text,
                reply_markup=view.markup,
                parse_mode=None,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message
            if _is_narrow_edit_failure(exc):
                return False
            raise

    async def _edit_inline(self, bot: Bot, inline_message_id: str, view: MenuView) -> bool:
        try:
            return await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=view.text,
                reply_markup=view.markup,
                parse_mode=None,
            )
        except TelegramBadRequest as exc:
            if _is_narrow_edit_failure(exc):
                return False
            raise


def _is_narrow_edit_failure(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "message is not modified",
            "message to edit not found",
            "message can't be edited",
            "message is inaccessible",
        )
    )


def _cb(action: MenuAction, revision: int, **kwargs: object) -> MenuCallback:
    if any(isinstance(value, str) and ":" in value for value in kwargs.values()):
        raise ValueError("callback fields must not contain the separator")
    callback = MenuCallback(action=action, revision=revision, **kwargs)
    # pack() performs aiogram's exact UTF-8 64-byte validation at render time.
    callback.pack()
    return callback


def _builder() -> InlineKeyboardBuilder:
    return InlineKeyboardBuilder()


def _percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{(value * 100):.1f}%".replace(".", ",")


def _when(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%H:%M")


def _system_icon(state: SystemState) -> str:
    return {
        SystemState.READY: "🟢",
        SystemState.STALE: "🟠",
        SystemState.DEGRADED: "🟠",
        SystemState.UNKNOWN: "⚪",
    }[state]


def _system_label(state: SystemState) -> str:
    return {
        SystemState.READY: "работает",
        SystemState.STALE: "данные устарели",
        SystemState.DEGRADED: "нужна проверка",
        SystemState.UNKNOWN: "неизвестно",
    }[state]


def _copy_label(state: CopyState) -> str:
    return {
        CopyState.EMPTY: "счёт не подключён",
        CopyState.ACTIVE: "включено",
        CopyState.PAUSED: "новые входы на паузе",
        CopyState.DEGRADED: "нужна проверка",
        CopyState.BLOCKED: "недоступно",
    }[state]


def _notice(value: str | None) -> str | None:
    if value is None:
        return None
    translations = {
        "account is not connected": "Счёт подписчика не подключён.",
        "account is disconnected; connect credentials to continue": (
            "Счёт отключён. Подключите его заново, чтобы продолжить."
        ),
        "account policy is unavailable": "Настройки риска временно недоступны.",
        "runtime requires attention": "Система требует проверки.",
        "global emergency is active": "Включён общий аварийный режим.",
        "global pause is active": "Включена общая пауза новых входов.",
        "new entries are paused": "Новые входы поставлены на паузу.",
        "Aster status is temporarily unavailable": "Aster временно не отвечает.",
        "permission_check_unavailable": "Права подключения пока не проверены.",
        "permission_check_pending": "Проверка прав подключения ещё не завершена.",
        "permission_check_override": (
            "Проверка ограничения по IP временно пропущена только для тестовой биржи."
        ),
    }
    return translations.get(value, value)


def _tri_state(value: bool | None) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    return "неизвестно"


def render_dashboard(dto: DashboardDTO, revision: int = 0) -> MenuView:
    builder = _builder()
    account = dto.account_masked or "—"
    positions = "—" if dto.open_positions is None else str(dto.open_positions)
    risk = _percent(dto.risk_percent)

    if dto.copy_state is CopyState.EMPTY:
        if dto.is_admin:
            text = (
                "Aster Copy\n\n"
                "Админский доступ активен.\n"
                "Вы управляете счётом-источником и всей системой через админку.\n"
                "Личный тестовый счёт подписчика не подключён — он нужен "
                "только если вы хотите отдельно проверить копирование на себе."
            )
            builder.button(
                text="🛠 Администрирование", callback_data=_cb(MenuAction.ADMIN, revision)
            )
            builder.button(
                text="🔐 Подключить личный тестовый счёт",
                callback_data=_cb(MenuAction.CONNECT, revision),
            )
            builder.button(text="Как это работает", callback_data=_cb(MenuAction.HOW, revision))
            builder.button(text="Безопасность", callback_data=_cb(MenuAction.SECURITY, revision))
            builder.adjust(1, 1, 2)
        else:
            text = (
                "Aster Copy\n\n"
                "Аккаунт пока не подключён.\n"
                "Подключите отдельный счёт на тестовой бирже Aster, затем проверьте риск "
                "и включите копирование."
            )
            builder.button(
                text="🔐 Подключить Aster", callback_data=_cb(MenuAction.CONNECT, revision)
            )
            builder.button(text="Как это работает", callback_data=_cb(MenuAction.HOW, revision))
            builder.button(text="Безопасность", callback_data=_cb(MenuAction.SECURITY, revision))
            builder.adjust(1, 2)
    else:
        if dto.copy_state is CopyState.ACTIVE:
            heading = "🟢 Всё работает\nКопирование: включено"
            builder.button(
                text="⏸ Поставить на паузу", callback_data=_cb(MenuAction.PAUSE, revision)
            )
        elif dto.copy_state is CopyState.PAUSED:
            heading = "⏸ Новые входы на паузе\nЗакрытия, сокращения и защита продолжают работать."
            builder.button(
                text="▶️ Возобновить копирование", callback_data=_cb(MenuAction.RESUME, revision)
            )
        else:
            heading = (
                f"{_system_icon(dto.system_state)} Нужна проверка\n"
                "Показаны последние сохранённые данные."
            )
            builder.button(
                text="↻ Проверить снова", callback_data=_cb(MenuAction.STATUS_REFRESH, revision)
            )

        text = (
            "Aster Copy\n\n"
            f"{heading}\n"
            f"Счёт: {account}\n"
            f"Риск на стоп: {risk}\n"
            "Плечо: автоматически по активу\n"
            f"Открытые позиции: {positions}\n\n"
            f"Обновлено в {_when(dto.updated_at)}"
        )
        builder.button(text="⚙️ Риск", callback_data=_cb(MenuAction.RISK, revision))
        builder.button(text="📊 Статус", callback_data=_cb(MenuAction.STATUS, revision))
        builder.button(text="🕘 Активность", callback_data=_cb(MenuAction.ACTIVITY, revision))
        builder.button(text="🔔 Уведомления", callback_data=_cb(MenuAction.NOTIFICATIONS, revision))
        builder.button(text="👤 Счёт", callback_data=_cb(MenuAction.ACCOUNT, revision))
        builder.button(text="❓ Помощь", callback_data=_cb(MenuAction.HELP, revision))
        if dto.is_admin:
            builder.button(text="🛠 Админ", callback_data=_cb(MenuAction.ADMIN, revision))
        builder.adjust(1, 2, 2, 1)

    notices = [_notice(dto.message), _notice(dto.permission.warning)]
    if any(notice for notice in notices):
        text += "\n\n" + "\n".join(notice for notice in notices if notice)

    return MenuView(Screen.HOME, text, builder.as_markup(), revision)


def render_status(dto: StatusDTO, revision: int = 0) -> MenuView:
    builder = _builder()
    permission = dto.permissions
    text = (
        "Статус\n\n"
        f"Система: {_system_label(dto.system_state)}\n"
        f"Копирование: {_copy_label(dto.copy_state)}\n"
        f"Aster: {'подключён' if dto.aster_connected else 'проверка недоступна'}\n"
        f"Права подключения: чтение {_tri_state(permission.can_read)} · "
        f"торговля {_tri_state(permission.can_perp_trade)} · "
        f"вывод средств {_tri_state(permission.can_withdraw)} · "
        f"ограничение по IP {_tri_state(permission.whitelist_ok)}\n"
        f"Счёт: {dto.account_masked or '—'}\n"
        f"Позиции: {dto.positions_count if dto.positions_count is not None else '—'}\n"
        f"Последняя проверка: {_when(dto.last_refresh_at)}"
    )
    notices = [_notice(dto.message), _notice(permission.warning)]
    if any(notice for notice in notices):
        text += "\n\n" + "\n".join(notice for notice in notices if notice)
    builder.button(text="↻ Обновить Aster", callback_data=_cb(MenuAction.STATUS_REFRESH, revision))
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 2)
    return MenuView(Screen.STATUS, text, builder.as_markup(), revision)


def render_risk(dto: RiskDTO, revision: int = 0) -> MenuView:
    builder = _builder()
    text = (
        "Риск\n\n"
        f"Риск на стоп: {_percent(dto.risk_percent)}\n"
        "Плечо: автоматически, максимум для актива\n"
        "Размер позиции рассчитывается по фактическому балансу и цене защитной стоп-заявки."
    )
    builder.button(
        text="Изменить риск", callback_data=_cb(MenuAction.RISK_EDIT, revision, key="risk")
    )
    builder.button(
        text="Быстрый риск: 1%",
        callback_data=_cb(MenuAction.RISK_PRESET, revision, key="risk", page=1),
    )
    builder.button(
        text="Быстрый риск: 2%",
        callback_data=_cb(MenuAction.RISK_PRESET, revision, key="risk", page=2),
    )
    builder.button(
        text="Быстрый риск: 5%",
        callback_data=_cb(MenuAction.RISK_PRESET, revision, key="risk", page=5),
    )
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 3, 2)
    return MenuView(Screen.RISK, text, builder.as_markup(), revision)


def render_activity(dto: ActivityPage, revision: int = 0) -> MenuView:
    builder = _builder()
    lines = ["Активность", ""]
    if not dto.items:
        lines.append("Пока нет безопасных событий.")
    else:
        for item in dto.items[:10]:
            if item.status.value == "completed":
                status_text = "Выполнено"
            elif item.status.value == "pending":
                if item.pending_age is not None:
                    status_text = f"Выполняется ({item.pending_age} сек)"
                else:
                    status_text = "Выполняется"
            else:
                status_text = "Ошибка"
            lines.append(f"{_when(item.occurred_at)} · {item.title} — {status_text}")
            if item.remediation:
                lines.append(f"  ↳ {item.remediation}")
    if dto.has_previous:
        builder.button(
            text="◀", callback_data=_cb(MenuAction.PAGE, revision, page=max(0, dto.page - 1))
        )
    if dto.has_next:
        builder.button(text="▶", callback_data=_cb(MenuAction.PAGE, revision, page=dto.page + 1))
    if dto.has_previous or dto.has_next:
        builder.adjust(2)
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(2)
    return MenuView(Screen.ACTIVITY, "\n".join(lines), builder.as_markup(), revision)


def render_notifications(dto: NotificationSettingsDTO, revision: int = 0) -> MenuView:
    builder = _builder()
    text = (
        "Уведомления\n\n"
        f"Состояние: {'включены' if dto.enabled else 'выключены'}\n"
        f"Последняя доставка: {_when(dto.last_delivery_at)}\n"
        f"Ожидают доставки: {dto.pending}\n"
        f"Неразрешённых ошибок: {dto.failed}"
    )
    label = "Выключить уведомления" if dto.enabled else "Включить уведомления"
    builder.button(text=label, callback_data=_cb(MenuAction.NOTIFICATIONS_TOGGLE, revision))
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 2)
    return MenuView(Screen.NOTIFICATIONS, text, builder.as_markup(), revision)


def render_help(revision: int = 0, security: bool = False) -> MenuView:
    builder = _builder()
    if security:
        text = (
            "Безопасность\n\n"
            "Подключение работает только с тестовой биржей Aster. Главный ключ "
            "кошелька не нужен. private key, показанный под API wallet address, "
            "вводится последним сообщением, не сохраняется в форме и удаляется после проверки. "
            "Telegram не гарантирует "
            "удаление из уведомлений, скриншотов и кэша."
        )
    else:
        text = (
            "Помощь\n\n"
            "Копирование повторяет состояние счёта-источника. Пауза "
            "останавливает новые входы, но закрытия и защитные действия "
            "продолжаются. Аварийное закрытие доступно только администратору."
        )
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(2)
    return MenuView(
        Screen.SECURITY if security else Screen.HELP, text, builder.as_markup(), revision
    )


def render_account(dto: DashboardDTO, revision: int = 0) -> MenuView:
    builder = _builder()
    text = (
        "Учётная запись\n\n"
        f"Счёт Aster: {dto.account_masked or '—'}\n"
        f"Ключ подключения: {dto.signer_masked or '—'}\n"
        "Изменения учётных данных требуют отдельного подтверждения."
    )
    builder.button(
        text="🔐 Заменить учётные данные", callback_data=_cb(MenuAction.REPLACE, revision)
    )
    builder.button(text="Отключить аккаунт", callback_data=_cb(MenuAction.DISCONNECT, revision))
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 1, 2)
    return MenuView(Screen.ACCOUNT, text, builder.as_markup(), revision)


def render_access_captcha(
    challenge: CaptchaChallenge,
    revision: int = 0,
    *,
    notice: str | None = None,
) -> MenuView:
    builder = _builder()
    text = (
        "Доступ к Aster Copy\n\n"
        "Перед регистрацией нужна короткая проверка. После правильного ответа "
        "заявка сама уйдёт администратору — Telegram ID пересылать не нужно.\n\n"
    )
    if notice:
        text += f"{notice}\n\n"
    text += f"Сколько будет {challenge.prompt}"
    for choice in challenge.choices:
        callback = GuestAccessCallback(
            action=GuestAccessAction.ANSWER,
            token=challenge.token,
            choice=choice,
        )
        callback.pack()
        builder.button(text=str(choice), callback_data=callback)
    builder.adjust(2, 2)
    return MenuView(Screen.ACCESS, text, builder.as_markup(), revision)


def render_access_pending(dto: AccessRequestDTO, revision: int = 0) -> MenuView:
    del dto
    builder = _builder()
    callback = GuestAccessCallback(action=GuestAccessAction.STATUS)
    callback.pack()
    builder.button(text="↻ Проверить статус", callback_data=callback)
    return MenuView(
        Screen.ACCESS,
        "Заявка отправлена\n\n"
        "Заявка сохранена и видна администратору в разделе «Заявки» админки. "
        "Повторно отправлять её не нужно. После решения бот откроет "
        "пользовательское меню.",
        builder.as_markup(),
        revision,
    )


def render_access_rejected(revision: int = 0) -> MenuView:
    builder = _builder()
    callback = GuestAccessCallback(action=GuestAccessAction.RETRY)
    callback.pack()
    builder.button(text="Пройти проверку снова", callback_data=callback)
    return MenuView(
        Screen.ACCESS,
        "Доступ пока не подтверждён\n\n"
        "Если это ошибка, через минуту можно пройти проверку ещё раз.",
        builder.as_markup(),
        revision,
    )


def render_access_wait(text: str, revision: int = 0) -> MenuView:
    return MenuView(Screen.ACCESS, text, _builder().as_markup(), revision)


def access_request_card(dto: AccessRequestDTO) -> tuple[str, InlineKeyboardMarkup]:
    builder = _builder()
    approve = AccessDecisionCallback(
        action=AccessDecisionAction.APPROVE,
        request_id=dto.request_id,
    )
    reject = AccessDecisionCallback(
        action=AccessDecisionAction.REJECT,
        request_id=dto.request_id,
    )
    approve.pack()
    reject.pack()
    builder.button(text="✅ Открыть доступ", callback_data=approve)
    builder.button(text="⛔ Отклонить", callback_data=reject)
    builder.adjust(2)
    identity = dto.display_name or "имя не указано"
    username = f"@{dto.username}" if dto.username else "не указан"
    requested = _when(dto.requested_at)
    text = (
        "Новая заявка на доступ\n\n"
        f"Пользователь: {identity}\n"
        f"Имя в Telegram: {username}\n"
        f"Telegram ID: {dto.user_id}\n"
        f"Получена: {requested}\n\n"
        "Проверка пройдена. Открыть пользовательское меню?"
    )
    return text, builder.as_markup()


def access_decision_text(dto: AccessRequestDTO, *, menu_sent: bool | None = None) -> str:
    identity = dto.display_name or (f"@{dto.username}" if dto.username else str(dto.user_id))
    if dto.status is AccessRequestStatus.APPROVED:
        delivery = (
            "Пользовательское меню отправлено."
            if menu_sent
            else "Пользователь может открыть меню командой /start."
        )
        return f"✅ Доступ открыт\n\n{identity}\n{delivery}"
    return f"⛔ Заявка отклонена\n\n{identity}"


def render_admin(dto: AdminDashboardDTO, revision: int = 0) -> MenuView:
    builder = _builder()
    text = (
        "Администрирование\n\n"
        f"Источник копирования: {dto.master_account_masked or 'основной счёт Aster'}\n"
        f"Система: {_system_label(dto.system_state)}\n"
        f"Глобальная пауза: {'да' if dto.global_pause else 'нет'}\n"
        f"Глобальная авария: {'да' if dto.global_emergency else 'нет'}\n"
        f"Счетов подписчиков: {dto.accounts_count}\n"
        f"Заявки на доступ: {dto.pending_access_requests}\n"
        f"Уведомления: ожидают {dto.outbox_pending}, ошибок {dto.outbox_failed}"
    )
    builder.button(
        text=f"🆕 Заявки · {dto.pending_access_requests}",
        callback_data=_cb(MenuAction.ADMIN_REQUESTS, revision),
    )
    builder.button(text="👥 Пользователи", callback_data=_cb(MenuAction.ADMIN_USERS, revision))
    builder.button(
        text="📋 Счета подписчиков",
        callback_data=_cb(MenuAction.ADMIN_FOLLOWERS, revision),
    )
    if dto.global_pause:
        builder.button(
            text="▶️ Снять глобальную паузу",
            callback_data=_cb(MenuAction.ADMIN_RESUME, revision),
        )
    else:
        builder.button(
            text="⏸ Поставить глобальную паузу",
            callback_data=_cb(MenuAction.ADMIN_PAUSE, revision),
        )
    builder.button(
        text="🚨 Глобально закрыть позиции",
        callback_data=_cb(MenuAction.ADMIN_EMERGENCY, revision),
    )
    builder.button(
        text="↻ Перепроверить работу", callback_data=_cb(MenuAction.ADMIN_RECONCILE, revision)
    )
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.BACK, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 1, 1, 1, 2)
    return MenuView(Screen.ADMIN, text, builder.as_markup(), revision)


def render_admin_users(dto: AdminUserPage, revision: int = 0) -> MenuView:
    builder = _builder()
    lines = [f"Пользователи · страница {dto.page + 1}", ""]
    if not dto.items:
        lines.append("Пока никому не выдан доступ.")
    for item in dto.items:
        account = "счёт подключён" if item.account_connected else "ожидает подключения"
        notifications = "уведомления включены" if item.notifications_enabled else "без уведомлений"
        access = "вход в меню открыт" if item.access_enabled else "вход в меню закрыт"
        admin = " · администратор" if item.is_admin else ""
        lines.append(f"{item.user_id} · {access}{admin}\n{account} · {notifications}")
        if not item.is_admin:
            builder.button(
                text=(
                    f"⛔ Закрыть вход {item.user_id}"
                    if item.access_enabled
                    else f"✅ Открыть вход {item.user_id}"
                ),
                callback_data=_cb(
                    MenuAction.ADMIN_USER_TOGGLE,
                    revision,
                    key=str(item.user_id),
                    page=dto.page,
                ),
            )
    builder.button(
        text="Резерв: добавить по ID",
        callback_data=_cb(MenuAction.ADMIN_USER_ADD, revision),
    )
    if dto.has_previous:
        builder.button(
            text="‹ Предыдущие",
            callback_data=_cb(MenuAction.ADMIN_USERS, revision, page=dto.page - 1),
        )
    if dto.has_next:
        builder.button(
            text="Следующие ›",
            callback_data=_cb(MenuAction.ADMIN_USERS, revision, page=dto.page + 1),
        )
    builder.button(text="‹ Админка", callback_data=_cb(MenuAction.ADMIN, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1)
    return MenuView(Screen.ADMIN_USERS, "\n".join(lines), builder.as_markup(), revision)


def render_admin_access_requests(
    dto: AdminAccessRequestPage,
    revision: int = 0,
) -> MenuView:
    builder = _builder()
    lines = [f"Заявки на доступ · страница {dto.page + 1}", ""]
    if not dto.items:
        lines.append("Новых заявок нет.")
    for item in dto.items:
        identity = item.display_name or (f"@{item.username}" if item.username else "без имени")
        username = f"@{item.username}" if item.username else "не указано"
        lines.append(
            f"{identity} · Имя в Telegram: {username}\n"
            f"Telegram ID: {item.user_id} · {_when(item.requested_at)}"
        )
        builder.button(
            text=f"✅ Одобрить #{item.request_id}",
            callback_data=AccessDecisionCallback(
                action=AccessDecisionAction.APPROVE,
                request_id=item.request_id,
            ),
        )
        builder.button(
            text=f"⛔ Отклонить #{item.request_id}",
            callback_data=AccessDecisionCallback(
                action=AccessDecisionAction.REJECT,
                request_id=item.request_id,
            ),
        )
    if dto.has_previous:
        builder.button(
            text="‹ Предыдущие",
            callback_data=_cb(MenuAction.ADMIN_REQUESTS, revision, page=dto.page - 1),
        )
    if dto.has_next:
        builder.button(
            text="Следующие ›",
            callback_data=_cb(MenuAction.ADMIN_REQUESTS, revision, page=dto.page + 1),
        )
    builder.button(text="‹ Админка", callback_data=_cb(MenuAction.ADMIN, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(*([2] * len(dto.items)), 1, 2)
    return MenuView(
        Screen.ADMIN_REQUESTS,
        "\n".join(lines),
        builder.as_markup(),
        revision,
    )


def render_admin_followers(dto: AdminFollowerPage, revision: int = 0) -> MenuView:
    builder = _builder()
    lines = [f"Счета подписчиков · страница {dto.page + 1}", ""]
    if not dto.items:
        lines.append("Подключённых счетов пока нет.")
    state_labels = {
        CopyState.ACTIVE: "🟢 копирование включено",
        CopyState.PAUSED: "⏸ новые входы на паузе",
        CopyState.DEGRADED: "🟠 нужна проверка",
        CopyState.BLOCKED: "🟠 заблокирован",
        CopyState.EMPTY: "⚪ не подключён",
    }
    for item in dto.items:
        owner = str(item.owner_user_id) if item.owner_user_id is not None else "без владельца"
        lines.append(
            f"{owner} · {item.account_masked or 'адрес скрыт'}\n"
            f"{state_labels[item.copy_state]} · риск {_percent(item.risk_percent)}"
        )
        builder.button(
            text=f"↻ Пересверить {owner}",
            callback_data=_cb(
                MenuAction.ADMIN_FOLLOWER_RECONCILE,
                revision,
                key=follower_selection_key(item.account_id),
                page=dto.page,
            ),
        )
    if dto.has_previous:
        builder.button(
            text="‹ Предыдущие",
            callback_data=_cb(MenuAction.ADMIN_FOLLOWERS, revision, page=dto.page - 1),
        )
    if dto.has_next:
        builder.button(
            text="Следующие ›",
            callback_data=_cb(MenuAction.ADMIN_FOLLOWERS, revision, page=dto.page + 1),
        )
    builder.button(text="‹ Админка", callback_data=_cb(MenuAction.ADMIN, revision))
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 2, 2)
    return MenuView(Screen.ADMIN_FOLLOWERS, "\n\n".join(lines), builder.as_markup(), revision)


def render_admin_user_confirmation(target_user_id: int, revision: int = 0) -> MenuView:
    builder = _builder()
    text = (
        "Разрешить доступ?\n\n"
        f"Telegram ID: {target_user_id}\n\n"
        "Пользователь сможет открыть бота и самостоятельно подключить свой "
        "тестовый Aster-счёт. Админские права ему не выдаются."
    )
    builder.button(
        text="Разрешить",
        callback_data=_cb(
            MenuAction.ADMIN_USER_ALLOW,
            revision,
            key=str(target_user_id),
        ),
    )
    builder.button(text="Отмена", callback_data=_cb(MenuAction.ADMIN_USERS, revision))
    builder.adjust(1, 1)
    return MenuView(Screen.CONFIRM, text, builder.as_markup(), revision)


def render_error(message: str, revision: int = 0) -> MenuView:
    builder = _builder()
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    return MenuView(Screen.HOME, f"Aster Copy\n\n{message}", builder.as_markup(), revision)


_ONBOARDING_ERROR_COPY: dict[OnboardingErrorCode, tuple[str, str]] = {
    OnboardingErrorCode.INVALID_INPUT: (
        "API wallet address и private key не являются одной парой.",
        "Возьмите API wallet address и private key, показанный под этим же адресом, "
        "и введите их заново.",
    ),
    OnboardingErrorCode.AGENT_SIGNER_MISMATCH: (
        "API wallet address и private key не являются одной парой.",
        "Используйте private key, показанный под тем же API wallet address; "
        "не смешивайте значения разных API wallet.",
    ),
    OnboardingErrorCode.AGENT_NOT_FOUND: (
        "API wallet сгенерирован, но ещё не авторизован обычным кошельком.",
        "Завершите авторизацию API wallet подписью обычного тестового кошелька, "
        "затем повторите проверку.",
    ),
    OnboardingErrorCode.AGENT_INVALID: (
        "API wallet не удалось проверить.",
        "Проверьте, что API wallet окончательно авторизован обычным кошельком, "
        "и повторите подключение.",
    ),
    OnboardingErrorCode.AGENT_READ_PERMISSION: (
        "Для API wallet не включено чтение.",
        "Включите чтение для API wallet и повторите подключение.",
    ),
    OnboardingErrorCode.AGENT_PERP_PERMISSION: (
        "Для API wallet не включено разрешение Perps.",
        "Откройте настройки создаваемого API wallet, включите Perps и повторите подключение.",
    ),
    OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION: (
        "Для API wallet включён вывод средств.",
        "Выключите вывод средств для API wallet и повторите подключение.",
    ),
    OnboardingErrorCode.AGENT_EXPIRED: (
        "Срок действия API wallet истёк или скоро закончится.",
        "Создайте новый API wallet или продлите действующий, затем повторите подключение.",
    ),
    OnboardingErrorCode.AGENT_IP_WHITELIST: (
        "Ограничения по IP для API wallet не соответствуют политике подключения.",
        "Проверьте ограничения по IP для API wallet и повторите подключение.",
    ),
    OnboardingErrorCode.SIGNED_READ_FAILED: (
        "Не удалось прочитать состояние тестового счёта.",
        "Проверьте авторизацию API wallet обычным кошельком и повторите попытку.",
    ),
    OnboardingErrorCode.ACCOUNT_NOT_FLAT: (
        "Счёт не пустой.",
        "Закройте все открытые позиции и отмените открытые ордера, затем повторите подключение.",
    ),
    OnboardingErrorCode.OPEN_ORDERS_PRESENT: (
        "На счёте есть открытые ордера.",
        "Отмените все открытые ордера и повторите подключение.",
    ),
    OnboardingErrorCode.POSITION_MODE_MISMATCH: (
        "В Aster testnet не включён Hedge Mode.",
        "Откройте Settings → Position mode → Hedge Mode, затем повторите подключение.",
    ),
    OnboardingErrorCode.OWNER_CONFLICT: (
        "У этого пользователя уже есть подключённый счёт.",
        "Используйте замену подключения для существующего счёта или сначала отключите его.",
    ),
    OnboardingErrorCode.USER_ADDRESS_CONFLICT: (
        "Этот адрес обычного тестового кошелька уже подключён.",
        "Используйте другой отдельный testnet-счёт или откройте замену существующего.",
    ),
    OnboardingErrorCode.MASTER_ADDRESS: (
        "Указан счёт-источник, а не отдельный счёт подписчика.",
        "Используйте другой отдельный обычный тестовый кошелёк для копирования.",
    ),
    OnboardingErrorCode.TESTNET_REQUIRED: (
        "Подключение разрешено только к Aster testnet.",
        "Откройте Aster testnet и повторите ввод данных этого testnet-счёта.",
    ),
    OnboardingErrorCode.INVALID_CONFIG: (
        "Проверка подключения временно недоступна.",
        "Повторите попытку позже. Если ошибка повторится, обратитесь к администратору.",
    ),
    OnboardingErrorCode.COMMIT_FAILED: (
        "Сохранение подключения не завершилось.",
        "Данные не считаются подтверждёнными. Повторите попытку через кнопку ниже.",
    ),
    OnboardingErrorCode.ACTIVATION_FAILED: (
        "Данные сохранены на паузе, но запуск подключения не завершился.",
        "Повторите попытку через кнопку ниже; бот безопасно попробует активацию ещё раз.",
    ),
}


def render_onboarding_error(
    code: OnboardingErrorCode | None,
    revision: int = 0,
    *,
    replacement: bool = False,
) -> MenuView:
    """Render a typed, secret-safe and recoverable onboarding failure."""
    problem, remedy = _ONBOARDING_ERROR_COPY.get(
        code,
        (
            "Проверка подключения временно не завершилась.",
            "Повторите попытку через кнопку ниже. Если ошибка повторится, попробуйте позже.",
        ),
    )
    heading = "Замена подключения не выполнена" if replacement else "Подключение не выполнено"
    builder = _builder()
    builder.button(
        text="🔄 Попробовать снова",
        callback_data=_cb(
            MenuAction.ONBOARDING_RETRY,
            revision,
            key="replace" if replacement else "connect",
        ),
    )
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 1)
    text = (
        f"{heading}\n\n{problem}\n\n"
        f"Что сделать:\n{remedy}\n\n"
        "private key не сохраняется в форме. Перед повтором подготовьте значения заново."
    )
    return MenuView(Screen.ONBOARDING_ERROR, text, builder.as_markup(), revision)


def render_onboarding_expired(
    revision: int = 0,
    *,
    replacement: bool = False,
) -> MenuView:
    """Render a clean recovery card after an onboarding session expires."""
    builder = _builder()
    builder.button(
        text="🔄 Попробовать снова",
        callback_data=_cb(
            MenuAction.ONBOARDING_RETRY,
            revision,
            key="replace" if replacement else "connect",
        ),
    )
    builder.button(text="⌂ На главную", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 1)
    heading = "Замена подключения прервана" if replacement else "Подключение прервано"
    text = (
        f"{heading}\n\n"
        "Сессия подключения истекла. Введённые значения не сохранены.\n\n"
        "Начните заново через кнопку ниже."
    )
    return MenuView(Screen.ONBOARDING_ERROR, text, builder.as_markup(), revision)


def render_onboarding_intro(revision: int = 0, *, replacement: bool = False) -> MenuView:
    """Show the short consent screen before collecting any credentials."""
    builder = _builder()
    builder.button(text="Понятно, продолжить", callback_data=_cb(MenuAction.CONSENT, revision))
    builder.button(text="Безопасность", callback_data=_cb(MenuAction.SECURITY, revision))
    builder.button(text="‹ Назад", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 2)
    heading = "Замена подключения к Aster" if replacement else "Подключение к Aster"
    text = (
        f"{heading}\n\n"
        "Перед подключением\n"
        "1. Откройте Aster testnet → Settings → Position mode → Hedge Mode.\n"
        "2. При создании Agent/API wallet выберите Perps (canPerpTrade) и чтение; "
        "вывод средств выключите.\n"
        "3. Окончательно авторизуйте API wallet подписью обычного кошелька.\n"
        "4. Проверьте: нет открытых позиций и ордеров.\n\n"
        "Далее бот запросит по одному:\n"
        "1. адрес обычного тестового кошелька;\n"
        "2. API wallet address;\n"
        "3. private key.\n\n"
        "Главный ключ обычного кошелька не нужен.\n\n"
        "Каждое из трёх сообщений с данными бот удаляет сразу после чтения, до проверки. "
        "Закрытый ключ будет зашифрован, но Telegram не гарантирует исчезновение сообщения "
        "из уведомлений, скриншотов и кэша.\n"
        "Этот способ предназначен только для тестовой биржи. После подключения "
        "копирование останется на паузе до проверки риска."
    )
    return MenuView(Screen.SECURITY, text, builder.as_markup(), revision)


def render_confirmation(confirmation: Confirmation, revision: int = 0) -> MenuView:
    """Render a generic, non-secret, single-use confirmation prompt."""
    try:
        confirmation_action = ConfirmationAction(confirmation.action)
    except (TypeError, ValueError):
        confirmation_action = None
    if confirmation_action is ConfirmationAction.EMERGENCY:
        return render_error("Аварийное закрытие доступно только администратору.", revision)
    builder = _builder()
    prompts = {
        "resume": (
            "Возобновить копирование?\n\n"
            "Новые входы снова станут доступны с текущими настройками риска. "
            "Закрытия и защитные действия продолжают работать.",
            "▶️ Подтвердить возобновление",
        ),
        "global_resume": (
            "Снять глобальную паузу?\n\n"
            "Новые входы снова станут доступны на счетах подписчиков с их настройками риска.",
            "Подтвердить снятие паузы",
        ),
        "disconnect": (
            "Отключить счёт Aster?\n\n"
            "Сначала проверим паузу, открытые позиции, наши заявки и незавершённые операции. "
            "Зашифрованные учётные данные будут удалены; журнал без секретов сохранится.",
            "Подтвердить отключение",
        ),
        "global_pause": (
            "Поставить глобальную паузу?\n\n"
            "Новые входы будут остановлены на всех счетах подписчиков. "
            "Закрытия, сокращения и защитные действия продолжат работать.",
            "Подтвердить глобальную паузу",
        ),
        "global_emergency": (
            "Глобально закрыть позиции?\n\n"
            "Новые входы будут остановлены. Позиции на всех счетах подписчиков будут "
            "закрыты, выставленные ботом защитные заявки отменены. Копирование останется на паузе.",
            "🚨 Подтвердить глобальное закрытие",
        ),
    }
    prompt, button_label = prompts.get(
        confirmation.action.value,
        ("Подтвердить операцию?", "Подтвердить"),
    )
    builder.button(
        text=button_label,
        callback_data=_cb(
            MenuAction.CONFIRM,
            revision,
            key=confirmation.action.value,
            token=confirmation.token,
        ),
    )
    builder.button(text="Отмена", callback_data=_cb(MenuAction.HOME, revision))
    builder.adjust(1, 1)
    return MenuView(
        Screen.CONFIRM,
        f"Подтверждение\n\n{prompt}\n\nПодтверждение одноразовое и имеет короткий срок действия.",
        builder.as_markup(),
        revision,
    )


def render_prompt(text: str, revision: int = 0) -> MenuView:
    """Render a short FSM prompt with only a safe cancel/home action."""
    builder = _builder()
    builder.button(text="Отмена", callback_data=_cb(MenuAction.HOME, revision))
    return MenuView(Screen.SECURITY, text, builder.as_markup(), revision)
