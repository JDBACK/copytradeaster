import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from copytradeaster.domain.models import AccountState, PositionKey, PositionState, PosSide
from copytradeaster.miniapp.live import (
    LiveGrant,
    LiveTicketStore,
    MiniAppLiveHub,
    MiniAppLiveRuntime,
    _mark_prices,
)
from copytradeaster.trading.snapshots import FollowerSnapshotStore


def streaming_frames(*symbols: str) -> dict[str, float]:
    """Fresh per-symbol stream frame stamps, as the mark loop records them."""
    import time

    return {symbol: time.monotonic() for symbol in symbols}


async def test_live_ticket_is_single_use_and_expires() -> None:
    now = [100.0]
    store = LiveTicketStore(ttl_s=5.0, clock=lambda: now[0])
    grant = LiveGrant(user_id=7, account_id="follower-7")
    first = await store.issue(grant)
    assert await store.consume(first) == grant
    assert await store.consume(first) is None

    expired = await store.issue(grant)
    now[0] = 106.0
    assert await store.consume(expired) is None


async def test_live_ticket_uses_miniapp_access_not_notification_block_state() -> None:
    from types import SimpleNamespace

    class Users:
        access_enabled = 1

        async def get(self, user_id: int):
            assert user_id == 7
            return {
                "telegram_user_id": 7,
                "account_id": "follower-7",
                "access_enabled": self.access_enabled,
                # Notification mute / Telegram delivery failure is not the
                # Mini App authorization boundary; access_enabled is.
                "blocked_at": "2026-09-10T00:00:00+00:00",
            }

    follower = object()
    users = Users()
    live = object.__new__(MiniAppLiveRuntime)
    live._users = users
    live.tickets = LiveTicketStore()
    live.app = SimpleNamespace(
        follower_registry=SimpleNamespace(
            get=lambda account_id: follower if account_id == "follower-7" else None
        )
    )

    issued = await live.issue_ticket(7)
    assert issued is not None
    assert await live.consume_ticket(issued["ticket"]) == LiveGrant(7, "follower-7")

    users.access_enabled = 0
    assert await live.issue_ticket(7) is None


def test_live_hub_is_account_scoped_and_latest_wins_when_client_is_slow() -> None:
    hub = MiniAppLiveHub(queue_max=1)
    account_a, initial_a = hub.subscribe("a")
    account_b, _ = hub.subscribe("b")
    assert initial_a["revision"] == 0

    hub.publish_health("a", "reconnecting")
    hub.publish_health("a", "live")

    assert account_a.qsize() == 1
    latest = account_a.get_nowait()
    assert latest["health"] == "live"
    assert latest["revision"] == 2
    assert account_b.empty()


def test_live_hub_stamps_events_with_the_process_epoch() -> None:
    """A restarted process must be distinguishable from out-of-order delivery.

    Revisions restart at 0 with the process, so the epoch is what tells a
    client that its watermark belongs to a previous run and must be reset
    instead of discarding every new event as stale.
    """
    hub = MiniAppLiveHub()
    queue, initial = hub.subscribe("a")
    assert initial["epoch"]

    hub.publish_health("a", "live")
    published = queue.get_nowait()
    assert published["epoch"] == initial["epoch"]
    assert published["revision"] == 1

    restarted = MiniAppLiveHub()
    _, restarted_initial = restarted.subscribe("a")
    assert restarted_initial["epoch"] != initial["epoch"]
    assert restarted_initial["revision"] == 0


def test_mark_price_parser_is_decimal_whitelisted_and_secret_free() -> None:
    payload = '[{"s":"BTCUSDT","p":"65432.10"},{"s":"ETHUSDT","p":"0"},{"s":"OTHER","p":"9"}]'
    assert _mark_prices(payload, {"BTCUSDT", "ETHUSDT"}) == {"BTCUSDT": Decimal("65432.10")}


