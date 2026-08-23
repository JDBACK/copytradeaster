from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import RiskPolicy, SkipReason, SymbolFilters
from copytradeaster.risk.engine import (
    deviation_exceeded,
    entry_quantity,
    floor_to_step,
    margin_check,
    post_fill_cap,
    scaled_reduce,
)


def filters(
    step="0.001",
    tick="0.1",
    min_qty="0.001",
    max_qty="1000",
    market_max_qty="120",
    min_notional="5",
) -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=D(tick),
        step_size=D(step),
        min_qty=D(min_qty),
        max_qty=D(max_qty),
        market_max_qty=D(market_max_qty),
        min_notional=D(min_notional),
    )


def policy(risk="0.01", max_lev=20, dev="0.01") -> RiskPolicy:
    return RiskPolicy(risk_percent=D(risk), max_leverage=max_lev, max_entry_deviation_pct=D(dev))


class TestFloorToStep:
    def test_basic(self):
        assert floor_to_step(D("0.0049"), D("0.001")) == D("0.004")
        assert floor_to_step(D("7"), D("0.5")) == D("7")

    def test_exact_multiple(self):
        assert floor_to_step(D("0.005"), D("0.005")) == D("0.005")


class TestEntryQuantity:
    def test_basic_sizing(self):
        # equity 1000, risk 1% => budget 10; entry 65000 stop 64500 => distance 500
        # per_unit = 500 * 1.001 = 500.5; qty_raw = 10/500.5 = 0.01998... -> 0.019
        d = entry_quantity(policy(), D(1000), D(65000), D(64500), filters(), D("0.001"))
        assert d.reason is None
        assert d.qty == D("0.019")

    def test_floors_to_step(self):
        d = entry_quantity(policy(), D(1000), D(65000), D(64500), filters(), D("0.001"))
        assert (d.qty / D("0.001")) % 1 == 0

    def test_below_min_qty(self):
        d = entry_quantity(policy(risk="0.00001"), D(10), D(65000), D(64500), filters(), D("0.001"))
        assert d.qty is None
        assert d.reason == SkipReason.BELOW_MIN_QTY

    def test_below_min_notional(self):
        # budget 0.1 -> qty_raw ~0.0002 < step -> BELOW_MIN_QTY; raise minNotional path:
        # use big step distance so qty passes minQty but notional < threshold
        f = filters(min_notional="200000")
        d = entry_quantity(policy(), D(1000), D(65000), D(64500), f, D("0.001"))
        assert d.reason == SkipReason.BELOW_MIN_NOTIONAL or d.reason == SkipReason.BELOW_MIN_QTY

    def test_clamp_to_market_max_qty(self):
        huge = filters(market_max_qty="0.02")
        d = entry_quantity(policy(risk="1"), D(100000), D(65000), D(60000), huge, D("0.001"))
        assert d.reason is None
        assert d.qty <= D("0.02")

    def test_zero_distance_guard(self):
        d = entry_quantity(policy(), D(1000), D(65000), D(65000), filters(), D("0.001"))
        assert d.qty is None
        assert d.reason == SkipReason.NO_STOP

    @pytest.mark.parametrize(
        "equity,lev,expect_ok",
        [
            (D(1000), 20, True),  # required = 39.975; limit = 800
            (D(50), 20, True),  # required = 39.975 <= limit 40
            (D(49), 20, False),  # required 39.975 > limit 39.2
        ],
    )
    def test_margin_boundary(self, equity, lev, expect_ok):
        assert margin_check(D("0.0123"), D(65000), lev, equity) == expect_ok

    def test_insufficient_margin_skip(self):
        f = filters()
        d = entry_quantity(policy(max_lev=1, risk="0.5"), D(100), D(65000), D(64500), f, D("0.001"))
        assert d.reason == SkipReason.INSUFFICIENT_MARGIN

    def test_margin_uses_effective_selected_leverage(self):
        # The coordinator may cap a 20x policy to a 2x master leverage. The
        # same quantity passes at 20x but must be rejected at the actual 2x.
        d = entry_quantity(
            policy(risk="1", max_lev=20),
            D(1000),
            D(65000),
            D(64500),
            filters(),
            D("0.001"),
            leverage=2,
        )
        assert d.reason == SkipReason.INSUFFICIENT_MARGIN

    def test_fee_buffer_shrinks_qty(self):
        with_buf = entry_quantity(policy(), D(1000), D(65000), D(64500), filters(), D("0.001"))
        without = entry_quantity(policy(), D(1000), D(65000), D(64500), filters(), D(0))
        assert without.qty >= with_buf.qty


class TestPostFillCap:
    def test_slippage_up_reduces_cap(self):
        base = post_fill_cap(policy(), D(1000), D(65000), D(64500), filters(), D("0.001"))
        slipped = post_fill_cap(policy(), D(1000), D(65100), D(64500), filters(), D("0.001"))
        assert slipped < base
        assert base == D("0.019")

    def test_zero_distance_returns_zero(self):
        assert post_fill_cap(policy(), D(1000), D(64500), D(64500), filters(), D("0.001")) == 0


class TestScaledReduce:
    def test_proportional(self):
        out = scaled_reduce(D("0.100"), D("1.0"), D("0.75"), filters())
        assert out == D("0.075")

    def test_master_old_zero(self):
        assert scaled_reduce(D("0.1"), D(0), D("0.5"), filters()) == 0

    def test_floor_by_step(self):
        out = scaled_reduce(D("0.1"), D("3"), D("1"), filters())
        assert out == D("0.033")


class TestDeviationExceeded:
    def test_strictly_greater_threshold(self):
        p = policy(dev="0.01")
        assert not deviation_exceeded(D(100), D(101), p)  # exactly 1% -> allowed
        assert deviation_exceeded(D(100), D("101.01"), p)  # 1.01% -> exceeded
        assert not deviation_exceeded(D(100), D("99.01"), p)  # 0.99% -> ok

    def test_symmetric(self):
        p = policy(dev="0.01")
        assert deviation_exceeded(D(100), D(98), p)
