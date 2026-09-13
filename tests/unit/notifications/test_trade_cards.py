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


def test_negative_and_sub_unit_pnl_keep_their_sign():
    for gross, expected in (
        ("-0.5", "PnL до комиссий: -0,5 USDT"),
        ("-12.25", "PnL до комиссий: -12,25 USDT"),
        ("-0.0004", "PnL до комиссий: -0,0004 USDT"),
    ):
        text = format_user_event(
            _event(
                "POSITION_CLOSED",
                {"symbol": "BTCUSDT", "side": "LONG", "realized_gross": gross},
            )
        )
        assert expected in text.splitlines(), (gross, text)


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


def test_entry_skip_reads_as_copy_never_as_an_engine_code():
    text = format_user_event(
        _event(
            "ENTRY_SKIPPED",
            {"symbol": "BTCUSDT", "reason": "STALE_MASTER", "pos_side": "LONG"},
        )
    )
    assert text == "Вход BTCUSDT (LONG) пропущен\nПричина: сигнал мастера устарел"
    assert "STALE_MASTER" not in text


def test_entry_skip_copy_covers_every_engine_reason():
    from copytradeaster.domain.models import SkipReason
    from copytradeaster.notifications.trade_cards import _SKIP_REASON_COPY

    covered = {reason.value for reason in SkipReason} - {"INTENTIONAL_FLAT"}
    assert covered == set(_SKIP_REASON_COPY)
    for reason in SkipReason:
        text = format_user_event(
            _event("ENTRY_SKIPPED", {"symbol": "BTCUSDT", "reason": reason.value})
        )
        assert reason.value not in text
        assert "пропущен" in text or reason is SkipReason.INTENTIONAL_FLAT


def test_entry_skip_without_known_reason_prints_no_code():
    assert (
        format_user_event(_event("ENTRY_SKIPPED", {"symbol": "BTCUSDT"})) == "Вход BTCUSDT пропущен"
    )
    assert (
        format_user_event(_event("ENTRY_SKIPPED", {"symbol": "BTCUSDT", "reason": "WEIRD_CODE"}))
        == "Вход BTCUSDT пропущен"
    )


def test_intentional_flat_is_not_phrased_as_a_skip():
    assert (
        format_user_event(
            _event(
                "ENTRY_SKIPPED",
                {"symbol": "BTCUSDT", "reason": "INTENTIONAL_FLAT", "pos_side": "SHORT"},
            )
        )
        == "Фиксация позиции BTCUSDT (SHORT)"
    )


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        format_user_event(_event("NOPE", {}))


def test_keyboard_action_only_for_trade_kinds():
    assert event_notification_action("POSITION_OPENED") == "open_trade"
    assert event_notification_action("POSITION_CLOSED") == "open_trade"
    assert event_notification_action("RISK_CHANGED") is None
    assert event_notification_action("COPY_PAUSED") is None


def test_closed_card_prints_the_proven_net_as_one_result():
    """A proven composition replaces the gross headline; no second number."""
    money = _money_view(net="17.04", result_state="net_complete")
    text = format_user_event(
        _event(
            "POSITION_CLOSED",
            {"symbol": "BTCUSDT", "side": "LONG", "realized_gross": "18.42"},
        ),
        money=money,
    )
    lines = text.splitlines()
    assert "Итог сделки: 17,04 USDT" in lines
    assert not any("до комиссий" in line for line in lines), lines


def test_closed_card_stays_honest_while_costs_are_unproven():
    money = _money_view(net=None, result_state="gross_only")
    text = format_user_event(
        _event(
            "POSITION_CLOSED",
            {"symbol": "BTCUSDT", "side": "LONG", "realized_gross": "18.42"},
        ),
        money=money,
    )
    lines = text.splitlines()
    assert "Итог уточняется: начисления ещё сверяются" in lines
    assert not any("до комиссий" in line for line in lines), lines


def test_closed_card_keeps_gross_when_no_money_reader_is_wired():
    text = format_user_event(
        _event(
            "POSITION_CLOSED",
            {"symbol": "BTCUSDT", "side": "LONG", "realized_gross": "18.42"},
        )
    )
    assert "PnL до комиссий: 18,42 USDT" in text.splitlines()


def _money_view(*, net: str | None, result_state: str):
    from copytradeaster.application.models import EpisodeMoneyView, MoneyComponent, MoneyState

    component = MoneyComponent(known_amount="0", state=MoneyState.COMPLETE)
    return EpisodeMoneyView(
        gross="18.42",
        net=net,
        commission=component,
        funding=component,
        builder=component,
        result_state=result_state,
    )
