"""Safe, typed errors for the direct onboarding application service."""

from __future__ import annotations

from enum import StrEnum


class OnboardingErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_CONFIG = "invalid_config"
    TESTNET_REQUIRED = "testnet_required"
    OWNER_CONFLICT = "owner_conflict"
    USER_ADDRESS_CONFLICT = "user_address_conflict"
    MASTER_ADDRESS = "master_address"
    AGENT_NOT_FOUND = "agent_not_found"
    AGENT_INVALID = "agent_invalid"
    AGENT_SIGNER_MISMATCH = "agent_signer_mismatch"
    AGENT_READ_PERMISSION = "agent_read_permission"
    AGENT_PERP_PERMISSION = "agent_perp_permission"
    AGENT_WITHDRAW_PERMISSION = "agent_withdraw_permission"
    AGENT_EXPIRED = "agent_expired"
    AGENT_IP_WHITELIST = "agent_ip_whitelist"
    SIGNED_READ_FAILED = "signed_read_failed"
    ACCOUNT_NOT_FLAT = "account_not_flat"
    OPEN_ORDERS_PRESENT = "open_orders_present"
    POSITION_MODE_MISMATCH = "position_mode_mismatch"
    COMMIT_FAILED = "commit_failed"
    ACTIVATION_FAILED = "activation_failed"
    # Builder (Mini App) flow: the user's wallet signs, the backend verifies
    # and submits.  These codes stay secret-safe across the application
    # boundary exactly like the direct-flow codes above.
    BUILDER_CONFIG_MISSING = "builder_config_missing"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_UNKNOWN = "approval_unknown"
    APPROVAL_SIGNATURE_REQUIRED = "approval_signature_required"
    SIGNATURE_MISMATCH = "signature_mismatch"
    AGENT_SUBMIT_FAILED = "agent_submit_failed"
    APPROVAL_STATUS_UNKNOWN = "approval_status_unknown"
    AGENT_NOT_CONFIRMED = "agent_not_confirmed"
    BUILDER_NOT_CONFIRMED = "builder_not_confirmed"
    BUILDER_MISMATCH = "builder_mismatch"
    BUILDER_FEE_MISMATCH = "builder_fee_mismatch"
    IDENTITY_CHANGED = "identity_changed"
    REAUTHORIZE_UNAVAILABLE = "reauthorize_unavailable"


_MESSAGES: dict[OnboardingErrorCode, str] = {
    OnboardingErrorCode.INVALID_INPUT: (
        "API wallet address и private key должны быть одной парой."
    ),
    OnboardingErrorCode.INVALID_CONFIG: "Конфигурация onboarding недействительна.",
    OnboardingErrorCode.TESTNET_REQUIRED: "Direct onboarding разрешён только для Aster testnet.",
    OnboardingErrorCode.OWNER_CONFLICT: "У пользователя уже есть подключённый аккаунт.",
    OnboardingErrorCode.USER_ADDRESS_CONFLICT: (
        "Этот адрес обычного тестового кошелька уже подключён."
    ),
    OnboardingErrorCode.MASTER_ADDRESS: (
        "Этот адрес обычного тестового кошелька используется как источник."
    ),
    OnboardingErrorCode.AGENT_NOT_FOUND: (
        "API wallet сгенерирован, но не авторизован обычным кошельком."
    ),
    OnboardingErrorCode.AGENT_INVALID: "Ответ Agent недействителен или неполон.",
    OnboardingErrorCode.AGENT_SIGNER_MISMATCH: (
        "API wallet address и private key не являются одной парой."
    ),
    OnboardingErrorCode.AGENT_READ_PERMISSION: "Для API wallet не включено чтение.",
    OnboardingErrorCode.AGENT_PERP_PERMISSION: ("Для API wallet не включена perpetual trading."),
    OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION: "Для API wallet включён вывод средств.",
    OnboardingErrorCode.AGENT_EXPIRED: "Срок действия API wallet истёк или скоро закончится.",
    OnboardingErrorCode.AGENT_IP_WHITELIST: "IP restrictions API wallet не соответствуют политике.",
    OnboardingErrorCode.SIGNED_READ_FAILED: ("Не удалось безопасно прочитать testnet account."),
    OnboardingErrorCode.ACCOUNT_NOT_FLAT: (
        "Счёт не пустой: нужны нулевые позиции и отсутствие открытых ордеров."
    ),
    OnboardingErrorCode.OPEN_ORDERS_PRESENT: "На счёте есть открытые ордера.",
    OnboardingErrorCode.POSITION_MODE_MISMATCH: "Режим счёта не Hedge.",
    OnboardingErrorCode.COMMIT_FAILED: "Не удалось атомарно сохранить onboarding.",
    OnboardingErrorCode.ACTIVATION_FAILED: (
        "Аккаунт сохранён на паузе; runtime activation требует повтора."
    ),
    OnboardingErrorCode.BUILDER_CONFIG_MISSING: (
        "Builder-подключение временно недоступно: конфигурация билдера не готова."
    ),
    OnboardingErrorCode.APPROVAL_EXPIRED: (
        "Заявка на авторизацию истекла. Начните подключение заново."
    ),
    OnboardingErrorCode.APPROVAL_UNKNOWN: (
        "Заявка на авторизацию не найдена. Начните подключение заново."
    ),
    OnboardingErrorCode.APPROVAL_SIGNATURE_REQUIRED: (
        "Для этой заявки ещё требуется подпись кошелька."
    ),
    OnboardingErrorCode.SIGNATURE_MISMATCH: (
        "Подпись выполнена другим кошельком. Подключите тот же кошелёк и повторите."
    ),
    OnboardingErrorCode.AGENT_SUBMIT_FAILED: (
        "Aster не принял авторизацию Agent. Попробуйте ещё раз."
    ),
    OnboardingErrorCode.APPROVAL_STATUS_UNKNOWN: (
        "Aster ещё не подтвердил результат авторизации. Повторите проверку без новой подписи."
    ),
    OnboardingErrorCode.AGENT_NOT_CONFIRMED: (
        "Aster не подтвердил авторизацию Agent после подписи."
    ),
    OnboardingErrorCode.BUILDER_NOT_CONFIRMED: (
        "Aster не подтвердил авторизацию Builder после подписи."
    ),
    OnboardingErrorCode.BUILDER_MISMATCH: ("Подтверждённый Builder не совпадает с ожидаемым."),
    OnboardingErrorCode.BUILDER_FEE_MISMATCH: (
        "Подтверждённая комиссия Builder не совпадает с согласованной."
    ),
    OnboardingErrorCode.IDENTITY_CHANGED: (
        "Аккаунт изменился во время проверки. Повторите действие."
    ),
    OnboardingErrorCode.REAUTHORIZE_UNAVAILABLE: ("Переавторизация недоступна для этого аккаунта."),
}


class OnboardingError(Exception):
    """An operator-safe failure with no exchange or credential payload."""

    def __init__(
        self,
        code: OnboardingErrorCode,
        *,
        exchange_code: int | None = None,
    ) -> None:
        self.code = code
        self.exchange_code = exchange_code
        self.message = _MESSAGES[code]
        # Never append the raw exchange message: it may echo request material.
        super().__init__(self.message)

    def __repr__(self) -> str:
        suffix = "" if self.exchange_code is None else f", exchange_code={self.exchange_code}"
        return f"OnboardingError(code={self.code.value!r}{suffix})"


__all__ = ["OnboardingError", "OnboardingErrorCode"]
