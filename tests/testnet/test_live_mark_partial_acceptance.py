"""Live acceptance: quote health is decided per symbol, never globally.

Merge-review corrective P1-2. The runtime used one global "mark stream is
alive" flag, so a whitelist whose second symbol went silent kept a healthy
verdict and starved the REST fallback: the dead symbol's quotes froze while
the UI still read live.

Both cases below use REAL endpoints:

  * the Aster testnet cluster acknowledges the mark subscription and pushes no
    frames, so every presentation quote there comes from the unsigned REST
    fallback (GET /fapi/v3/premiumIndex) — the fallback must cover every
    whitelisted symbol before health may read live;
  * the mainnet cluster pushes BTCUSDT about once per second, so a whitelist
    that also asks for a symbol it never streams is a genuine partial feed:
    the streamed symbol keeps its stream quotes while the silent one must keep
    health honest and the fallback engaged.

Only public, unsigned market-data endpoints are used: no credentials, no
account rows, no orders. The mainnet case is opt-in because it leaves the
testnet profile:

    CT_TESTNET=1 pytest tests/testnet/test_live_mark_partial_acceptance.py -v
    CT_TESTNET=1 MARK_PARTIAL_LIVE_MAINNET=1 \
        pytest tests/testnet/test_live_mark_partial_acceptance.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from copytradeaster.aster.network_profile import AsterNetworkProfile
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.config import Settings
from copytradeaster.miniapp.live import MiniAppLiveRuntime
from copytradeaster.storage.database import Database

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]

ACCOUNT = "copyc2"
STEP_TIMEOUT_S = 30.0
MAINNET_SYMBOL = "BTCUSDT"
MAINNET_SILENT_SYMBOL = "ZZZUSDT"


class _Snapshots:
    """Quote fan-out seam: records the coalesced quotes the UI would read."""

    def __init__(self) -> None:
        self.marks: dict[str, Any] = {}

    def subscribe(self, callback):
        del callback
        return lambda: None

    def apply_mark_prices(self, prices) -> None:
        self.marks = dict(prices)


class _Registry:
    def snapshot(self):
        return []

    def get(self, account_id: str):
        del account_id
        return None


class _RecordingClient:
    """Delegate everything; record every REST quote fetch and its symbols."""

    def __init__(self, inner: AsterClient) -> None:
        self._inner = inner
        self.mark_calls: list[tuple[str, ...]] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def mark_prices(self, symbols) -> dict:
        wanted = tuple(sorted(str(symbol).upper() for symbol in symbols))
        self.mark_calls.append(wanted)
        return await self._inner.mark_prices(symbols)


class _Harness:
    def __init__(self, settings: Settings, db: Database, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.db = db
        self.http = http
        self.limiter = RateLimits(
            settings.weight_limit_per_min,
            settings.orders_limit_per_min,
            settings.orders_limit_per_10s,
        )
        self.runtimes: list[MiniAppLiveRuntime] = []

    def public_client(self, base_url: str) -> AsterClient:
        """Unsigned public reads only: identity/key providers stay untouched."""
        return AsterClient(
            self.http,
            base_url,
            self.limiter,
            SimpleNamespace(),  # type: ignore[arg-type] - unsigned paths never read it
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    def build(self, cfg, client: AsterClient) -> tuple[MiniAppLiveRuntime, _Snapshots]:
        snapshots = _Snapshots()
        app = SimpleNamespace(
            cfg=cfg,
            db=self.db,
            master_client=client,
            follower_registry=_Registry(),
            coordinator=SimpleNamespace(),
            follower_snapshots=snapshots,
        )
        runtime = MiniAppLiveRuntime(app)
        # Health is only published for accounts the hub already tracks; the
        # private-stream arm is out of scope here, so seed it healthy.
        runtime._private_health[ACCOUNT] = True  # noqa: SLF001 - health seam
        self.runtimes.append(runtime)
        return runtime, snapshots

    def cfg_for(self, ws_user_base: str, symbols: list[str]):
        return SimpleNamespace(
            ws_user_base=ws_user_base,
            symbol_whitelist=list(symbols),
            listenkey_keepalive_min=self.settings.listenkey_keepalive_min,
        )


@pytest.fixture
async def harness(tmp_path: Path):
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail("signed tests are restricted to testnet")
    db = Database(tmp_path / "marks.sqlite3")
    await db.connect()
    await db.migrate()
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    built = _Harness(settings, db, http)
    try:
        yield built
    finally:
        for runtime in built.runtimes:
            with contextlib.suppress(Exception):
                await runtime.stop()
        await db.close()
        await http.aclose()


async def _wait_until(predicate, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}")


def _drain(queue: asyncio.Queue) -> list[dict]:
    events: list[dict] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_rest_fallback_covers_every_silent_symbol(harness):
    """Testnet pushes no mark frames: every whitelisted symbol needs a quote."""
    client = _RecordingClient(harness.public_client(harness.settings.rest_base_url))
    symbols = list(harness.settings.symbol_whitelist)
    cfg = harness.cfg_for(harness.settings.ws_user_base, symbols)
    runtime, snapshots = harness.build(cfg, client)
    queue, hello = runtime.hub.subscribe(ACCOUNT)
    assert hello["health"] == "connecting", hello
    fallback = asyncio.create_task(runtime._run_mark_fallback())  # noqa: SLF001 - live seam
    try:
        await _wait_until(
            lambda: set(snapshots.marks) == set(symbols),
            STEP_TIMEOUT_S,
            "fallback quotes for every whitelisted symbol",
        )
        stamps = {symbol: runtime._fallback_quotes_at.get(symbol) for symbol in symbols}  # noqa: SLF001
        assert all(stamp is not None for stamp in stamps.values()), stamps
        assert runtime._quotes_live, "fallback-covered quotes must count as live"  # noqa: SLF001
        await _wait_until(
            lambda: any(event.get("health") == "live" for event in _drain(queue)),
            STEP_TIMEOUT_S,
            "a live health verdict once every symbol was covered",
        )
    finally:
        fallback.cancel()
        await asyncio.gather(fallback, return_exceptions=True)


async def test_partial_feed_is_not_reported_as_live(harness):
    """Mainnet streams BTCUSDT and never ZZZUSDT: a genuine partial feed."""
    if os.environ.get("MARK_PARTIAL_LIVE_MAINNET") != "1":
        pytest.skip("set MARK_PARTIAL_LIVE_MAINNET=1 to read public mainnet marks")
    profile = AsterNetworkProfile.mainnet()
    assert "testnet" not in profile.ws_user_base, profile.ws_user_base
    client = _RecordingClient(harness.public_client(profile.rest_base_url))
    symbols = [MAINNET_SYMBOL, MAINNET_SILENT_SYMBOL]
    cfg = harness.cfg_for(profile.ws_user_base, symbols)
    runtime, snapshots = harness.build(cfg, client)
    queue, hello = runtime.hub.subscribe(ACCOUNT)
    stream = asyncio.create_task(runtime._run_market_stream())  # noqa: SLF001 - live seam
    fallback = asyncio.create_task(runtime._run_mark_fallback())  # noqa: SLF001 - live seam
    try:
        await _wait_until(
            lambda: MAINNET_SYMBOL in runtime._market_frames,  # noqa: SLF001
            STEP_TIMEOUT_S,
            f"streamed frames for {MAINNET_SYMBOL}",
        )
        await _wait_until(
            lambda: MAINNET_SYMBOL in snapshots.marks,
            STEP_TIMEOUT_S,
            "coalesced quotes for the streamed symbol",
        )
        stamps = dict(runtime._market_frames)  # noqa: SLF001 - per-symbol evidence
        assert stamps[MAINNET_SYMBOL] > 0
        assert MAINNET_SILENT_SYMBOL not in stamps, stamps
        assert runtime._silent_symbols() == [MAINNET_SILENT_SYMBOL]  # noqa: SLF001
        assert not runtime._quotes_live, "one dead symbol is a quote outage"  # noqa: SLF001
        # The REST fallback must stay engaged for the silent symbol only: a
        # global "stream is alive" verdict would idle it with a frozen quote.
        # The baseline drops the startup iteration that ran before the first
        # stream frame proved which symbol the stream actually feeds.
        baseline = len(client.mark_calls)
        await _wait_until(
            lambda: (
                sum(
                    1
                    for symbols_asked in client.mark_calls[baseline:]
                    if MAINNET_SILENT_SYMBOL in symbols_asked
                )
                >= 2
            ),
            STEP_TIMEOUT_S,
            f"the fallback to keep asking for {MAINNET_SILENT_SYMBOL}",
        )
        assert all(
            MAINNET_SYMBOL not in symbols_asked for symbols_asked in client.mark_calls[baseline:]
        ), client.mark_calls[baseline:]
        health = [event["health"] for event in _drain(queue) if event.get("type") == "health"]
        assert "live" not in health, health
        assert hello["health"] == "connecting", hello
    finally:
        for task in (stream, fallback):
            task.cancel()
        await asyncio.gather(stream, fallback, return_exceptions=True)
