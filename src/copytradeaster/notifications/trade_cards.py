"""Shared user-event presenter: one meaning for push, menu and Mini App.

Pure rendering over immutable :class:`StoredEvent` facts. The backend
counts and proves; this module only turns proven facts into Russian
plain-text lines following the §7 examples. No emoji, no BUY/SELL-derived
result signs, no invented units or links: a missing fact omits its line.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from copytradeaster.storage.events import UserEvent

_OPEN_TRADE_ACTION = "open_trade"

# Engine skip codes turn into plain copy here: the feed is read by the owner, and
# a raw enum (``STALE_MASTER``) means nothing outside the code.  ``INTENTIONAL_FLAT``
# is deliberately absent - a dust flat inside an open episode is not a skip.
_SKIP_REASON_COPY: dict[str, str] = {
    "NO_STOP": "защитный стоп ещё не рассчитан",
    "BELOW_MIN_QTY": "объём меньше минимального лота биржи",
    "BELOW_MIN_NOTIONAL": "объём меньше минимальной суммы ордера",
    "INSUFFICIENT_MARGIN": "недостаточно свободной маржи",
    "ENTRY_DEVIATION": "цена входа слишком далеко ушла от мастера",
    "DISABLED": "аккаунт отключён",
    "PAUSED": "копирование приостановлено",
    "STALE_MASTER": "сигнал мастера устарел",
    "OWN_PROTECTIVE_FILLED": "защитный стоп в этой сделке уже исполнен",
    "EXTERNAL_FOLLOWER_CLOSE": (
        "позиция закрыта вне копитрейдинга; повторный вход отключён до следующей сделки мастера"
    ),
    "UPGRADE_FLAT_CAUSE_UNKNOWN": (
        "причина закрытия до обновления не подтверждена; повторный вход отключён"
    ),
    "LEVERAGE_NOT_READY": "плечо для символа ещё не подготовлено",
    "MARGIN_TYPE_UNKNOWN": "режим маржи мастера не подтверждён",
    "MARGIN_TYPE_MISMATCH": "режим маржи отличается; увеличение позиции остановлено",
}

_SKIP_FLAT_REASON = "INTENTIONAL_FLAT"


def _num(value: Any) -> str | None:
    """Canonical fact string with thousands grouping; None stays missing."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not dec.is_finite():
        return None
    if dec == dec.to_integral_value():
        grouped = f"{int(dec):,}".replace(",", " ")
    else:
        raw = format(dec, "f")
        head, _, tail = raw.partition(".")
        sign = "-" if dec < 0 else ""
        grouped = sign + f"{abs(int(head)):,}".replace(",", " ") + "." + tail
        grouped = grouped.rstrip("0").rstrip(".")
    return grouped.replace(".", ",")


def _side_suffix(facts: dict[str, Any]) -> str:
    side = str(facts.get("side") or "").upper()
    if side in ("LONG", "SHORT"):
        return f" · {side}"
    return ""


def _paren_side(facts: dict[str, Any]) -> str:
    """`` (LONG)`` for skip facts, which carry the side as ``pos_side``."""
    side = str(facts.get("side") or facts.get("pos_side") or "").upper()
    return f" ({side})" if side in ("LONG", "SHORT") else ""


def _with_asset(qty: str | None, asset: str | None) -> str | None:
    if qty is None:
        return None
    return f"{qty} {asset}" if asset else qty