def test_snapshot_store_mark_update_preserves_rest_capture_age_and_advances_revision() -> None:
    captured = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)
    published = captured + timedelta(seconds=1)
    store = FollowerSnapshotStore(clock=lambda: captured)
    key = PositionKey("BTCUSDT", PosSide.LONG)
    state = AccountState(
        account_id="follower-1",
        available_usdt=Decimal("1000"),
        equity_usdt=Decimal("1010"),
        positions={
            key: PositionState(
                key=key,
                qty=Decimal("0.01"),
                entry_price=Decimal("60000"),
                leverage=5,
                mark_price=Decimal("61000"),
                liquidation_price=Decimal("50000"),
                unrealized_profit=Decimal("10"),
            )
        },
    )
    first = store.publish(state, captured_at=captured)

    (quote,) = store.apply_mark_prices({"BTCUSDT": Decimal("62000")}, published_at=published)

    assert quote.revision == first.revision + 1
    assert quote.captured_at == captured
    assert quote.published_at == published
    assert quote.source == "mark-price"
    assert quote.state.positions[key].unrealized_profit == Decimal("20")
    assert quote.state.equity_usdt == Decimal("1020")


async def test_follower_live_burst_coalesces_to_one_post_boundary_rest_refresh() -> None:
    import asyncio
    from types import SimpleNamespace

    from copytradeaster.aster.user_stream import StreamEvent

    class Registry:
        def __init__(self) -> None:
            self.runtime = None

        def get(self, account_id: str):
            assert account_id == "follower-1"
            return self.runtime

    class Coordinator:
        def __init__(self) -> None:
            self.calls = 0

        async def snapshot_follower(self, runtime) -> None:
            self.calls += 1

    class BurstStream:
        def __init__(self, **kwargs) -> None:
            self.on_event = kwargs["on_event"]
            self.on_connected = kwargs["on_connected"]

        async def run(self, stop: asyncio.Event) -> None:
            await self.on_connected()
            for _ in range(20):
                await self.on_event(StreamEvent(raw={"e": "ACCOUNT_UPDATE"}, received_at=0.0))
            await asyncio.sleep(0.3)
            stop.set()

    registry = Registry()
    coordinator = Coordinator()
    app = SimpleNamespace(
        follower_registry=registry,
        coordinator=coordinator,
        cfg=SimpleNamespace(ws_user_base="wss://example.invalid", listenkey_keepalive_min=50),
    )
    runtime = SimpleNamespace(account_id="follower-1", client=object())
    registry.runtime = runtime

    live = object.__new__(MiniAppLiveRuntime)
    live.app = app
    live.hub = MiniAppLiveHub()
    live._user_stream_factory = BurstStream
    live._private_health = {}
    live._market_frames = streaming_frames("BTCUSDT")
    live._quotes_live = False
    live._fallback_quotes_at = {}

    stop = asyncio.Event()
    await live._run_follower(runtime, stop)

    # One authoritative REST boundary on connect + one coalesced refresh for
    # the entire private-stream burst. The WebSocket never mirrors 20 commands.
    assert coordinator.calls == 2


