"""Pre-mainnet gate: does the cluster really push public mark prices?

The Aster testnet cluster acknowledges the mark-price subscription and then
stays silent, so presentation quotes there come from the REST fallback
(``GET /fapi/v3/premiumIndex``), and the live runtime proves stream liveness
by a frame that carries a price. Production must be verified to push frames
before mainnet; this probe answers that question and checks the fallback.

Both endpoints are public and unsigned: no credentials, no database, no
``.env``. The frame reader is deliberately independent of
``miniapp/live.py``, so a bug in the runtime parser cannot hide here.

Usage:
    .venv/bin/python scripts/check_mark_stream.py [--network testnet|mainnet]
        [--symbols BTCUSDT,ETHUSDT] [--seconds 30]

Exit codes:
    0 — every requested symbol delivered mark-price frames (stream works)
    1 — the subscription was acknowledged but no frame arrived (silent)
    2 — the socket or the REST fallback failed
    3 — only some requested symbols delivered frames (partial feed)

The verdict is per symbol on purpose: one healthy symbol must never pass the
gate for a whitelist whose other symbol has gone silent, because the runtime
keeps that symbol's quotes on the REST fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

from copytradeaster.aster.network_profile import AsterNetworkProfile  # noqa: E402

#: Shipped default matches ``CT_SYMBOL_WHITELIST``; pass ``--symbols`` to
#: probe a deployment that follows another whitelist.
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT"
SUBSCRIBE_ID = 1
HTTP_TIMEOUT_S = 10.0


def read_frame(payload: str | bytes, wanted: set[str]) -> tuple[bool, dict[str, Decimal]]:
    """Classify one frame: (subscription ack, mark prices keyed by symbol).

    Understands both shapes: a WS mark-price frame (``s``/``p``) and a REST
    ``premiumIndex`` row (``symbol``/``markPrice``). Only finite positive
    prices of wanted symbols are reported.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        raw = json.loads(payload)
    except ValueError:
        return False, {}
    if isinstance(raw, dict) and "data" in raw:
        raw = raw["data"]
    if isinstance(raw, dict) and "result" in raw and "s" not in raw and "symbol" not in raw:
        return True, {}
    prices: dict[str, Decimal] = {}
    for row in raw if isinstance(raw, list) else [raw]:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("s") or row.get("symbol") or "").strip().upper()
        if symbol not in wanted:
            continue
        try:
            price = Decimal(str(row.get("p") or row.get("markPrice") or ""))
        except (InvalidOperation, ValueError):
            continue
        if price.is_finite() and price > 0:
            prices[symbol] = price
    return False, prices


async def probe_stream(
    url: str, subscriptions: list[str], symbols: list[str], seconds: float
) -> tuple[bool, dict[str, list[float]]]:
    """Read one socket for ``seconds``; report the ack and per-symbol frames."""
    wanted = set(symbols)
    frames: dict[str, list[float]] = {}
    acked = False
    started = time.monotonic()
    async with connect(url) as ws:
        if subscriptions:
            await ws.send(
                json.dumps({"method": "SUBSCRIBE", "params": subscriptions, "id": SUBSCRIBE_ID})
            )
        while time.monotonic() - started < seconds:
            remaining = seconds - (time.monotonic() - started)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            is_ack, prices = read_frame(raw, wanted)
            acked = acked or is_ack
            for symbol, _price in prices.items():
                frames.setdefault(symbol, []).append(time.monotonic() - started)
    return acked, frames


async def probe_rest(base_url: str, symbols: list[str]) -> tuple[float, dict[str, Decimal]]:
    """Latency and marks of the unsigned fallback the runtime depends on."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_S)) as client:
        started = time.monotonic()
        response = await client.get(f"{base_url.rstrip('/')}/fapi/v3/premiumIndex")
        latency = time.monotonic() - started
        response.raise_for_status()
        text = response.text
    _acked, prices = read_frame(text, set(symbols))
    return latency, prices


def report(label: str, acked: bool, frames: dict[str, list[float]], seconds: float) -> None:
    total = sum(len(times) for times in frames.values())
    per_symbol = ", ".join(f"{symbol}={len(times)}" for symbol, times in sorted(frames.items()))
    print(f"{label}: ack={acked} frames={total} ({per_symbol or 'none'}) over {seconds:g}s")
    for symbol, times in sorted(frames.items()):
        if len(times) > 1:
            gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
            print(
                f"  {symbol}: gaps min={min(gaps):.2f}s max={max(gaps):.2f}s "
                f" (expected ~1s for @1s stream)"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--network",
        choices=("testnet", "mainnet"),
        default="testnet",
        help="official Aster network to probe (default: testnet)",
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"comma-separated symbols (default: {DEFAULT_SYMBOLS})",
    )
    parser.add_argument("--seconds", type=float, default=30.0, help="probe window per path")
    return parser.parse_args()


def verdict(
    symbols: list[str],
    frames: dict[str, list[float]],
    prices: dict[str, Decimal],
) -> tuple[int, list[str]]:
    """Per-symbol gate verdict and its report lines.

    Streaming is claimed only when every requested symbol delivered frames:
    one healthy symbol must not pass the gate for a whitelist whose other
    symbol is silent, because the runtime keeps the silent symbol on the REST
    fallback and its PnL would freeze there.
    """
    streamed = [symbol for symbol in symbols if frames.get(symbol)]
    missing = [symbol for symbol in symbols if not frames.get(symbol)]
    covered = [symbol for symbol in missing if symbol in prices]
    if not missing:
        return 0, ["VERDICT: STREAMING — the cluster pushes mark-price frames."]
    coverage = (
        ", ".join(covered)
        if covered
        else ("none of the silent symbols" if streamed else "none of the symbols")
    )
    if streamed:
        return 3, [
            f"VERDICT: PARTIAL — {len(streamed)}/{len(symbols)} symbols streamed "
            f"({', '.join(streamed)}); no frames for {', '.join(missing)}.",
            f"  REST fallback covers: {coverage}",
        ]
    return 1, [
        "VERDICT: SILENT — presentation quotes depend on the REST fallback here.",
        f"  REST fallback covers: {coverage}",
    ]


async def main() -> int:
    args = parse_args()
    profile = (
        AsterNetworkProfile.testnet()
        if args.network == "testnet"
        else AsterNetworkProfile.mainnet()
    )
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        print("no symbols to probe", file=sys.stderr)
        return 2
    ws_base = profile.ws_user_base.rstrip("/")
    print(f"network: {profile.name}  ws={ws_base}  rest={profile.rest_base_url}")

    try:
        subscriptions = [f"{symbol.lower()}@markPrice@1s" for symbol in symbols]
        acked, frames = await probe_stream(ws_base, subscriptions, symbols, args.seconds)
        report("subscribe path", acked, frames, args.seconds)
        if not frames and len(symbols) == 1:
            raw = f"{ws_base}/{symbols[0].lower()}@markPrice@1s"
            raw_acked, raw_frames = await probe_stream(raw, [], symbols, args.seconds)
            report(f"raw path ({raw})", raw_acked, raw_frames, args.seconds)
            frames = raw_frames
        latency, prices = await probe_rest(profile.rest_base_url, symbols)
    except Exception as exc:  # probe failure is the verdict, not a crash
        print(f"probe failed class={type(exc).__name__}", file=sys.stderr)
        return 2

    shown = ", ".join(f"{symbol}={price}" for symbol, price in sorted(prices.items())) or "none"
    print(f"rest fallback: {latency * 1000:.0f} ms  marks={shown}")
    code, lines = verdict(symbols, frames, prices)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
