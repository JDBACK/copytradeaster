import asyncio
import time

import pytest

from copytradeaster.aster.user_stream import StreamEvent, UserStream


class FakeClient:
    def __init__(self, listen_keys: list[str]) -> None:
        self._keys = list(listen_keys)
        self.keepalives = 0

    async def start_listen_key(self, account_id: str) -> str:
        if not self._keys:
            raise AssertionError("listen key exhausted")
        return self._keys.pop(0)

    async def keepalive_listen_key(self, account_id: str) -> None:
        self.keepalives += 1


class FakeWS:
    """Async-iterable fake with send/recv per the plan's protocol test."""

    def __init__(self, messages: list, hang_forever: bool = True) -> None:
        self.messages = list(messages)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def send(self, msg):
        pass

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)  # hang until cancelled/stop


class FlakyFactory:
    """Raises N times, then yields the given ws objects in order."""

    def __init__(self, failures: int, sockets: list[FakeWS]) -> None:
        self.failures = failures
        self.sockets = sockets
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("boom")
        if not self.sockets:
            self.sockets.append(FakeWS([]))
        return self.sockets.pop(0)


def make_stream(client, factory, on_event, on_reconnect=None, **kwargs):
    return UserStream(
        client=client,
        account_id="main",
        ws_base="wss://stream.test/ws",
        keepalive_min=50,
        on_event=on_event,
        on_reconnect=on_reconnect or (lambda: asyncio.sleep(0)),
        ws_factory=factory,
        backoff_base_s=kwargs.pop("backoff_base_s", 0.05),
        backoff_max_s=kwargs.pop("backoff_max_s", 0.2),
        max_session_s=kwargs.pop("max_session_s", 3600.0),
        **kwargs,
    )


async def test_events_delivered_to_on_event():
    events: list[StreamEvent] = []
    connected: list[int] = []

    async def collect(event: StreamEvent) -> None:
        events.append(event)

    async def connected_cb() -> None:
        connected.append(1)

    ws = FakeWS(
        [
            b'{"e":"ACCOUNT_UPDATE","E":1,"a":{"m":"ORDER","B":[],"P":[]}}',
            '{"e":"MARGIN_CALL","E":2,"cw":"1","p":[]}',
        ]
    )
    factory = FlakyFactory(0, [ws])
    client = FakeClient(["lk1"])
    stream = make_stream(client, factory, collect, on_connected=connected_cb)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    for _ in range(200):
        if len(events) >= 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert [e.raw["e"] for e in events] == ["ACCOUNT_UPDATE", "MARGIN_CALL"]
    assert factory.urls == ["wss://stream.test/ws/lk1"]
    assert events[0].received_at > 0
    assert connected == [1]