async def test_slow_boundary_after_a_dead_socket_never_reads_live() -> None:
    """A REST boundary that outlives its socket must not restore ``live``."""
    import asyncio
    from types import SimpleNamespace

    release = asyncio.Event()
    boundary_started = asyncio.Event()

    class Registry:
        def __init__(self) -> None:
            self.runtime = None

        def get(self, account_id: str):
            assert account_id == "follower-1"
            return self.runtime

    class Coordinator:
        async def snapshot_follower(self, runtime) -> None:
            boundary_started.set()
            await release.wait()

    class DyingStream:
        def __init__(self, **kwargs) -> None:
            self.on_connected = kwargs["on_connected"]
            self.on_disconnected = kwargs["on_disconnected"]

        async def run(self, stop: asyncio.Event) -> None:
            # The socket accepts, starts the authoritative boundary, dies while
            # that boundary is still in flight, and only then lets it finish.
            boundary = asyncio.create_task(self.on_connected())
            await asyncio.sleep(0)
            await self.on_disconnected()
            await boundary

    registry = Registry()
    hub = MiniAppLiveHub()
    queue, _ = hub.subscribe("follower-1")
    app = SimpleNamespace(
        follower_registry=registry,
        coordinator=Coordinator(),
        cfg=SimpleNamespace(ws_user_base="wss://example.invalid", listenkey_keepalive_min=50),
    )
    runtime = SimpleNamespace(account_id="follower-1", client=object())
    registry.runtime = runtime

    live = object.__new__(MiniAppLiveRuntime)
    live.app = app
    live.hub = hub
    live._user_stream_factory = DyingStream
    live._private_health = {}
    live._quotes_live = True

    stop = asyncio.Event()
    task = asyncio.create_task(live._run_follower(runtime, stop))
    await asyncio.wait_for(boundary_started.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert live._private_health.get("follower-1") is False
    assert [event for event in events if event.get("health") == "live"] == []


async def test_market_stream_parent_cancellation_cleans_recv_child() -> None:
    import asyncio
    from types import SimpleNamespace

    import pytest

    started = asyncio.Event()

    class HangingWS:
        def __init__(self) -> None:
            self.recv_task: asyncio.Task | None = None
            self.subscriptions: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, payload: str) -> None:
            self.subscriptions.append(payload)

        async def recv(self):
            self.recv_task = asyncio.current_task()
            started.set()
            await asyncio.sleep(3600)

    ws = HangingWS()
    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = {}
    live._quotes_live = False
    live._fallback_quotes_at = {}
    live._private_health = {}
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=lambda prices: None),
    )
    live._market_ws_factory = lambda url: ws

    task = asyncio.create_task(live._run_market_stream())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ws.recv_task is not None
    assert ws.recv_task.cancelled()
    # The stream is established through the SUBSCRIBE protocol: the documented
    # raw path is silent on the Aster testnet cluster.
    assert ws.subscriptions and '"method": "SUBSCRIBE"' in ws.subscriptions[0]
    assert "btcusdt@markPrice@1s" in ws.subscriptions[0]


async def test_silent_mark_stream_is_not_claimed_live() -> None:
    """A socket that opens but never produces a frame must not read as live."""
    import asyncio
    from types import SimpleNamespace

    import pytest

    class SilentWS:
        def __init__(self) -> None:
            self.closes = 0
            self.sent = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closes += 1
            return False

        async def send(self, payload: str) -> None:
            self.sent += 1

        async def recv(self):
            await asyncio.sleep(3600)

    ws = SilentWS()
    hub = MiniAppLiveHub()
    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = {}
    live._quotes_live = False
    live._fallback_quotes_at = {}
    live._private_health = {"a": True}
    live.hub = hub
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=lambda prices: None),
    )
    live._market_ws_factory = lambda url: ws

    import copytradeaster.miniapp.live as live_module

    original = live_module.LIVE_MARK_ACK_TIMEOUT_S
    live_module.LIVE_MARK_ACK_TIMEOUT_S = 0.05
    try:
        task = asyncio.create_task(live._run_market_stream())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        live_module.LIVE_MARK_ACK_TIMEOUT_S = original

    assert live._market_frames == {}
    assert live._quotes_live is False
    assert ws.closes >= 1
    # No frame means no claim of liveness: the rail must never read `live`
    # off a socket that only acknowledged the subscription.
    assert hub._health.get("a") != "live"


