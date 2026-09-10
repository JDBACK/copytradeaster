"""Live acceptance: a dead follower socket must never read as live.

Merge-review correctives P1-3/P1-4. The Mini App derived its health from a REST
boundary that can outlive the socket which started it: a successful snapshot
re-asserted "live" after the stream had already died (P1-3). And the reconnect
backoff reset on the handshake alone, so a socket that dies right after connect
bought a fresh listenKey every base interval forever (P1-4).

This scenario runs the REAL MiniAppLiveRuntime with the REAL UserStream against
the REAL testnet cluster on a disposable follower account:

  * the transport is cut (TCP drop) while the slow authoritative boundary is
    still in flight; health must stay "reconnecting" until a genuinely new
    session starts,
  * four sessions are cut right after they connect; the measured reconnect
    ladder must grow instead of repeating the base interval.

The cut goes through a local CONNECT proxy instead of DELETE /fapi/v3/listenKey
on purpose: deleting the account's key also kills the socket of any other
process streaming that account (a running bot), so the harness must not use it.
A cut connection is the production failure it reproduces, and the abandoned
listenKeys simply expire.

Run: CT_TESTNET=1 pytest tests/testnet/test_live_stream_health_acceptance.py -v

STREAM_HEALTH_ACCOUNT selects the signed account (default copyc2).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect

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
LADDER_TIMEOUT_S = 90.0


async def _noop() -> None:
    """Awaitable placeholder for callbacks this scenario does not assert."""


class _Pipe:
    """One tunnelled connection with both of its endpoints."""

    def __init__(self, client: asyncio.StreamWriter) -> None:
        self.client = client
        self.upstream: asyncio.StreamWriter | None = None

    def close(self) -> None:
        for writer in (self.client, self.upstream):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()


class _ConnectProxy:
    """Minimal CONNECT tunnel whose live connections can be cut on demand.

    The client's TLS stays end to end with the exchange: the proxy only pipes
    bytes, so dropping a pipe is exactly the transport failure of a dead socket.
    """

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._pipes: set[_Pipe] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    @property
    def url(self) -> str:
        assert self._server is not None and self._server.sockets
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def drop(self) -> int:
        pipes = list(self._pipes)
        self._pipes.clear()
        for pipe in pipes:
            pipe.close()
        return len(pipes)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pipe = _Pipe(writer)
        self._pipes.add(pipe)
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = request.decode("latin-1").split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                return
            host, _, port_text = parts[1].rpartition(":")
            # Consume the remaining request headers. The tunnel carries raw
            # TLS afterwards, so the header block is the only HTTP here.
            while await asyncio.wait_for(reader.readline(), timeout=10.0) not in (
                b"\r\n",
                b"",
            ):
                pass
            upstream_reader, upstream_writer = await asyncio.open_connection(
                host,
                int(port_text),
            )
            pipe.upstream = upstream_writer
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                self._pump(reader, upstream_writer),
                self._pump(upstream_reader, writer),
                return_exceptions=True,
            )
        except Exception:  # noqa: BLE001 - a failed tunnel is a dropped socket
            pass
        finally:
            self._pipes.discard(pipe)
            pipe.close()

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()


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
    """Delegate reads; count listenKeys and stay out of the cluster's keys.

    close_listen_key is deliberately inert: the harness must not delete an
    account-level key another process is streaming.
    """

    def __init__(self, inner: AsterClient) -> None:
        self._inner = inner
        self.listen_keys: list[float] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def start_listen_key(self, account_id: str) -> str:
        key = await self._inner.start_listen_key(account_id)
        self.listen_keys.append(time.monotonic())
        return key

    async def close_listen_key(self, account_id: str) -> None:
        del account_id


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
    proxy = _ConnectProxy()
    await proxy.start()
    recorder = _RecordingClient(client)
    runtime = FollowerRuntime(account_id=ACCOUNT, client=recorder, identity=None)
    coordinator = _Coordinator(BOUNDARY_DELAY_S)
    snapshots = _Snapshots()
    app = _App(settings, scratch, _Registry(runtime), coordinator, snapshots, recorder)

    def tunneled_user_stream(**kwargs: Any) -> UserStream:
        return UserStream(
            **kwargs,
            ws_factory=lambda url: ws_connect(url, proxy=proxy.url, open_timeout=20.0),
        )

    hub_runtime = MiniAppLiveRuntime(app, user_stream_factory=tunneled_user_stream)
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

    await hub_runtime.start()
    try:
        # The socket must come up and quotes must be covered (the testnet mark
        # feed is silent, so the REST fallback owns presentation quotes).
        await wait_health("live", CONNECT_TIMEOUT_S)
        assert snapshots.marks, "the REST quote fallback never published a mark"
        assert coordinator.started, "the connected boundary never ran"

        # --- P1-3: cut the transport while a slow boundary is in flight ------ #
        proxy.drop()
        await wait_health("reconnecting", 20.0)
        await wait_boundaries(len(coordinator.started) + 1, 25.0)
        keys_before = len(recorder.listen_keys)
        kill_at = time.monotonic()
        await asyncio.sleep(0.5)
        proxy.drop()
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
            f"live at {[round(stamp - kill_at, 2) for stamp in false_live]}s after the cut"
        )
        assert health_events("live"), "the stream never reported live at all"

        # The honest end state is a visible outage, not a stale "live".
        proxy.drop()
        await wait_health("reconnecting", 20.0)
        last_health = [event for _, event in events if event.get("type") == "health"][-1]
        assert last_health["health"] == "reconnecting", last_health

        # --- P1-4: fast death must grow the ladder, never repeat the base ---- #
        # Measured on a fresh UserStream: the hub's stream has already climbed
        # the ladder (by design), so its next interval would be the tail of that
        # climb instead of the documented base. Stopping the hub first also
        # keeps a single account-level listenKey owner among our own processes.
        await hub_runtime.stop()
        recorder.listen_keys.clear()
        stream = UserStream(
            client=recorder,
            account_id=ACCOUNT,
            ws_base=settings.ws_user_base,
            keepalive_min=settings.listenkey_keepalive_min,
            on_event=lambda _event: _noop(),
            on_reconnect=lambda: _noop(),
            ws_factory=lambda url: ws_connect(url, proxy=proxy.url, open_timeout=20.0),
        )
        stream_stop = asyncio.Event()
        stream_task = asyncio.create_task(stream.run(stream_stop))
        try:
            ladder: list[float] = []
            deadline = time.monotonic() + LADDER_TIMEOUT_S
            for _round in range(LADDER_ROUNDS):
                remaining = max(deadline - time.monotonic(), 1.0)
                await wait_listen_keys(len(recorder.listen_keys) + 1, remaining)
                ladder.append(recorder.listen_keys[-1])
                await asyncio.sleep(0.8)
                proxy.drop()
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
        await proxy.stop()