async def test_receiver_buffers_frames_while_connected_boundary_runs():
    """PM-B2: frames already waiting on the socket must be tagged/queued
    while the post-connect authoritative snapshot callback is still running."""
    received = asyncio.Event()
    boundary_completed = asyncio.Event()

    async def collect(event: StreamEvent) -> None:
        assert event.raw["e"] == "ORDER_TRADE_UPDATE"
        received.set()

    async def connected_cb() -> None:
        await asyncio.wait_for(received.wait(), timeout=0.5)
        boundary_completed.set()

    ws = FakeWS(['{"e":"ORDER_TRADE_UPDATE","E":1,"o":{"x":"TRADE"}}'])
    stream = make_stream(
        FakeClient(["lk1"]),
        FlakyFactory(0, [ws]),
        collect,
        on_connected=connected_cb,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        await asyncio.wait_for(boundary_completed.wait(), timeout=1)
    finally:
        stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_disconnect_callback_runs_before_reconnect_backoff():
    disconnected = asyncio.Event()

    async def on_disconnected() -> None:
        disconnected.set()

    stream = make_stream(
        FakeClient(["lk1", "lk2"]),
        FlakyFactory(0, [FakeWS(['{"e":"listenKeyExpired","E":5}'])]),
        lambda event: asyncio.sleep(0),
        on_disconnected=on_disconnected,
        backoff_base_s=1,
        backoff_max_s=1,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        await asyncio.wait_for(disconnected.wait(), timeout=0.5)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


async def test_disconnect_during_connected_boundary_is_reported_before_boundary_finishes():
    """A socket that dies during the REST boundary must close admission before
    the connected callback can finish and publish READY."""
    disconnected = asyncio.Event()
    boundary_observed_disconnect = asyncio.Event()

    async def on_connected() -> None:
        await asyncio.wait_for(disconnected.wait(), timeout=0.5)
        boundary_observed_disconnect.set()

    async def on_disconnected() -> None:
        disconnected.set()

    stream = make_stream(
        FakeClient(["lk1"]),
        FlakyFactory(0, [FakeWS(['{"e":"listenKeyExpired","E":5}'])]),
        lambda event: asyncio.sleep(0),
        on_connected=on_connected,
        on_disconnected=on_disconnected,
        backoff_base_s=1,
        backoff_max_s=1,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        await asyncio.wait_for(boundary_observed_disconnect.wait(), timeout=1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


async def test_connected_boundary_failure_reports_disconnect_and_stops_admission_cycle():
    disconnected = asyncio.Event()
    stop = asyncio.Event()

    async def on_connected() -> None:
        raise RuntimeError("snapshot unavailable")

    async def on_disconnected() -> None:
        disconnected.set()
        stop.set()

    stream = make_stream(
        FakeClient(["lk1"]),
        FlakyFactory(0, [FakeWS([])]),
        lambda event: asyncio.sleep(0),
        on_connected=on_connected,
        on_disconnected=on_disconnected,
    )

    await asyncio.wait_for(stream.run(stop), timeout=1)
    assert disconnected.is_set()


async def test_listen_key_expired_rotates_and_reconnects():
    events: list[StreamEvent] = []
    reconnected: list[int] = []

    async def collect(event: StreamEvent) -> None:
        events.append(event)

    ws1 = FakeWS(['{"e":"listenKeyExpired","E":5}'])
    ws2 = FakeWS(['{"e":"ACCOUNT_UPDATE","E":6,"a":{"m":"ORDER","B":[],"P":[]}}'])
    factory = FlakyFactory(0, [ws1, ws2])
    client = FakeClient(["lk-a", "lk-b"])
    stream = make_stream(client, factory, collect, on_reconnect=lambda: reconnected.append(1))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    for _ in range(200):
        if len(events) >= 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert factory.urls[0].endswith("/lk-a")
    assert any(u.endswith("/lk-b") for u in factory.urls[1:])
    assert reconnected, "on_reconnect must fire after rotation"


async def test_backoff_grows_between_failures():
    attempts: list[float] = []

    class TimeFactory:
        def __call__(self, url):
            attempts.append(time.monotonic())
            raise ConnectionError("down")

    client = FakeClient(["k"] * 50)
    stream = make_stream(client, TimeFactory(), lambda e: asyncio.sleep(0))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(0.6)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(attempts) >= 3
    gaps = [attempts[i + 1] - attempts[i] for i in range(len(attempts) - 1)]
    # base 0.05 -> gap1 in [0.05, 0.065]; gap2 (base 0.1) in [0.1, 0.13]: strictly larger
    assert gaps[1] > gaps[0]


async def test_stop_event_terminates_promptly():
    ws = FakeWS([])
    factory = FlakyFactory(0, [ws])
    client = FakeClient(["lk1"])
    stream = make_stream(client, factory, lambda e: asyncio.sleep(0))
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    started = time.monotonic()
    await asyncio.wait_for(task, timeout=2)
    assert time.monotonic() - started < 1.0
    assert ws.closed


async def test_stop_race_consumes_recv_exception() -> None:
    stop = asyncio.Event()

    class ClosingWS(FakeWS):
        def __init__(self) -> None:
            super().__init__([])
            self.recv_task: asyncio.Task | None = None

        async def recv(self):
            self.recv_task = asyncio.current_task()
            await stop.wait()
            raise ConnectionError("closed with stop")

    ws = ClosingWS()
    stream = make_stream(
        FakeClient(["lk1"]),
        FlakyFactory(0, [ws]),
        lambda event: asyncio.sleep(0),
    )
    task = asyncio.create_task(stream.run(stop))
    while ws.recv_task is None:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert ws.recv_task.done()
    # CPython keeps this flag true until a task exception is observed. This is
    # the exact regression that previously surfaced at process shutdown.
    assert ws.recv_task._log_traceback is False  # type: ignore[attr-defined]


async def test_parent_cancellation_cancels_recv_child() -> None:
    started = asyncio.Event()

    class HangingWS(FakeWS):
        def __init__(self) -> None:
            super().__init__([])
            self.recv_task: asyncio.Task | None = None

        async def recv(self):
            self.recv_task = asyncio.current_task()
            started.set()
            await asyncio.sleep(3600)

    ws = HangingWS()
    stop = asyncio.Event()
    stream = make_stream(
        FakeClient(["lk1"]),
        FlakyFactory(0, [ws]),
        lambda event: asyncio.sleep(0),
    )
    task = asyncio.create_task(stream.run(stop))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ws.recv_task is not None
    assert ws.recv_task.cancelled()


@pytest.mark.parametrize("keepalive_min", [50])
async def test_keepalive_sends_put(keepalive_min):
    client = FakeClient(["lk1"])

    class ShortStream(UserStream):
        pass

    events: list[StreamEvent] = []
    ws = FakeWS([])
    factory = FlakyFactory(0, [ws])
    stream = UserStream(
        client=client,
        account_id="main",
        ws_base="wss://s/ws",
        keepalive_min=0,  # 0 minutes -> immediate first keepalive tick
        on_event=events.append,
        on_reconnect=lambda: asyncio.sleep(0),
        ws_factory=factory,
        backoff_base_s=0.05,
        backoff_max_s=0.1,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert client.keepalives >= 1
