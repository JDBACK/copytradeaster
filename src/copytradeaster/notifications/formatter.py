"""High-craft Russian formatting for trading and system notifications."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from copytradeaster.domain.models import ActionKind, ExecutionAction, PosSide, SkipReason


def _format_decimal(value: Any, *, max_decimals: int = 8) -> str:
    """Format decimal numbers cleanly with thousands separators."""
    if value is None:
        return "—"
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)

    if dec == dec.to_integral():
        return f"{int(dec):,}".replace(",", " ")

    text = f"{dec:,.{max_decimals}f}".replace(",", " ")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _pos_side_label(side: PosSide | str | None) -> str:
    if side is None:
        return ""
    normalized = str(side).upper()
    if normalized == "LONG":
        return "LONG"
    if normalized == "SHORT":
        return "SHORT"
    return ""


def format_confirmed_action(
    action: ExecutionAction,
    *,
    account_id: str | None = None,
) -> str:
    """Format an executed trading command into a clean trader-facing message."""
    del account_id
    kind = action.kind
    symbol = action.symbol
    payload = action.payload or {}

    # 1. Market Open
    if kind == ActionKind.MARKET_OPEN:
        pos_side = payload.get("positionSide") or action.position_side.value
        side_label = _pos_side_label(pos_side)
        side_suffix = f" ({side_label})" if side_label else ""
        qty = payload.get("quantity") or payload.get("origQty")
        qty_formatted = _format_decimal(qty)

        lines = [f"🟢 Открыта позиция {symbol}{side_suffix}"]
        if qty_formatted != "—":
            lines.append(f"Объём: {qty_formatted}")
        return "\n".join(lines)

    # 2. Market Close
    if kind == ActionKind.MARKET_CLOSE:
        pos_side = payload.get("positionSide") or action.position_side.value
        side_label = _pos_side_label(pos_side)
        side_suffix = f" ({side_label})" if side_label else ""
        qty = payload.get("quantity") or payload.get("origQty")
        qty_formatted = _format_decimal(qty)

        lines = [f"🔴 Закрыта позиция {symbol}{side_suffix}"]
        if qty_formatted != "—":
            lines.append(f"Объём: {qty_formatted}")
        return "\n".join(lines)

    # 3. Stop-Loss
    if kind == ActionKind.PLACE_STOP:
        stop_price = payload.get("stopPrice") or payload.get("price")
        price_formatted = _format_decimal(stop_price)
        return f"🛡 Установлен защитный стоп {symbol}\nУровень стопа: {price_formatted} USDT"

    # 4. Take-Profit
    if kind == ActionKind.PLACE_TP:
        tp_price = payload.get("stopPrice") or payload.get("price")
        price_formatted = _format_decimal(tp_price)
        return f"🎯 Установлен тейк-профит {symbol}\nУровень тейка: {price_formatted} USDT"

    # 5. Leverage
    if kind == ActionKind.SET_LEVERAGE:
        leverage = payload.get("leverage") or "—"
        return f"⚡️ Плечо {symbol} установлено: {leverage}x"

    # 6. Cancel Order
    if kind == ActionKind.CANCEL_ORDER:
        return f"🗑 Снят ордер {symbol}"

    safe_kind = str(kind.value if hasattr(kind, "value") else kind)
    return f"Команда {safe_kind} для {symbol} подтверждена."


def format_skip_reason(
    symbol: str,
    reason: SkipReason | str,
    pos_side: PosSide | str | None = None,
    *,
    account_id: str | None = None,
) -> str:
    """Format an actionable skip notice for the follower chat."""
    del account_id
    if isinstance(reason, str):
        try:
            reason_enum = SkipReason(reason)
        except ValueError:
            reason_enum = None
    else:
        reason_enum = reason

    side_label = _pos_side_label(pos_side)
    side_suffix = f" ({side_label})" if side_label else ""

    if reason_enum == SkipReason.NO_STOP:
        return f"⏳ Ожидание защитного стопа {symbol}{side_suffix}."

    if reason_enum == SkipReason.INSUFFICIENT_MARGIN:
        return (
            f"⚠️ Пропуск сделки {symbol}{side_suffix}\n"
            "Причина: недостаточно свободной маржи на балансе."
        )

    if reason_enum in (SkipReason.BELOW_MIN_QTY, SkipReason.BELOW_MIN_NOTIONAL):
        return (
            f"⚠️ Пропуск сделки {symbol}{side_suffix}\n"
            "Причина: объём позиции ниже минимального лимита биржи."
        )

    if reason_enum == SkipReason.ENTRY_DEVIATION:
        return (
            f"⚠️ Пропуск сделки {symbol}{side_suffix}\n"
            "Причина: цена входа слишком сильно отклонилась от мастера."
        )

    if reason_enum == SkipReason.PAUSED:
        return (
            f"⏸ Пропуск сделки {symbol}{side_suffix}\n"
            "Причина: копирование приостановлено в настройках."
        )

    if reason_enum == SkipReason.DISABLED:
        return f"⏸ Пропуск сделки {symbol}{side_suffix}\nПричина: аккаунт отключён."

    if reason_enum == SkipReason.OWN_PROTECTIVE_FILLED:
        return (
            f"ℹ️ Пропуск сделки {symbol}{side_suffix}\n"
            "Причина: защитный стоп в текущем эпизоде уже исполнен."
        )

    if reason_enum == SkipReason.STALE_MASTER:
        return f"⚠️ Пропуск сделки {symbol}{side_suffix}\nПричина: сигнал мастера устарел."

    if reason_enum == SkipReason.INTENTIONAL_FLAT:
        return f"ℹ️ Фиксация позиции {symbol}{side_suffix}."

    safe_reason = str(reason.value if hasattr(reason, "value") else reason)
    return f"⚠️ Пропуск {symbol}{side_suffix}: {safe_reason}."


__all__ = [
    "format_confirmed_action",
    "format_skip_reason",
]
