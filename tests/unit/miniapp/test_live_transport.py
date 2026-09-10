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
    live._market_live = True
    live._quotes_live = False
    live._mark_fallback_at = 0.0

    stop = asyncio.Event()
    await live._run_follower(runtime, stop)

    # One authoritative REST boundary on connect + one coalesced refresh for
    # the entire private-stream burst. The WebSocket never mirrors 20 commands.
    assert coordinator.calls == 2


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
    live._market_live = False
    live._quotes_live = False
    live._mark_fallback_at = 0.0
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
    live._market_live = False
    live._quotes_live = False
    live._mark_fallback_at = 0.0
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

    assert live._market_live is False
    assert live._quotes_live is False
    assert ws.closes >= 1
    # No frame means no claim of liveness: the rail must never read `live`
    # off a socket that only acknowledged the subscription.
    assert hub._health.get("a") != "live"


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
    live._market_live = False  # the mark socket opened but stays silent
    live._quotes_live = False
    live._mark_fallback_at = 0.0
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
    live._market_live = True
    live._quotes_live = True
    live._mark_fallback_at = 0.0
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
