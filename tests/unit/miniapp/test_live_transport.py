from datetime import UTC, datetime, timedelta
from decimal import Decimal

from copytradeaster.domain.models import AccountState, PositionKey, PositionState, PosSide
from copytradeaster.miniapp.live import LiveGrant, LiveTicketStore, MiniAppLiveHub, _mark_prices
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
    from copytradeaster.miniapp.live import MiniAppLiveRuntime

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

    stop = asyncio.Event()
    await live._run_follower(runtime, stop)

    # One authoritative REST boundary on connect + one coalesced refresh for
    # the entire private-stream burst. The WebSocket never mirrors 20 commands.
    assert coordinator.calls == 2
