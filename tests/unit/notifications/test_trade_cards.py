"""Shared user-event presenter: one meaning for push, menu and Mini App."""

from __future__ import annotations

import pytest

from copytradeaster.notifications.trade_cards import (
    event_notification_action,
    format_user_event,
)
from copytradeaster.storage.events import UserEvent


def _event(kind: str, facts: dict, trade_ref: str | None = "ref-1") -> UserEvent:
    return UserEvent(
        event_ref="evt-1",
        kind=kind,  # type: ignore[arg-type]
        trade_ref=trade_ref,
        occurred_at=1_000,
        recorded_at=2_000,
        facts=facts,
        dedup_key="key-1",
        notify_eligible=True,
    )


def test_open_close_cards_carry_numbers_without_guesses():
    opened = format_user_event(
        _event(
            "POSITION_OPENED",
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_qty": "0.016",
                "entry_price": "62400",
            },
        ),
        quantity_asset="BTC",
    )
    assert opened.splitlines() == [
        "Открыта BTCUSDT · LONG",
        "Объём: 0,016 BTC",
        "Средняя цена исполнения: 62 400",
    ]
    closed = format_user_event(
        _event(
            "POSITION_CLOSED",
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_price": "62400",
                "exit_price": "64100",
                "realized_gross": "18.42",
                "duration_ms": 8_280_000,
            },
        )
    )
    assert closed.splitlines() == [
        "Закрыта BTCUSDT · LONG",
        "Средний вход: 62 400",
        "Средний выход: 64 100",
        "PnL до комиссий: 18,42 USDT",
        "В позиции: 2 ч 18 мин",
    ]


def test_partial_close_marks_gross_as_pre_commission():
    text = format_user_event(
        _event(
            "POSITION_REDUCED",
            {
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "closed_qty": "0.004",
                "close_price": "64100",
                "remaining_qty": "0.012",
                "realized_gross": "6.8",
            },
        ),
        quantity_asset="BTC",
    )
    assert "Сокращена BTCUSDT · SHORT" in text
    assert "Закрыто: 0,004 BTC по 64 100" in text
    assert "PnL закрытой части: 6,8 USDT до комиссий" in text


def test_missing_result_is_explicit_not_zero():
    text = format_user_event(_event("POSITION_CLOSED", {"symbol": "BTCUSDT"}))
    assert "Закрыта BTCUSDT" in text
    assert "Результат уточняется" in text
    assert "0" not in text


def test_unknown_side_is_omitted_not_guessed():
    text = format_user_event(_event("POSITION_OPENED", {"symbol": "BTCUSDT", "entry_qty": "1"}))
    assert text.splitlines()[0] == "Открыта BTCUSDT"


def test_protection_and_system_kinds():
    changed = format_user_event(
        _event(
            "PROTECTION_CHANGED",
            {"symbol": "BTCUSDT", "slot": "stop", "old_price": "61500", "new_price": "61900"},
        )
    )
    assert changed == "Стоп BTCUSDT изменён: 61 500 → 61 900"
    assert (
        format_user_event(
            _event("PROTECTION_UNCONFIRMED", {"symbol": "BTCUSDT", "slot": "take_profit"})
        )
        == "Тейк-профит BTCUSDT не подтверждён"
    )
    assert (
        format_user_event(
            _event("RISK_CHANGED", {"risk_percent": "0.02", "previous_percent": "0.01"})
        )
        == "Риск на сделку изменён: 1,0% → 2,0%"
    )
    assert "приостановлено" in format_user_event(_event("COPY_PAUSED", {}))
    assert "возобновлено" in format_user_event(_event("COPY_RESUMED", {}))
    assert "пропущен" in format_user_event(
        _event("ENTRY_SKIPPED", {"symbol": "BTCUSDT", "reason": "ENTRY_DEVIATION"})
    )


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        format_user_event(_event("NOPE", {}))


def test_keyboard_action_only_for_trade_kinds():
    assert event_notification_action("POSITION_OPENED") == "open_trade"
    assert event_notification_action("POSITION_CLOSED") == "open_trade"
    assert event_notification_action("RISK_CHANGED") is None
    assert event_notification_action("COPY_PAUSED") is None