async def test_subscription_ack_alone_never_claims_live_quotes() -> None:
    """The ack is a frame too; only a mark price may claim stream liveness."""
    import asyncio
    import json
    from types import SimpleNamespace

    import pytest

    class AckOnlyWS:
        def __init__(self) -> None:
            self.acks = 0
            self.closes = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closes += 1
            return False

        async def send(self, payload: str) -> None:
            pass

        async def recv(self):
            if self.acks == 0:
                self.acks += 1
                return json.dumps({"id": 1, "result": None})
            await asyncio.sleep(3600)

    ws = AckOnlyWS()
    hub = MiniAppLiveHub()
    queue, _ = hub.subscribe("a")
    applied: list[dict[str, Decimal]] = []
    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = {}
    live._quotes_live = False
    live._fallback_quotes_at = {}
    live._private_health = {"a": True}
    live.hub = hub
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=lambda prices: applied.append(prices)),
    )
    live._market_ws_factory = lambda url: ws

    import copytradeaster.miniapp.live as live_module

    original = live_module.LIVE_MARK_ACK_TIMEOUT_S
    live_module.LIVE_MARK_ACK_TIMEOUT_S = 0.05
    try:
        task = asyncio.create_task(live._run_market_stream())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        live_module.LIVE_MARK_ACK_TIMEOUT_S = original

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert ws.acks == 1
    assert applied == []
    assert live._market_frames == {}
    assert live._quotes_live is False
    # The acknowledgement must not be published as a live verdict: the rail
    # would otherwise claim flowing quotes while PnL stands still.
    assert [event for event in events if event.get("health") == "live"] == []


async def test_rest_mark_fallback_keeps_quotes_live_without_frames() -> None:
    """The silent-cluster case: REST marks fill the presentation quote path."""
    import asyncio
    from types import SimpleNamespace

    import pytest

    applied: list[dict[str, Decimal]] = []
    requested: list[list[str]] = []

    async def mark_prices(symbols: list[str]) -> dict[str, Decimal]:
        requested.append(list(symbols))
        return {"BTCUSDT": Decimal("70000.5")}

    hub = MiniAppLiveHub()
    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = {}  # the mark socket opened but stays silent
    live._quotes_live = False
    live._fallback_quotes_at = {}
    live._private_health = {"a": True}
    live.hub = hub
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=applied.append),
        master_client=SimpleNamespace(mark_prices=mark_prices),
    )

    import copytradeaster.miniapp.live as live_module

    original = live_module.MARK_FALLBACK_INTERVAL_S
    live_module.MARK_FALLBACK_INTERVAL_S = 0.05
    try:
        task = asyncio.create_task(live._run_mark_fallback())
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        live_module.MARK_FALLBACK_INTERVAL_S = original

    assert requested and requested[0] == ["BTCUSDT"]
    assert applied and applied[0] == {"BTCUSDT": Decimal("70000.5")}
    assert live._quotes_live is True
    assert hub._health.get("a") == "live"


async def test_mark_fallback_stands_down_while_stream_frames_flow() -> None:
    """The shared stream stays primary: no extra REST while frames arrive."""
    import asyncio
    from types import SimpleNamespace

    import pytest

    calls: list[list[str]] = []

    async def mark_prices(symbols: list[str]) -> dict[str, Decimal]:
        calls.append(list(symbols))
        return {"BTCUSDT": Decimal("70001")}

    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = streaming_frames("BTCUSDT")
    live._quotes_live = True
    live._fallback_quotes_at = {}
    live._private_health = {}
    live.hub = MiniAppLiveHub()
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=lambda prices: None),
        master_client=SimpleNamespace(mark_prices=mark_prices),
    )

    import copytradeaster.miniapp.live as live_module

    original = live_module.MARK_FALLBACK_INTERVAL_S
    live_module.MARK_FALLBACK_INTERVAL_S = 0.05
    try:
        task = asyncio.create_task(live._run_mark_fallback())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        live_module.MARK_FALLBACK_INTERVAL_S = original

    assert calls == []


