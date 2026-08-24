from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from copytradeaster.application.models import (
    ActivityItem,
    ActivityPage,
    AdminDashboardDTO,
    AdminFollowerItem,
    AdminFollowerPage,
    AdminUserItem,
    AdminUserPage,
    Confirmation,
    ConfirmationAction,
    CopyState,
    DashboardDTO,
    NotificationSettingsDTO,
    PermissionSummary,
    RiskDTO,
    StatusDTO,
    SystemState,
)
from copytradeaster.onboarding.errors import OnboardingErrorCode
from copytradeaster.telegram.menu import (
    MenuAction,
    MenuCallback,
    MenuView,
    callback_bytes,
    render_account,
    render_activity,
    render_admin,
    render_admin_followers,
    render_admin_user_confirmation,
    render_admin_users,
    render_confirmation,
    render_dashboard,
    render_error,
    render_help,
    render_notifications,
    render_onboarding_error,
    render_onboarding_expired,
    render_onboarding_intro,
    render_risk,
    render_status,
)


def _actions(view) -> set[MenuAction]:
    return {
        MenuCallback.unpack(button.callback_data).action
        for row in view.markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }


def test_empty_dashboard_exposes_admin_only_for_admin() -> None:
    regular = render_dashboard(DashboardDTO(copy_state=CopyState.EMPTY))
    admin = render_dashboard(DashboardDTO(copy_state=CopyState.EMPTY, is_admin=True))

    assert MenuAction.ADMIN not in _actions(regular)
    assert MenuAction.ADMIN in _actions(admin)
    assert _actions(admin) >= {
        MenuAction.CONNECT,
        MenuAction.HOW,
        MenuAction.SECURITY,
    }
    assert "Вы управляете счётом-источником" in admin.text


def test_status_renders_permissions_as_tri_state_and_surfaces_safe_notices() -> None:
    view = render_status(
        StatusDTO(
            system_state=SystemState.DEGRADED,
            copy_state=CopyState.BLOCKED,
            aster_connected=True,
            permissions=PermissionSummary(
                can_read=None,
                can_perp_trade=True,
                can_withdraw=False,
                whitelist_ok=None,
                warning="Проверка прав устарела.",
            ),
            message="Показаны последние сохранённые данные.",
        )
    )

    assert "чтение неизвестно" in view.text
    assert "торговля да" in view.text
    assert "вывод средств нет" in view.text
    assert "ограничение по IP неизвестно" in view.text
    assert "Проверка прав устарела." in view.text
    assert "Показаны последние сохранённые данные." in view.text


def test_empty_dashboard_surfaces_notice_as_plain_bounded_text() -> None:
    view = render_dashboard(
        DashboardDTO(
            copy_state=CopyState.EMPTY,
            message="<b>Связь временно недоступна</b>.",
            permission=PermissionSummary(warning="Проверьте статус позже."),
            risk_percent=Decimal("0.01"),
        )
    )

    assert "<b>Связь временно недоступна</b>." in view.text
    assert "Проверьте статус позже." in view.text
    assert len(view.text) <= 4096

    translated = render_dashboard(
        DashboardDTO(
            copy_state=CopyState.PAUSED,
            message="global pause is active",
            permission=PermissionSummary(warning="permission_check_pending"),
        )
    )
    assert "Включена общая пауза новых входов." in translated.text
    assert "Проверка прав подключения ещё не завершена." in translated.text
    assert "permission_check" not in translated.text


def test_follower_ui_exposes_only_risk_and_exchange_managed_leverage() -> None:
    risk = render_risk(
        RiskDTO(
            risk_percent=Decimal("0.01"),
            max_leverage=20,
            max_entry_deviation_pct=Decimal("0.02"),
        )
    )
    account = render_account(
        DashboardDTO(
            copy_state=CopyState.ACTIVE,
            account_id="copyc1",
            account_masked="0x1234…abcd",
            signer_masked="0xabcd…1234",
        )
    )
    callback_keys = {
        MenuCallback.unpack(button.callback_data).key
        for row in risk.markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }

    assert "Плечо: автоматически, максимум для актива" in risk.text
    assert "Маржа" not in risk.text
    assert "Изменить плечо" not in risk.text
    assert "Изменить отклонение" not in risk.text
    assert callback_keys.isdisjoint({"leverage", "deviation"})
    assert "Аварийно" not in account.text


