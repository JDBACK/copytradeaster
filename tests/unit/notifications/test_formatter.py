"""Unit tests for notification formatter."""

from __future__ import annotations

from copytradeaster.domain.models import ActionKind, ExecutionAction, PosSide, SkipReason
from copytradeaster.notifications.formatter import (
    format_confirmed_action,
    format_skip_reason,
)


def test_format_market_open():
    action = ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.039",
            "positionSide": "LONG",
        },
        client_order_id="ct-1",
        replaces_client_order_id=None,
        priority=1,
        position_side=PosSide.LONG,
    )
    text = format_confirmed_action(action)
    assert "🟢 Открыта позиция BTCUSDT (LONG)" in text
    assert "Объём: 0.039" in text


def test_format_market_close():
    action = ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "0.039",
            "positionSide": "LONG",
        },
        client_order_id="ct-2",
        replaces_client_order_id=None,
        priority=1,
        position_side=PosSide.LONG,
    )
    text = format_confirmed_action(action)
    assert "🔴 Закрыта позиция BTCUSDT (LONG)" in text
    assert "Объём: 0.039" in text


def test_format_place_stop():
    action = ExecutionAction(
        kind=ActionKind.PLACE_STOP,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "stopPrice": "78646.16",
            "positionSide": "LONG",
        },
        client_order_id="ct-3",
        replaces_client_order_id=None,
        priority=1,
        position_side=PosSide.LONG,
    )
    text = format_confirmed_action(action)
    assert "🛡 Установлен защитный стоп BTCUSDT" in text
    assert "78 646.16 USDT" in text


def test_format_place_tp():
    action = ExecutionAction(
        kind=ActionKind.PLACE_TP,
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "stopPrice": "79500.00",
            "positionSide": "LONG",
        },
        client_order_id="ct-4",
        replaces_client_order_id=None,
        priority=1,
        position_side=PosSide.LONG,
    )
    text = format_confirmed_action(action)
    assert "🎯 Установлен тейк-профит BTCUSDT" in text
    assert "79 500 USDT" in text


def test_format_set_leverage():
    action = ExecutionAction(
        kind=ActionKind.SET_LEVERAGE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "leverage": "200"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=0,
    )
    text = format_confirmed_action(action)
    assert "⚡️ Плечо BTCUSDT установлено: 200x" in text


def test_format_margin_mode():
    action = ExecutionAction(
        kind=ActionKind.SET_MARGIN_TYPE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "marginType": "ISOLATED"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=0,
    )
    assert "ISOLATED" in format_confirmed_action(action)


def test_format_cancel_order():
    action = ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "origClientOrderId": "ct-old"},
        client_order_id=None,
        replaces_client_order_id=None,
        priority=1,
    )
    text = format_confirmed_action(action)
    assert "🗑 Снят ордер BTCUSDT" in text


def test_format_skips():
    assert (
        format_skip_reason("BTCUSDT", SkipReason.NO_STOP, PosSide.LONG)
        == "⏳ Ожидание защитного стопа BTCUSDT (LONG)."
    )
    assert "недостаточно свободной маржи" in format_skip_reason(
        "BTCUSDT", SkipReason.INSUFFICIENT_MARGIN, PosSide.LONG
    )
    assert "отклонилась от мастера" in format_skip_reason(
        "BTCUSDT", SkipReason.ENTRY_DEVIATION, PosSide.LONG
    )
    assert "приостановлено" in format_skip_reason("BTCUSDT", SkipReason.PAUSED, PosSide.LONG)
    external = format_skip_reason("BTCUSDT", SkipReason.EXTERNAL_FOLLOWER_CLOSE, PosSide.LONG)
    assert "закрыта вне копитрейдинга" in external
    assert "следующую сделку мастера" in external
    assert "режим маржи мастера" in format_skip_reason(
        "BTCUSDT", SkipReason.MARGIN_TYPE_UNKNOWN, PosSide.LONG
    )
    assert "режим маржи отличается" in format_skip_reason(
        "BTCUSDT", SkipReason.MARGIN_TYPE_MISMATCH, PosSide.LONG
    )