def _fallback_runtime(whitelist, mark_prices, applied):
    import asyncio
    from types import SimpleNamespace

    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = streaming_frames("BTCUSDT")
    live._fallback_quotes_at = {}
    live._quotes_live = False
    live._private_health = {"a": True}
    live.hub = MiniAppLiveHub()
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(ws_user_base="wss://example.invalid/ws", symbol_whitelist=whitelist),
        follower_snapshots=SimpleNamespace(apply_mark_prices=applied.append),
        master_client=SimpleNamespace(mark_prices=mark_prices),
    )
    return live


async def _run_fallback_briefly(live) -> None:
    import asyncio

    import copytradeaster.miniapp.live as live_module

    original = live_module.MARK_FALLBACK_INTERVAL_S
    live_module.MARK_FALLBACK_INTERVAL_S = 0.05
    try:
        task = asyncio.create_task(live._run_mark_fallback())
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        live_module.MARK_FALLBACK_INTERVAL_S = original


def test_mark_frames_coalesce_per_symbol_and_hold_the_render_cadence() -> None:
    """Quotes reach the UI once per window, carrying the latest per symbol.

    Publishing every frame re-invalidated each client DTO faster than its own
    weak read could finish, so the response was always fenced by the next quote
    and the screen never converged on fresh marks.
    """
    from types import SimpleNamespace

    applied: list[dict[str, Decimal]] = []
    live = object.__new__(MiniAppLiveRuntime)
    live.app = SimpleNamespace(follower_snapshots=SimpleNamespace(apply_mark_prices=applied.append))

    live._queue_mark_prices({"BTCUSDT": Decimal("70000")}, at=100.0)
    assert applied == [{"BTCUSDT": Decimal("70000")}]

    # Both symbols arrive inside the window: one publication, latest per symbol.
    live._queue_mark_prices({"ETHUSDT": Decimal("3000")}, at=100.4)
    live._queue_mark_prices({"BTCUSDT": Decimal("70001")}, at=100.9)
    assert len(applied) == 1

    live._queue_mark_prices({"ETHUSDT": Decimal("3001")}, at=101.0)
    assert applied[-1] == {"BTCUSDT": Decimal("70001"), "ETHUSDT": Decimal("3001")}
    assert len(applied) == 2


async def test_partial_mark_feed_falls_back_only_for_the_silent_symbol() -> None:
    """One streaming symbol must not keep the fallback idle for another."""

    requested: list[list[str]] = []
    applied: list[dict[str, Decimal]] = []

    async def mark_prices(symbols: list[str]) -> dict[str, Decimal]:
        requested.append(list(symbols))
        return {"ETHUSDT": Decimal("3000")}

    live = _fallback_runtime(["BTCUSDT", "ETHUSDT"], mark_prices, applied)
    await _run_fallback_briefly(live)

    # BTCUSDT is streaming, so its feed is never re-read over REST; the silent
    # symbol is the only thing the fallback pays for.
    assert requested and all(row == ["ETHUSDT"] for row in requested)
    assert applied and applied[0] == {"ETHUSDT": Decimal("3000")}
    assert live._quotes_live is True
    assert live.hub._health.get("a") == "live"
    assert live._silent_symbols() == []


async def test_streaming_symbol_never_masks_a_dead_one() -> None:
    """A live BTCUSDT feed must not report ETHUSDT quotes as healthy."""
    import time

    import copytradeaster.miniapp.live as live_module

    requested: list[list[str]] = []
    applied: list[dict[str, Decimal]] = []

    async def mark_prices(symbols: list[str]) -> dict[str, Decimal]:
        requested.append(list(symbols))
        return {"BTCUSDT": Decimal("70000")}

    live = _fallback_runtime(["BTCUSDT", "ETHUSDT"], mark_prices, applied)
    # Both symbols were streaming and the account read live; then ETHUSDT stops
    # delivering frames while BTCUSDT keeps going.
    live._market_frames = streaming_frames("BTCUSDT", "ETHUSDT")
    live._market_frames["ETHUSDT"] = time.monotonic() - live_module.MARK_FRAME_FRESH_S - 1.0
    live._quotes_live = True
    live.hub.publish_health("a", "live")
    await _run_fallback_briefly(live)

    assert requested and all(row == ["ETHUSDT"] for row in requested)
    assert live._silent_symbols() == ["ETHUSDT"]
    assert live._quotes_live is False
    assert live.hub._health.get("a") == "reconnecting"


