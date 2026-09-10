"""Live acceptance: a dead follower socket must never read as live.

Merge-review correctives P1-3/P1-4. The Mini App derived its health from a
REST boundary that can outlive the socket which started it: a successful
snapshot re-asserted "live" after the stream had already died (P1-3). And the
reconnect backoff reset on the handshake alone, so a socket that dies right
after connect bought a fresh listenKey every base interval forever (P1-4).

This scenario runs the REAL MiniAppLiveRuntime with the REAL UserStream
against the REAL testnet cluster on a disposable follower account:

  * the socket is killed server-side (DELETE /fapi/v3/listenKey) while the
    slow authoritative boundary is still in flight; health must stay
    "reconnecting" until a genuinely new session starts,
  * four sessions are killed right after they connect; the measured
    reconnect ladder must grow instead of repeating the base interval.

The REST snapshot is deliberately slow (BOUNDARY_DELAY_S), which is the shape
that let the late boundary re-publish "live" in production.

Run: CT_TESTNET=1 pytest tests/testnet/test_miniapp_stream_health_live.py -v

STREAM_HEALTH_ACCOUNT selects the signed account (default copyc2).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

import httpx
import pytest

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.aster.user_stream import UserStream
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.miniapp.live import MiniAppLiveRuntime
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import NonceStore
from copytradeaster.trading.coordinator import FollowerRuntime

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]

ACCOUNT = os.environ.get("STREAM_HEALTH_ACCOUNT", "copyc2")
BOUNDARY_DELAY_S = 8.0
CONNECT_TIMEOUT_S = 45.0
LADDER_ROUNDS = 4


async def _noop() -> None:
    """Awaitable placeholder for callbacks this scenario does not assert."""


class _Registry:
    """Minimal follower registry: one REAL runtime, production-shaped reads."""

    def __init__(self, runtime: FollowerRuntime) -> None:
        self._runtime = runtime

    def snapshot(self) -> list[FollowerRuntime]:
        return [self._runtime]

    def get(self, account_id: str) -> FollowerRuntime | None:
        return self._runtime if account_id == self._runtime.account_id else None


class _Snapshots:
    """Snapshot fan-out seam; the hub only subscribes and applies marks."""

    def __init__(self) -> None:
        self._callback = None
        self.marks: dict = {}

    def subscribe(self, callback):
        self._callback = callback
        return lambda: setattr(self, "_callback", None)

    def apply_mark_prices(self, prices) -> None:
        self.marks = dict(prices)


class _Coordinator:
    """Slow authoritative boundary: the REST read outlives the socket."""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s
        self.started: list[float] = []

    async def snapshot_follower(self, runtime: FollowerRuntime) -> None:
        del runtime
        self.started.append(time.monotonic())
        await asyncio.sleep(self._delay_s)


class _App:
    def __init__(self, cfg, db, registry, coordinator, snapshots, client) -> None:
        self.cfg = cfg
        self.db = db
        self.follower_registry = registry
        self.coordinator = coordinator
        self.follower_snapshots = snapshots
        self.master_client = client


class _RecordingClient:
    """Delegate everything; timestamp every listenKey handout."""

    def __init__(self, inner: AsterClient) -> None:
        self._inner = inner
        self.listen_keys: list[float] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def start_listen_key(self, account_id: str) -> str:
        key = await self._inner.start_listen_key(account_id)
        self.listen_keys.append(time.monotonic())
        return key


@pytest.fixture
async def live(tmp_path: Path):
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail("signed tests are restricted to testnet")
    creds_db = Database(settings.db_path)
    await creds_db.connect()
    scratch = Database(tmp_path / "stream-health.sqlite3")
    await scratch.connect()
    await scratch.migrate()
    try:
        creds = SqliteCredentials(creds_db, settings.encryption_key_bytes())
        await creds.load()
        if ACCOUNT not in creds.account_ids():
            pytest.skip(f"{ACCOUNT} credentials are not imported")
        http = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        limiter = RateLimits(
            settings.weight_limit_per_min,
            settings.orders_limit_per_min,
            settings.orders_limit_per_10s,
        )
        client = AsterClient(
            http, settings.rest_base_url, limiter, creds, creds, NonceStore(creds_db)
        )
        try:
            yield settings, client, scratch
        finally:
            await http.aclose()
    finally:
        await scratch.close()
        await creds_db.close()


async def test_dead_socket_never_reads_live_and_backoff_grows(live):
    settings, client, scratch = live
    recorder = _RecordingClient(client)
    runtime = FollowerRuntime(account_id=ACCOUNT, client=recorder, identity=None)
    coordinator = _Coordinator(BOUNDARY_DELAY_S)
    snapshots = _Snapshots()
    app = _App(settings, scratch, _Registry(runtime), coordinator, snapshots, recorder)
    hub_runtime = MiniAppLiveRuntime(app)
    queue, _hello = hub_runtime.hub.subscribe(ACCOUNT)

    events: list[tuple[float, dict]] = []
    reading = asyncio.Event()
    reading.set()

    async def reader() -> None:
        while reading.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            events.append((time.monotonic(), event))

    reader_task = asyncio.create_task(reader())

    def health_events(health: str, *, after: float | None = None) -> list[float]:
        return [
            stamp
            for stamp, event in events
            if event.get("type") == "health"
            and event.get("health") == health
            and (after is None or stamp > after)
        ]

    async def wait_health(health: str, timeout_s: float) -> float:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            stamps = health_events(health)
            if stamps:
                return stamps[-1]
            await asyncio.sleep(0.2)
        raise TimeoutError(f"health {health} was never published: {[e for _, e in events]}")

    async def wait_listen_keys(count: int, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(recorder.listen_keys) >= count:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"only {len(recorder.listen_keys)} listenKeys were handed out")

    async def wait_boundaries(count: int, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(coordinator.started) >= count:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"only {len(coordinator.started)} boundaries started")

    async def kill_socket() -> None:
        # Server-side close: the cluster drops the socket holding this key.
        await client.close_listen_key(ACCOUNT)

    await hub_runtime.start()
    try:
        # The socket must come up, and quotes must be covered (the testnet
        # mark feed is silent, so the REST fallback owns presentation quotes).
        await wait_health("live", CONNECT_TIMEOUT_S)
        assert snapshots.marks, "the REST quote fallback never published a mark"
        assert coordinator.started, "the connected boundary never ran"

        # --- P1-3: kill the socket while a slow boundary is in flight -------- #
        await kill_socket()
        await wait_health("reconnecting", 20.0)
        await wait_boundaries(len(coordinator.started) + 1, 25.0)
        keys_before = len(recorder.listen_keys)
        kill_at = time.monotonic()
        await kill_socket()
        await asyncio.sleep(BOUNDARY_DELAY_S + 8.0)
        next_key_at = next(
            (stamp for stamp in recorder.listen_keys[keys_before:] if stamp > kill_at),
            None,
        )
        assert next_key_at is not None, "the stream never reconnected"
        false_live = health_events("live", after=kill_at)
        false_live = [stamp for stamp in false_live if stamp < next_key_at + BOUNDARY_DELAY_S]
        assert not false_live, (
            "health read live while the socket that owned the boundary was dead: "
            f"live at {[round(stamp - kill_at, 2) for stamp in false_live]}s after the kill"
        )
        assert health_events("live"), "the stream never reported live at all"

        # The honest end state is a visible outage, not a stale "live".
        await kill_socket()
        await wait_health("reconnecting", 20.0)
        last_health = [event for _, event in events if event.get("type") == "health"][-1]
        assert last_health["health"] == "reconnecting", last_health

        # --- P1-4: fast death must grow the ladder, never repeat the base ---- #
        # The ladder is measured on a fresh UserStream: the hub's stream above
        # has already climbed the ladder (by design), so its next interval
        # would be the tail of that climb rather than the documented 1-2-4.
        # Stopping the hub first also keeps one account-level listenKey owner.
        await hub_runtime.stop()
        recorder.listen_keys.clear()
        stream = UserStream(
            client=recorder,
            account_id=ACCOUNT,
            ws_base=settings.ws_user_base,
            keepalive_min=settings.listenkey_keepalive_min,
            on_event=lambda _event: _noop(),
            on_reconnect=lambda: _noop(),
        )
        stream_stop = asyncio.Event()
        stream_task = asyncio.create_task(stream.run(stream_stop))
        try:
            ladder: list[float] = []
            for _round in range(LADDER_ROUNDS):
                await wait_listen_keys(len(recorder.listen_keys) + 1, CONNECT_TIMEOUT_S)
                ladder.append(recorder.listen_keys[-1])
                await asyncio.sleep(0.8)
                await kill_socket()
            spans = [later - earlier for earlier, later in zip(ladder, ladder[1:], strict=False)]
            assert len(spans) == LADDER_ROUNDS - 1, spans
            assert spans[-1] >= 3.0, f"reconnect backoff stayed near the base interval: {spans}"
            assert spans[-1] > spans[0] + 0.8, f"reconnect backoff did not grow: {spans}"
        finally:
            stream_stop.set()
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
    finally:
        reading.clear()
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await hub_runtime.stop()
        with contextlib.suppress(Exception):
            await client.close_listen_key(ACCOUNT)
