"""Pre-mainnet mark-stream gate: the verdict must hold for every symbol."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_mark_stream.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("check_mark_stream", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_symbol_streaming_passes(gate):
    code, lines = gate.verdict(
        ["BTCUSDT", "ETHUSDT"],
        {"BTCUSDT": [0.9, 1.9], "ETHUSDT": [1.0, 2.0]},
        {"BTCUSDT": Decimal("70000")},
    )
    assert code == 0
    assert lines == ["VERDICT: STREAMING — the cluster pushes mark-price frames."]


def test_one_silent_symbol_never_passes_as_streaming(gate):
    code, lines = gate.verdict(
        ["BTCUSDT", "ETHUSDT"],
        {"BTCUSDT": [0.9, 1.9]},
        {"BTCUSDT": Decimal("70000"), "ETHUSDT": Decimal("3000")},
    )
    assert code == 3
    assert lines[0].startswith("VERDICT: PARTIAL — 1/2 symbols streamed (BTCUSDT)")
    assert lines[1] == "  REST fallback covers: ETHUSDT"


def test_partial_feed_reports_an_uncovered_silent_symbol(gate):
    code, lines = gate.verdict(["BTCUSDT", "ETHUSDT"], {"BTCUSDT": [0.9]}, {})
    assert code == 3
    assert lines[1] == "  REST fallback covers: none of the silent symbols"


def test_silent_cluster_is_reported_with_fallback_coverage(gate):
    code, lines = gate.verdict(["BTCUSDT"], {}, {"BTCUSDT": Decimal("70000")})
    assert code == 1
    assert lines == [
        "VERDICT: SILENT — presentation quotes depend on the REST fallback here.",
        "  REST fallback covers: BTCUSDT",
    ]


def test_frame_reader_accepts_stream_and_rest_shapes(gate):
    acked, prices = gate.read_frame('{"id":1,"result":null}', {"BTCUSDT"})
    assert (acked, prices) == (True, {})
    assert gate.read_frame('{"e":"markPriceUpdate","s":"BTCUSDT","p":"70000.5"}', {"BTCUSDT"}) == (
        False,
        {"BTCUSDT": Decimal("70000.5")},
    )
    assert gate.read_frame(
        '[{"symbol":"ETHUSDT","markPrice":"3000.5"}]', {"BTCUSDT", "ETHUSDT"}
    ) == (False, {"ETHUSDT": Decimal("3000.5")})
