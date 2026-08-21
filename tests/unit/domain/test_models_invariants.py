from decimal import Decimal

import pytest

from copytradeaster.domain.models import (
    PositionKey,
    PosSide,
    QtyDecision,
    RiskPolicy,
    SkipReason,
    SymbolFilters,
)


class TestQtyDecisionInvariant:
    def test_qty_only(self):
        d = QtyDecision(Decimal("0.5"), None)
        assert d.qty == Decimal("0.5")
        assert d.reason is None

    def test_reason_only(self):
        d = QtyDecision(None, SkipReason.NO_STOP)
        assert d.qty is None
        assert d.reason == SkipReason.NO_STOP

    @pytest.mark.parametrize(
        "qty,reason",
        [
            (None, None),
            (Decimal("1"), SkipReason.BELOW_MIN_QTY),
        ],
    )
    def test_exactly_one_field(self, qty, reason):
        with pytest.raises(ValueError):
            QtyDecision(qty, reason)


class TestPositionKey:
    def test_hashable_and_eq(self):
        k1 = PositionKey("BTCUSDT", PosSide.BOTH)
        k2 = PositionKey("BTCUSDT", PosSide.BOTH)
        k3 = PositionKey("BTCUSDT", PosSide.LONG)
        assert k1 == k2
        assert hash(k1) == hash(k2)
        assert k1 != k3
        assert len({k1, k2, k3}) == 2

    def test_default_pos_side(self):
        assert PositionKey("ETHUSDT").pos_side == PosSide.BOTH


class TestDecimalOnly:
    def test_accepts_str_int_decimal(self):
        f = SymbolFilters(
            symbol="BTCUSDT",
            tick_size="0.1",
            step_size=1,
            min_qty=Decimal("0.001"),
            max_qty=Decimal(1000),
            market_max_qty=Decimal("120"),
            min_notional="5",
        )
        assert f.tick_size == Decimal("0.1")
        assert f.step_size == Decimal(1)
        assert f.min_notional == Decimal("5")

    @pytest.mark.parametrize(
        "field", ["tick_size", "step_size", "min_qty", "max_qty", "market_max_qty", "min_notional"]
    )
    def test_rejects_float(self, field):
        kwargs = dict(
            symbol="BTCUSDT",
            tick_size="0.1",
            step_size="1",
            min_qty="0.001",
            max_qty="1000",
            market_max_qty="120",
            min_notional="5",
        )
        kwargs[field] = 0.5
        with pytest.raises(TypeError, match="float is forbidden"):
            SymbolFilters(**kwargs)

    def test_policy_rejects_float(self):
        with pytest.raises(TypeError, match="float is forbidden"):
            RiskPolicy(risk_percent=0.01, max_leverage=20, max_entry_deviation_pct="0.01")

    def test_policy_accepts_str(self):
        p = RiskPolicy(risk_percent="0.01", max_leverage=20, max_entry_deviation_pct="0.01")
        assert p.risk_percent == Decimal("0.01")