def _duration(duration_ms: Any) -> str | None:
    try:
        total_seconds = int(duration_ms) // 1000
    except (TypeError, ValueError):
        return None
    if total_seconds < 0:
        return None
    days, rest = divmod(total_seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, _ = divmod(rest, 60)
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{total_seconds} с"


def _slot_word(slot: Any) -> str:
    return "Тейк-профит" if str(slot) == "take_profit" else "Стоп"


def format_user_event(
    event: UserEvent,
    *,
    quantity_asset: str | None = None,
    money: Any | None = None,
) -> str:
    """Render one stored user event as Russian plain text.

    ``quantity_asset`` supplies the base-asset unit when the caller knows
    it (service layer); without it quantities render unitless rather than
    guessed. ``money`` is the optional proven :class:`EpisodeMoneyView` of the
    event's episode: a closed trade prints one net result when every cost is
    proven, and says so when they are not — never a gross figure the reader
    would take for final. Raises ValueError on an unknown kind.
    """
    facts = dict(event.facts)
    quantity_asset = quantity_asset or facts.get("quantity_asset")
    symbol = str(facts.get("symbol") or "—")
    kind = event.kind
    if kind == "POSITION_OPENED":
        lines = [f"Открыта {symbol}{_side_suffix(facts)}"]
        qty = _with_asset(_num(facts.get("entry_qty")), quantity_asset)
        if qty is not None:
            lines.append(f"Объём: {qty}")
        price = _num(facts.get("entry_price"))
        if price is not None:
            lines.append(f"Средняя цена исполнения: {price}")
        return "\n".join(lines)
    if kind == "POSITION_INCREASED":
        lines = [f"Увеличена {symbol}{_side_suffix(facts)}"]
        qty = _with_asset(_num(facts.get("added_qty")), quantity_asset)
        price = _num(facts.get("price"))
        if qty is not None and price is not None:
            lines.append(f"Добавлено: {qty} по {price}")
        elif qty is not None:
            lines.append(f"Добавлено: {qty}")
        remaining = _with_asset(_num(facts.get("remaining_qty")), quantity_asset)
        if remaining is not None:
            lines.append(f"Осталось: {remaining}")
        return "\n".join(lines)
    if kind == "POSITION_REDUCED":
        lines = [f"Сокращена {symbol}{_side_suffix(facts)}"]
        qty = _with_asset(_num(facts.get("closed_qty")), quantity_asset)
        price = _num(facts.get("close_price"))
        if qty is not None and price is not None:
            lines.append(f"Закрыто: {qty} по {price}")
        elif qty is not None:
            lines.append(f"Закрыто: {qty}")
        remaining = _with_asset(_num(facts.get("remaining_qty")), quantity_asset)
        if remaining is not None:
            lines.append(f"Осталось: {remaining}")
        gross = _num(facts.get("realized_gross"))
        if gross is not None:
            # The closed share keeps its own gross: an episode-level net would
            # also absorb the entry costs of the still-open remainder.
            lines.append(f"PnL закрытой части: {gross} USDT до комиссий")
        return "\n".join(lines)
    if kind == "POSITION_CLOSED":
        lines = [f"Закрыта {symbol}{_side_suffix(facts)}"]
        entry = _num(facts.get("entry_price"))
        if entry is not None:
            lines.append(f"Средний вход: {entry}")
        exit_price = _num(facts.get("exit_price"))
        if exit_price is not None:
            lines.append(f"Средний выход: {exit_price}")
        # One result per trade: the proven net replaces the gross headline, and
        # an unproven composition says so instead of printing gross as final.
        if money is not None and getattr(money, "result_state", None) == "net_complete":
            net = _num(getattr(money, "net", None))
            if net is not None:
                lines.append(f"Итог сделки: {net} USDT")
        elif money is not None:
            lines.append("Итог уточняется: начисления ещё сверяются")
        else:
            gross = _num(facts.get("realized_gross"))
            if gross is not None:
                lines.append(f"PnL до комиссий: {gross} USDT")
            else:
                lines.append("Результат уточняется")
        duration = _duration(facts.get("duration_ms"))
        if duration is not None:
            lines.append(f"В позиции: {duration}")
        return "\n".join(lines)
    if kind in ("PROTECTION_SET", "PROTECTION_CHANGED", "PROTECTION_UNCONFIRMED"):
        slot = _slot_word(facts.get("slot"))
        if kind == "PROTECTION_SET":
            price = _num(facts.get("new_price"))
            suffix = f": {price}" if price is not None else ""
            return f"{slot} {symbol} установлен{suffix}"
        if kind == "PROTECTION_CHANGED":
            old = _num(facts.get("old_price"))
            new = _num(facts.get("new_price"))
            if old is not None and new is not None:
                return f"{slot} {symbol} изменён: {old} → {new}"
            if new is not None:
                return f"{slot} {symbol} изменён: {new}"
            return f"{slot} {symbol} изменён"
        return f"{slot} {symbol} не подтверждён"
    if kind == "RISK_CHANGED":
        new = _percent(facts.get("risk_percent"))
        old = _percent(facts.get("previous_percent"))
        if new is not None and old is not None:
            return f"Риск на сделку изменён: {old} → {new}"
        if new is not None:
            return f"Риск на сделку: {new}"
        return "Риск на сделку изменён"
    if kind == "COPY_PAUSED":
        return "Копирование приостановлено. Открытые позиции и защита продолжают работать."
    if kind == "COPY_RESUMED":
        return "Копирование возобновлено."
    if kind == "CONNECTIVITY_LOST":
        return "Связь с биржей потеряна. Проверяем состояние позиций."
    if kind == "CONNECTIVITY_RESTORED":
        return "Связь с биржей восстановлена."
    if kind == "HISTORY_CORRECTED":
        return "История сделки уточнена. Откройте детали для актуального результата."
    if kind == "ENTRY_SKIPPED":
        reason = str(facts.get("reason") or "").strip()
        if reason == _SKIP_FLAT_REASON:
            return f"Фиксация позиции {symbol}{_paren_side(facts)}"
        lines = [f"Вход {symbol}{_paren_side(facts)} пропущен"]
        reason_copy = _SKIP_REASON_COPY.get(reason)
        if reason_copy is not None:
            lines.append(f"Причина: {reason_copy}")
        return "\n".join(lines)
    raise ValueError(f"unsupported user event kind: {kind!r}")


def _percent(value: Any) -> str | None:
    """Fraction fact (0.02) becomes a Russian percent (2,0%)."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError):
        return str(value)
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0")
        if text.endswith("."):
            text += "0"
    else:
        text += ".0"
    return text.replace(".", ",") + "%"


def event_notification_action(kind: str) -> str | None:
    """Typed keyboard action for an event kind; trade opens link the trade."""
    if kind in ("POSITION_OPENED", "POSITION_INCREASED", "POSITION_REDUCED", "POSITION_CLOSED"):
        return _OPEN_TRADE_ACTION
    return None