async def test_mark_frame_from_the_stream_claims_live_quotes() -> None:
    """A frame with a price is the proof; the ack before it never was."""
    import asyncio
    import json
    from types import SimpleNamespace

    import pytest

    class FrameWS:
        def __init__(self) -> None:
            self.frames = [
                json.dumps({"id": 1, "result": None}),
                json.dumps({"e": "markPriceUpdate", "s": "BTCUSDT", "p": "70000.5"}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, payload: str) -> None:
            pass

        async def recv(self):
            if self.frames:
                return self.frames.pop(0)
            await asyncio.sleep(3600)

    ws = FrameWS()
    hub = MiniAppLiveHub()
    queue, _ = hub.subscribe("a")
    applied: list[dict[str, Decimal]] = []
    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = {}
    live._quotes_live = False
    live._fallback_quotes_at = {}
    live._private_health = {"a": True}
    live.hub = hub
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=applied.append),
    )
    live._market_ws_factory = lambda url: ws

    task = asyncio.create_task(live._run_market_stream())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert applied == [{"BTCUSDT": Decimal("70000.5")}]
    # The published verdict is the proof: the teardown afterwards (cancellation
    # clears the claim, by design) must not be mistaken for "never live".
    assert [event for event in events if event.get("health") == "live"]


async def test_silent_stream_backs_off_instead_of_reconnecting_forever() -> None:
    """An accepted-but-silent socket must not buy a handshake every second."""
    import asyncio
    import time
    from types import SimpleNamespace

    import pytest

    import copytradeaster.miniapp.live as live_module

    opens: list[float] = []

    class SilentWS:
        async def __aenter__(self):
            opens.append(time.monotonic())
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, payload: str) -> None:
            pass

        async def recv(self):
            await asyncio.sleep(3600)

    live = object.__new__(MiniAppLiveRuntime)
    live._stop = asyncio.Event()
    live._market_frames = {}
    live._quotes_live = False
    live._fallback_quotes_at = {}
    live._private_health = {}
    live.hub = MiniAppLiveHub()
    live.app = SimpleNamespace(
        cfg=SimpleNamespace(
            ws_user_base="wss://example.invalid/ws",
            symbol_whitelist=["BTCUSDT"],
        ),
        follower_snapshots=SimpleNamespace(apply_mark_prices=lambda prices: None),
    )
    live._market_ws_factory = lambda url: SilentWS()

    originals = (
        live_module.LIVE_MARK_ACK_TIMEOUT_S,
        live_module.MARKET_BACKOFF_INITIAL_S,
        live_module.MARKET_BACKOFF_MAX_S,
    )
    live_module.LIVE_MARK_ACK_TIMEOUT_S = 0.01
    live_module.MARKET_BACKOFF_INITIAL_S = 0.02
    live_module.MARKET_BACKOFF_MAX_S = 0.16
    try:
        task = asyncio.create_task(live._run_market_stream())
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        (
            live_module.LIVE_MARK_ACK_TIMEOUT_S,
            live_module.MARKET_BACKOFF_INITIAL_S,
            live_module.MARKET_BACKOFF_MAX_S,
        ) = originals

    assert len(opens) >= 3
    gaps = [later - earlier for earlier, later in zip(opens, opens[1:], strict=False)]
    # Resetting the backoff on every successful handshake (not on a delivered
    # frame) kept this interval constant, so a silent cluster paid ~7800
    # connections a day for nothing.
    assert gaps[-1] > gaps[0] * 2