def test_confirmation_renderer_keeps_documented_token_within_callback_budget() -> None:
    # The transport contract documents opaque confirmation tokens as at most
    # 16 ASCII characters; exercise the real renderer boundary, not service
    # internals, so a future token implementation can remain independent.
    token = "t" * 16
    view = render_confirmation(
        Confirmation(
            token=token,
            action=ConfirmationAction.GLOBAL_EMERGENCY,
            account_id=None,
        ),
        revision=999,
    )
    callbacks = [
        MenuCallback.unpack(button.callback_data)
        for row in view.markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    confirmation_callback = next(
        callback for callback in callbacks if callback.action is MenuAction.CONFIRM
    )

    assert len(token) == 16
    assert confirmation_callback.token == token
    assert callback_bytes(confirmation_callback) <= 64


def test_admin_confirmation_copy_states_scope_and_consequence() -> None:
    view = render_confirmation(
        Confirmation(
            token="t" * 16,
            action=ConfirmationAction.GLOBAL_EMERGENCY,
            account_id=None,
        )
    )

    assert "всех счетах подписчиков" in view.text
    assert "Копирование останется на паузе" in view.text
    assert (
        "глобальное закрытие"
        in next(
            button.text
            for row in view.markup.inline_keyboard
            for button in row
            if button.callback_data
            and MenuCallback.unpack(button.callback_data).action is MenuAction.CONFIRM
        ).lower()
    )


def test_renderers_cover_degraded_activity_notifications_help_and_admin_resume() -> None:
    degraded = render_dashboard(
        DashboardDTO(
            copy_state=CopyState.DEGRADED,
            system_state=SystemState.STALE,
            account_masked="0x1234…abcd",
            signer_masked="0xabcd…1234",
            risk_percent=Decimal("0.015"),
            max_leverage=7,
            open_positions=2,
            updated_at=datetime(2026, 8, 24, 9, 30, tzinfo=UTC),
            is_admin=True,
        )
    )
    assert "Нужна проверка" in degraded.text
    assert MenuAction.STATUS_REFRESH in _actions(degraded)
    assert MenuAction.ADMIN in _actions(degraded)

    activity = render_activity(
        ActivityPage(
            items=(
                ActivityItem(
                    occurred_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
                    action="pause",
                    result="ok",
                    detail="safe detail",
                ),
            ),
            page=2,
            has_previous=True,
            has_next=True,
        ),
        revision=2,
    )
    assert "safe detail" in activity.text
    assert MenuAction.PAGE in _actions(activity)

    empty_activity = render_activity(ActivityPage())
    assert "Пока нет безопасных событий." in empty_activity.text

    notifications = render_notifications(
        NotificationSettingsDTO(enabled=False, pending=3, failed=1)
    )
    assert "выключены" in notifications.text
    assert "Включить уведомления" in {
        button.text for row in notifications.markup.inline_keyboard for button in row
    }

    security = render_help(security=True)
    assert security.screen.value == "security"
    assert "private key, показанный под API wallet address" in security.text

    admin = render_admin(AdminDashboardDTO(global_pause=True, global_emergency=False))
    assert "▶️ Снять глобальную паузу" in {
        button.text for row in admin.markup.inline_keyboard for button in row
    }
    assert MenuAction.ADMIN_RESUME in _actions(admin)
    assert MenuAction.ADMIN_USERS in _actions(admin)
    assert MenuAction.ADMIN_FOLLOWERS in _actions(admin)


def test_admin_user_and_follower_screens_are_plain_bounded_and_actionable() -> None:
    users = render_admin_users(
        AdminUserPage(
            items=(AdminUserItem(123456789, account_connected=True),),
            page=1,
            has_previous=True,
            has_next=True,
        )
    )
    assert "123456789 · вход в меню открыт" in users.text
    assert "счёт подключён" in users.text
    assert {
        MenuAction.ADMIN_USER_ADD,
        MenuAction.ADMIN_USER_TOGGLE,
        MenuAction.ADMIN_USERS,
        MenuAction.ADMIN,
    } <= _actions(users)

    followers = render_admin_followers(
        AdminFollowerPage(
            items=(
                AdminFollowerItem(
                    account_id="tg-7",
                    owner_user_id=7,
                    account_masked="0x1234…abcd",
                    copy_state=CopyState.ACTIVE,
                    risk_percent=Decimal("0.01"),
                ),
            )
        )
    )
    assert "0x1234…abcd" in followers.text
    assert "tg-7" not in followers.text
    assert MenuAction.ADMIN_FOLLOWER_RECONCILE in _actions(followers)

    confirmation = render_admin_user_confirmation(123456789)
    assert "Админские права ему не выдаются" in confirmation.text
    assert MenuAction.ADMIN_USER_ALLOW in _actions(confirmation)


def test_onboarding_intro_uses_a_user_visible_aster_vocabulary() -> None:
    view = render_onboarding_intro()

    assert "Перед подключением" in view.text
    assert "Aster testnet → Settings → Position mode → Hedge Mode" in view.text
    assert "Perps" in view.text
    assert "адрес обычного тестового кошелька" in view.text
    assert "API wallet address" in view.text
    assert "private key" in view.text
    assert "авторизуйте API wallet подписью обычного кошелька" in view.text
    assert "Perps (canPerpTrade) и чтение" in view.text
    assert "вывод средств выключите" in view.text
    assert "нет открытых позиций и ордеров" in view.text
    assert "Маржа" not in view.text
    assert "margin" not in view.text.casefold()
    assert "каждое из трёх сообщений с данными бот удаляет сразу после чтения, до проверки" in (
        view.text.casefold()
    )
    assert "□" not in view.text
    assert "User" not in view.text
    assert "Signer" not in view.text
    assert "flat" not in view.text.casefold()


@pytest.mark.parametrize("code", list(OnboardingErrorCode))
def test_onboarding_error_renderer_is_typed_actionable_and_secret_safe(
    code: OnboardingErrorCode,
) -> None:
    view = render_onboarding_error(code, revision=7)
    actions = _actions(view)

    assert view.screen.value == "onboarding_error"
    assert actions >= {MenuAction.ONBOARDING_RETRY, MenuAction.HOME}
    assert "🔄 Попробовать снова" in {
        button.text for row in view.markup.inline_keyboard for button in row
    }
    assert "⌂ На главную" in {button.text for row in view.markup.inline_keyboard for button in row}
    assert "User" not in view.text
    assert "Signer" not in view.text
    assert "flat" not in view.text.casefold()
    assert "private-key-value" not in view.text
    assert "0x1111111111111111111111111111111111111111" not in view.text
    assert "exchange" not in view.text.casefold()


def test_typed_onboarding_errors_use_exact_aster_labels() -> None:
    perps = render_onboarding_error(OnboardingErrorCode.AGENT_PERP_PERMISSION)
    assert "Perps" in perps.text
    assert "настройки создаваемого API wallet" in perps.text
    assert "perpetual trading" not in perps.text

    hedge = render_onboarding_error(OnboardingErrorCode.POSITION_MODE_MISMATCH)
    assert "Settings → Position mode → Hedge Mode" in hedge.text


def test_onboarding_expired_renderer_is_a_clean_recovery_card() -> None:
    view = render_onboarding_expired(revision=4, replacement=True)

    assert view.screen.value == "onboarding_error"
    assert "Сессия подключения истекла" in view.text
    assert "Введённые значения не сохранены" in view.text
    assert "🔄 Попробовать снова" in {
        button.text for row in view.markup.inline_keyboard for button in row
    }
    retry = next(
        MenuCallback.unpack(button.callback_data)
        for row in view.markup.inline_keyboard
        for button in row
        if button.callback_data
        and MenuCallback.unpack(button.callback_data).action is MenuAction.ONBOARDING_RETRY
    )
    assert retry.key == "replace"
    assert retry.revision == 4


def test_onboarding_error_renderer_keeps_replacement_mode_in_retry_callback() -> None:
    view = render_onboarding_error(
        OnboardingErrorCode.AGENT_NOT_FOUND,
        revision=11,
        replacement=True,
    )
    callback = next(
        MenuCallback.unpack(button.callback_data)
        for row in view.markup.inline_keyboard
        for button in row
        if button.callback_data
        and MenuCallback.unpack(button.callback_data).action is MenuAction.ONBOARDING_RETRY
    )

    assert "Замена подключения не выполнена" in view.text
    assert callback.key == "replace"
    assert callback.revision == 11


def test_menu_engine_defaults_and_text_limits_are_deterministic() -> None:
    from copytradeaster.telegram.menu import MenuEngine, MenuSession, MenuSessionStore, Screen

    long = MenuView(Screen.HOME, "x" * 5000, render_error("ok").markup, revision=1)
    assert len(long.text) == 4094

    engine = MenuEngine()
    # No application means every screen uses its safe DTO default; this also
    # exercises all presenter/application boundaries without a Bot API call.
    import asyncio

    dashboard, status, risk, activity, notifications, admin = asyncio.run(
        _render_default_screens(engine)
    )
    assert dashboard.screen is Screen.HOME
    assert status.screen is Screen.STATUS
    assert risk.screen is Screen.RISK
    assert activity.screen is Screen.ACTIVITY
    assert notifications.screen is Screen.NOTIFICATIONS
    assert admin.screen is Screen.ADMIN

    store = MenuSessionStore()
    session = MenuSession(1, 2, 3, Screen.HOME, 4, datetime.now(UTC))
    asyncio.run(store.remember(session))
    assert asyncio.run(store.accept_revision(1, 2, 3, 4))
    assert not asyncio.run(store.accept_revision(1, 2, 3, 5))


async def _render_default_screens(engine):
    return (
        await engine.dashboard(1),
        await engine.status(1),
        await engine.risk(1),
        await engine.activity(1),
        await engine.notifications(1),
        await engine.admin(1),
    )
