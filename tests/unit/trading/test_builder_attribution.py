"""P2.7 Builder attribution regressions: payload, routing, executor, race, security."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copytradeaster.aster.trading_api import PlaceOrderRequest
from copytradeaster.domain.models import Side
from copytradeaster.trading.builder_attribution import (
    AttributionKind,
    BuilderAttributionProvider,
)

BUILDER_A = "0x1111111111111111111111111111111111111111"
BUILDER_B = "0x2222222222222222222222222222222222222222"
CONFIG_FEE = "0.0001"
NOW_MS = 1_700_000_000_000
FUTURE_MS = 4_102_444_800_000
PAST_MS = 1_600_000_000_000
SIGNER_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SIGNER_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeAccounts:
    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    async def get(self, account_id: str):
        return self._rows.get(account_id)


class FakeValidations:
    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    async def get(self, account_id: str):
        return self._rows.get(account_id)


class FakeCreds:
    def __init__(self, signers: dict[str, str]) -> None:
        self._signers = signers

    def identity(self, account_id: str):
        if account_id not in self._signers:
            raise KeyError(account_id)

        class _I:
            pass

        ident = _I()
        ident.signer = self._signers[account_id]
        return ident


def builder_row(signer_note: str = "x", builder: str = BUILDER_A, fee: str = CONFIG_FEE):
    del signer_note
    return {
        "role": "follower",
        "enabled": 1,
        "identity_source": "builder",
        "builder_address": builder,
        "builder_name": "copytradeaster",
        "builder_max_fee_rate": fee,
        "builder_agent_expires_at_ms": FUTURE_MS,
        "builder_verified_at": "2026-01-01T00:00:00+00:00",
    }


def direct_row():
    return {
        "role": "follower",
        "enabled": 1,
        "identity_source": "direct",
        "builder_address": None,
        "builder_name": None,
        "builder_max_fee_rate": None,
        "builder_agent_expires_at_ms": None,
        "builder_verified_at": None,
    }


def valid_validation(signer: str = SIGNER_A):
    return {
        "signer_address": signer,
        "can_read": 1,
        "can_perp_trade": 1,
        "can_withdraw": 0,
        "expired_ms": FUTURE_MS,
        "ip_whitelist": "0.0.0.0/0",
    }


def make_provider(accounts, validations, creds, builder=BUILDER_A, fee=CONFIG_FEE):
    return BuilderAttributionProvider(
        accounts=FakeAccounts(accounts),
        validations=FakeValidations(validations),
        credentials=FakeCreds(creds),
        builder_address=builder,
        builder_name="copytradeaster",
        max_fee_rate=fee,
        clock_ms=lambda: NOW_MS,
    )


class TestAsterPayload:
    def test_direct_has_no_builder_fields(self):
        req = PlaceOrderRequest(symbol="BTCUSDT", side=Side.BUY, type="MARKET")
        params = req.to_params()
        assert "builder" not in params
        assert "feeRate" not in params

    def test_valid_builder_market(self):
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            type="MARKET",
            builder=BUILDER_A,
            fee_rate=Decimal("0.0001"),
        )
        params = req.to_params()
        assert params["builder"] == BUILDER_A
        assert params["feeRate"] == "0.0001"

    def test_valid_builder_stop_and_tp(self):
        stop = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP_MARKET",
            stop_price=Decimal("64000"),
            builder=BUILDER_A,
            fee_rate=Decimal("0.0001"),
            working_type="MARK_PRICE",
        )
        assert stop.to_params()["feeRate"] == "0.0001"
        assert stop.to_params()["builder"] == BUILDER_A
        tp = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            stop_price=Decimal("66000"),
            builder=BUILDER_A,
            fee_rate=Decimal("0.0001"),
            working_type="MARK_PRICE",
        )
        assert tp.to_params()["feeRate"] == "0.0001"

    def test_limit_stop_tp_flavors_keep_builder(self):
        req = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="STOP",
            quantity=Decimal("0.05"),
            price=Decimal("63900"),
            stop_price=Decimal("64000"),
            builder=BUILDER_A,
            fee_rate=Decimal("0.0001"),
            working_type="MARK_PRICE",
        )
        params = req.to_params()
        assert params["builder"] == BUILDER_A
        assert params["price"] == "63900"
        req2 = PlaceOrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            type="TAKE_PROFIT",
            quantity=Decimal("0.05"),
            price=Decimal("66100"),
            stop_price=Decimal("66000"),
            builder=BUILDER_A,
            fee_rate=Decimal("0.0001"),
            working_type="MARK_PRICE",
        )
        assert req2.to_params()["builder"] == BUILDER_A

    def test_pair_invariant(self):
        with pytest.raises(ValueError):
            PlaceOrderRequest(symbol="X", side=Side.BUY, type="MARKET", builder=BUILDER_A)
        with pytest.raises(ValueError):
            PlaceOrderRequest(symbol="X", side=Side.BUY, type="MARKET", fee_rate=Decimal("0.0001"))

    def test_negative_nan_infinity_rejected(self):
        with pytest.raises(ValueError):
            PlaceOrderRequest(
                symbol="X",
                side=Side.BUY,
                type="MARKET",
                builder=BUILDER_A,
                fee_rate=Decimal("-0.0001"),
            )
        with pytest.raises(ValueError):
            PlaceOrderRequest(
                symbol="X",
                side=Side.BUY,
                type="MARKET",
                builder=BUILDER_A,
                fee_rate=Decimal("NaN"),
            )
        with pytest.raises(ValueError):
            PlaceOrderRequest(
                symbol="X",
                side=Side.BUY,
                type="MARKET",
                builder=BUILDER_A,
                fee_rate=Decimal("Infinity"),
            )
        with pytest.raises(TypeError):
            PlaceOrderRequest(
                symbol="X",
                side=Side.BUY,
                type="MARKET",
                builder=BUILDER_A,
                fee_rate=float("0.0001"),  # type: ignore[arg-type]
            )

    def test_absolute_cap(self):
        ok = PlaceOrderRequest(
            symbol="X",
            side=Side.BUY,
            type="MARKET",
            builder=BUILDER_A,
            fee_rate=Decimal("0.001"),
        )
        assert ok.to_params()["feeRate"] == "0.001"
        with pytest.raises(ValueError):
            PlaceOrderRequest(
                symbol="X",
                side=Side.BUY,
                type="MARKET",
                builder=BUILDER_A,
                fee_rate=Decimal("0.0011"),
            )

    def test_decimal_canonicalization(self):
        req = PlaceOrderRequest(
            symbol="X",
            side=Side.BUY,
            type="MARKET",
            builder=BUILDER_A,
            fee_rate=Decimal("1E-4"),
        )
        assert req.to_params()["feeRate"] == "0.0001"
        assert isinstance(req.fee_rate, Decimal)


class TestRouting:
    async def test_builder_a_gets_a(self):
        p = make_provider({"a": builder_row()}, {"a": valid_validation(SIGNER_A)}, {"a": SIGNER_A})
        d = await p.resolve("a")
        assert d.kind == AttributionKind.BUILDER_VALID
        assert d.attribution is not None
        assert d.attribution.builder == BUILDER_A
        assert d.attribution.fee_rate == Decimal(CONFIG_FEE)

    async def test_builder_b_gets_b_no_cross_leak(self):
        other_builder = BUILDER_B
        p = make_provider(
            {"b": builder_row(builder=other_builder)},
            {"b": valid_validation(SIGNER_B)},
            {"b": SIGNER_B},
            builder=other_builder,
        )
        d = await p.resolve("b")
        assert d.kind == AttributionKind.BUILDER_VALID
        assert d.attribution is not None
        assert d.attribution.builder == BUILDER_B
        # A-configured provider must not validate a B-persisted account.
        p2 = make_provider(
            {"b": builder_row(builder=BUILDER_B)},
            {"b": valid_validation(SIGNER_B)},
            {"b": SIGNER_B},
            builder=BUILDER_A,
        )
        d2 = await p2.resolve("b")
        assert d2.kind == AttributionKind.BUILDER_UNAVAILABLE
        assert d2.attribution is None

    async def test_direct_gets_nothing(self):
        p = make_provider({"d": direct_row()}, {}, {"d": SIGNER_A})
        d = await p.resolve("d")
        assert d.kind == AttributionKind.DIRECT
        assert d.attribution is None

    async def test_missing_snapshot_not_direct(self):
        row = builder_row()
        row["builder_address"] = None
        p = make_provider({"a": row}, {"a": valid_validation()}, {"a": SIGNER_A})
        d = await p.resolve("a")
        assert d.kind == AttributionKind.BUILDER_UNAVAILABLE

    async def test_corrupt_fee_not_direct(self):
        row = builder_row(fee="not-a-decimal")
        p = make_provider({"a": row}, {"a": valid_validation()}, {"a": SIGNER_A})
        d = await p.resolve("a")
        assert d.kind == AttributionKind.BUILDER_UNAVAILABLE

    async def test_config_mismatch_invalid(self):
        p = make_provider(
            {"a": builder_row(builder=BUILDER_A)},
            {"a": valid_validation()},
            {"a": SIGNER_A},
            builder=BUILDER_B,
        )
        d = await p.resolve("a")
        assert d.kind == AttributionKind.BUILDER_UNAVAILABLE
        p2 = make_provider(
            {"a": builder_row(fee="0.0001")},
            {"a": valid_validation()},
            {"a": SIGNER_A},
            builder=BUILDER_A,
            fee="0.0002",
        )
        d2 = await p2.resolve("a")
        assert d2.kind == AttributionKind.BUILDER_UNAVAILABLE

    async def test_agent_expiry_and_signer_mismatch(self):
        row = builder_row()
        row["builder_agent_expires_at_ms"] = PAST_MS
        p = make_provider({"a": row}, {"a": valid_validation()}, {"a": SIGNER_A})
        assert (await p.resolve("a")).kind == AttributionKind.BUILDER_UNAVAILABLE
        v = valid_validation(signer=SIGNER_A)
        v["expired_ms"] = PAST_MS
        p2 = make_provider({"a": builder_row()}, {"a": v}, {"a": SIGNER_A})
        assert (await p2.resolve("a")).kind == AttributionKind.BUILDER_UNAVAILABLE
        p3 = make_provider({"a": builder_row()}, {"a": valid_validation(SIGNER_A)}, {"a": SIGNER_B})
        assert (await p3.resolve("a")).kind == AttributionKind.BUILDER_UNAVAILABLE

    async def test_ui_ttl_does_not_remove_attribution(self):
        row = builder_row()
        row["builder_verified_at"] = "2020-01-01T00:00:00+00:00"
        p = make_provider({"a": row}, {"a": valid_validation()}, {"a": SIGNER_A})
        d = await p.resolve("a")
        assert d.kind == AttributionKind.BUILDER_VALID

    async def test_expiring_warning_window_keeps_attribution(self):
        # 6 days left: inside BUILDER_EXPIRY_WARNING_MS (7d) but actually valid.
        soon = NOW_MS + 6 * 24 * 60 * 60 * 1000
        row = builder_row()
        row["builder_agent_expires_at_ms"] = soon
        v = valid_validation()
        v["expired_ms"] = soon
        p = make_provider({"a": row}, {"a": v}, {"a": SIGNER_A})
        d = await p.resolve("a")
        assert d.kind == AttributionKind.BUILDER_VALID
