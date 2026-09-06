"""Strict P8a healing acceptance: classification + intent match (review 636dfff P2)."""

import json

from tests.testnet.test_pre_mainnet_load_live import (
    _classified_deterministic,
    _failed_rows_healed,
)


def _row(account, kind, error, created, side="", position_side="", leverage=""):
    return {
        "account_id": account,
        "kind": kind,
        "last_error": error,
        "created_at": created,
        "payload": json.dumps(
            {
                "symbol": "BTCUSDT",
                "side": side,
                "positionSide": position_side,
                "leverage": leverage,
            }
        ),
    }


def _close(account, side, position_side, error, created):
    return _row(account, "MARKET_CLOSE", error, created, side, position_side)


def test_unclassified_errors_never_heal_even_with_successor():
    successor = [
        _close("copyc1", "SELL", "LONG", "", "2026-09-06T05:00:00+00:00"),
    ]
    for bad in ("http:500 boom", "http:502 bad gateway", "RuntimeError: reset", ""):
        failed = [_close("copyc1", "SELL", "LONG", bad, "2026-09-06T04:00:00+00:00")]
        ok, msg = _failed_rows_healed(failed, successor)
        assert not ok, f"{bad!r} accepted as healed: {msg}"
        assert not _classified_deterministic(bad)


def test_opposite_hedge_leg_never_heals():
    failed = [_close("copyc1", "SELL", "LONG", "-4226:aster:-4226", "2026-09-06T04:00:00+00:00")]
    successor = [
        _close("copyc1", "BUY", "SHORT", "", "2026-09-06T05:00:00+00:00"),
    ]
    ok, msg = _failed_rows_healed(failed, successor)
    assert not ok, f"cross-leg successor accepted: {msg}"


def test_classified_reject_with_same_intent_heals():
    failed = [_close("copyc1", "SELL", "LONG", "-4226:aster:-4226", "2026-09-06T04:00:00+00:00")]
    successor = [
        _close("copyc1", "SELL", "LONG", "", "2026-09-06T05:00:00+00:00"),
    ]
    ok, msg = _failed_rows_healed(failed, successor)
    assert ok, msg


def test_boot_lifecycle_verdict_with_successor_heals():
    failed = [
        _row(
            "copyc3",
            "PLACE_STOP",
            "boot-proven-not-executed:current:EXPIRED:executed=0",
            "2026-09-06T04:00:00+00:00",
        )
    ]
    successor = [
        _row("copyc3", "PLACE_STOP", "", "2026-09-06T05:00:00+00:00"),
    ]
    ok, msg = _failed_rows_healed(failed, successor)
    assert ok, msg


def test_moot_only_for_classified_close_on_flat_account():
    close = _close("copyc1", "SELL", "LONG", "-4226:aster:-4226", "2026-09-06T05:06:39+00:00")
    flat = frozenset({"copyc1"})
    ok, msg = _failed_rows_healed([close], [], flat_moot_accounts=flat)
    assert ok and "1 moot-by-flat" in msg, msg
    # Same shape, unclassified error: fatal even when flat.
    ok, _ = _failed_rows_healed(
        [_close("copyc1", "SELL", "LONG", "", "2026-09-06T05:06:39+00:00")],
        [],
        flat_moot_accounts=flat,
    )
    assert not ok
    # An unconfirmed OPEN is never moot: flat proves nothing about open intent.
    ok, _ = _failed_rows_healed(
        [
            _row(
                "copyc1",
                "MARKET_OPEN",
                "-4226:aster:-4226",
                "2026-09-06T05:06:39+00:00",
                side="BUY",
                position_side="LONG",
            )
        ],
        [],
        flat_moot_accounts=flat,
    )
    assert not ok
