"""Coordinator routing tests: fan-out, error isolation, pause, branches a-e,
self-stop re-entry guard - all against fake clients, no HTTP."""

import asyncio
import time
from decimal import Decimal as D

import pytest

from copytradeaster.app import App
from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    ExecutionAction,
    PositionKey,
    PositionState,
    PosSide,
    ProtectiveOrder,
    RiskPolicy,
    Side,
    SkipReason,
    SymbolFilters,
    TargetState,
)
from copytradeaster.master.projector import MasterDelta, MasterState
from copytradeaster.notifications.models import NotificationEvent, NotificationSeverity
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CommandRepo,
    CopyTradeRepo,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor
from copytradeaster.trading.registry import FollowerRegistry


class FakeClient:
    def __init__(self, available="1000"):
        self.available = D(available)
        self.positions: dict[PositionKey, dict] = {}
        self.placed: list[dict] = []
        self.leverage_calls: list[tuple[str, int]] = []
        self.margin_type_calls: list[tuple[str, str]] = []
        self.flat_margin_types: dict[str, str] = {}
        self.flat_leverage = 20
        self.query_results: dict[str, object] = {}

    async def balance(self, account_id):
        return [FakeBalance(self.available)]

    async def position_risk(self, account_id, symbol=None):
        del account_id
        rows = [
            FakePosition(k, v)
            for k, v in self.positions.items()
            if symbol is None or k.symbol == symbol
        ]
        for flat_symbol in ("BTCUSDT",):
            if (symbol is None or symbol == flat_symbol) and not any(
                row.symbol == flat_symbol for row in rows
            ):
                rows.append(
                    FakePosition(
                        PositionKey(flat_symbol),
                        {
                            "qty": D(0),
                            "leverage": self.flat_leverage,
                            "margin_type": self.flat_margin_types.get(flat_symbol, "cross"),
                        },
                    )
                )
        return rows

    async def open_orders(self, account_id, symbol=None):
        out = []
        for k, v in self.positions.items():
            if v.get("stop") is not None:
                out.append(FakeOrder("STOP_MARKET", v["stop"], k, qty=abs(v["qty"])))
            if v.get("tp") is not None:
                out.append(FakeOrder("TAKE_PROFIT_MARKET", v["tp"], k, qty=abs(v["qty"])))
            if v.get("stop_limit") is not None:
                out.append(FakeOrder("STOP", v["stop_limit"], k, qty=abs(v["qty"])))
            if v.get("tp_limit") is not None:
                out.append(FakeOrder("TAKE_PROFIT", v["tp_limit"], k, qty=abs(v["qty"])))
        return out

    async def place_order(self, account_id, req):
        self.placed.append(req.to_params())
        return FakePlaced(req)

    async def cancel_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        return FakePlaced(None)

    async def set_leverage(self, account_id, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))

    async def set_margin_type(self, account_id, symbol, margin_type):
        del account_id
        self.margin_type_calls.append((symbol, margin_type))
        self.flat_margin_types[symbol] = margin_type
        for key, value in self.positions.items():
            if key.symbol == symbol:
                value["margin_type"] = margin_type

    async def query_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        del account_id, symbol, order_id
        if orig_client_order_id in self.query_results:
            return self.query_results[orig_client_order_id]
        for params in self.placed:
            if params.get("newClientOrderId") == orig_client_order_id:
                placed = FakePlaced(None)
                placed.client_order_id = orig_client_order_id
                return placed
        return None

    async def query_order_history(self, account_id, symbol, *, orig_client_order_id):
        return await self.query_order(account_id, symbol, orig_client_order_id=orig_client_order_id)


class FakeBalance:
    def __init__(self, available):
        self.asset = "USDT"
        self.available_balance = available


class FakePosition:
    def __init__(self, key, v):
        self.symbol = key.symbol
        self.position_amt = v["qty"]
        self.entry_price = v.get("entry", D(65000))
        self.mark_price = D(65000)
        self.leverage = str(v.get("leverage", 20))
        self.liquidation_price = D(30000)
        self.position_side = key.pos_side
        self.un_realized_profit = D(0)
        self.margin_type = v.get("margin_type", "cross")
        self.isolated_margin = v.get(
            "isolated_margin",
            abs(self.position_amt) * self.entry_price / D(max(int(self.leverage), 1))
            if str(self.margin_type).upper() == "ISOLATED"
            else D(0),
        )


class FakeOrder:
    def model_dump(self):
        return {"coid": self.client_order_id}

    def __init__(self, type_, stop_price, key, coid="ct-stop", status="NEW", ut=1, qty=D("0.05")):
        self.type = type_
        self.stop_price = stop_price
        self.symbol = key.symbol
        self.position_side = key.pos_side
        self.client_order_id = coid
        self.status = status
        self.close_position = type_ in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
        self.reduce_only = not self.close_position
        self.side = Side.SELL
        self.working_type = "MARK_PRICE"
        self.order_id = 5
        self.update_time = ut
        self.orig_qty = D(0) if self.close_position else qty


class FakePlaced:
    def __init__(self, req):
        self.client_order_id = req.new_client_order_id if req else "ct-x"
        self.status = "NEW"


FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    tick_size=D("0.1"),
    step_size=D("0.001"),
    min_qty=D("0.001"),
    max_qty=D(1000),
    market_max_qty=D(120),
    min_notional=D(5),
)


def filters_provider():
    return {"BTCUSDT": FILTERS}


def master_state(qty=D("1"), stop=D(64500), leverage=20, entry=D(65000)) -> MasterState:
    key = PositionKey("BTCUSDT")
    pos = PositionState(
        key=key,
        qty=qty,
        entry_price=entry,
        leverage=leverage,
        mark_price=D(65000),
        liquidation_price=D(30000),
        margin_type="CROSSED",
    )
    if stop is not None:
        pos.stop = ProtectiveOrder(
            client_order_id="m-stop",
            order_id=1,
            type="STOP_MARKET",
            side=Side.SELL,
            stop_price=stop,
            reduce_only=True,
            close_position=False,
            working_type="MARK_PRICE",
        )
    positions = {key: pos} if (qty != 0 or stop is not None) else {}
    return MasterState(available_usdt=D(100000), positions=positions)


def delta_of(state) -> MasterDelta:
    return MasterDelta(changed_symbols=frozenset({k.symbol for k in state.positions}), state=state)


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "coord.sqlite3")
    await db.connect()
    await db.migrate()
    from copytradeaster.storage.repositories import AccountRepo

    followers = []
    for i in range(1, 5):
        account_id = f"copyc{i}"
        await AccountRepo(db).upsert(account_id, "follower", account_id, f"0x{account_id}")
        fr = FollowerRuntime(account_id=account_id, client=FakeClient(), identity=None)
        await SettingsRepo(db).upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
            account_id,
        )
        followers.append(fr)
    commands = CommandRepo(db)
    trades = CopyTradeRepo(db)
    links = OrderLinkRepo(db)
    settings = SettingsRepo(db)
    registry = FollowerRegistry(followers)
    routing_app = App.__new__(App)
    routing_app.follower_registry = registry
    routing_app.master_client = FakeClient()
    executor = Executor(
        routing_app._client_for,
        commands,
        links,
        {},
        settings=settings,
    )
    notifications: list[str] = []

    async def notify(text):
        notifications.append(text)

    coordinator = Coordinator(
        followers=registry,
        settings=settings,
        executor=executor,
        trades=trades,
        links=links,
        filters_provider=filters_provider,
        app_flags=settings,
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        fee_buffer_pct=D("0.001"),
        working_type="MARK_PRICE",
        on_notify=notify,
    )
    yield coordinator, followers, db, notifications
    await db.close()


async def test_fanout_all_followers_receive_plan(env):
    coordinator, followers, db, notifications = env
    await coordinator.on_master_delta(delta_of(master_state()))
    placed_counts = [len(fr.client.placed) for fr in followers]
    assert all(c >= 2 for c in placed_counts), placed_counts  # open + stop each
    # sizing: budget 1000*0.01=10; distance 500*1.001 -> qty 0.019
    open_params = [p for p in followers[0].client.placed if p["type"] == "MARKET"][0]
    assert open_params["quantity"] == "0.019"


async def test_runtime_entry_admission_blocks_open_but_not_user_pause_state(env):
    coordinator, followers, db, notifications = env
    del db, notifications
    coordinator._entry_admission = lambda: False

    await coordinator.on_master_delta(delta_of(master_state()))

    assert all(fr.client.placed == [] for fr in followers)

    # The process-local startup/reconnect gate is not a durable user pause:
    # risk-reducing convergence must remain available while entries are shut.
    key = PositionKey("BTCUSDT")
    followers[0].client.positions[key] = {"qty": D("0.05"), "stop": D("64500")}
    await coordinator.on_master_delta(delta_of(MasterState(available_usdt=D(1), positions={})))
    assert any(order["type"] == "MARKET" for order in followers[0].client.placed)


async def test_production_recompute_uses_exchange_symbol_maximum(env):
    coordinator, followers, _, _ = env
    client = followers[0].client
    calls: list[tuple[str, str]] = []

    async def max_leverage(account_id: str, symbol: str) -> int:
        calls.append((account_id, symbol))
        return 200

    client.max_leverage = max_leverage  # type: ignore[attr-defined,method-assign]
    await coordinator.on_master_delta(delta_of(master_state(leverage=20)))

    assert calls == [("copyc1", "BTCUSDT")]
    # PM-B7 entry gate: the 200x exchange maximum would breach the stop
    # margin (65000/500+975 -> cap 44), so the entry lands capped, not at max.
    assert client.leverage_calls == [("BTCUSDT", 44)]


async def test_targets_public_seam_uses_client_maximum_when_supplied(env):
    coordinator, followers, db, _ = env
    client = followers[0].client
    calls: list[str] = []

    async def max_leverage(account_id: str, symbol: str) -> int:
        del account_id
        calls.append(symbol)
        return 200

    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    client.max_leverage = max_leverage  # type: ignore[attr-defined,method-assign]
    targets, _ = await coordinator.targets_for(
        "copyc1",
        master_state(leverage=20),
        AccountState(
            account_id="copyc1",
            available_usdt=D("1000"),
            positions={},
            open_orders=[],
        ),
        policy,
        client=client,
    )
    assert calls == ["BTCUSDT"]
    assert targets and targets[0].leverage == 44


@pytest.mark.parametrize(
    ("master_qty", "stop", "expected_qty"),
    [(-D("1"), D("64500"), D("0")), (D("1"), D("60000"), D("0.001"))],
)
async def test_leverage_failure_does_not_block_existing_close_or_reduction(
    env, master_qty, stop, expected_qty
):
    coordinator, followers, db, _ = env
    client = followers[0].client
    key = PositionKey("BTCUSDT")
    calls: list[str] = []

    async def unavailable(account_id: str, symbol: str) -> int:
        del account_id
        calls.append(symbol)
        raise AssertionError("leverage lookup must not run for close/reduction")

    client.max_leverage = unavailable  # type: ignore[attr-defined,method-assign]
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    actual = AccountState(
        account_id="copyc1",
        available_usdt=D("1000"),
        positions={
            key: PositionState(
                key=key,
                qty=D("0.1"),
                entry_price=D("65000"),
                leverage=7,
                mark_price=D("65000"),
                liquidation_price=D("30000"),
            )
        },
        open_orders=[],
    )

    targets, _ = await coordinator.targets_for(
        "copyc1",
        master_state(qty=master_qty, stop=stop),
        actual,
        policy,
        client=client,
    )

    assert calls == []
    assert targets and targets[0].qty == expected_qty
    assert targets[0].leverage == 7


async def test_fanout_freshness_is_captured_once_for_slow_followers(env):
    coordinator, followers, db, notifications = env
    del db
    coordinator._stale_master_ms = 1
    for follower in followers:
        original_balance = follower.client.balance

        async def slow_balance(account_id, original_balance=original_balance):
            await asyncio.sleep(0.01)
            return await original_balance(account_id)

        follower.client.balance = slow_balance  # type: ignore[method-assign]
    state = master_state()
    state.event_time = int(time.time() * 1000)
    await coordinator.on_master_delta(delta_of(state))
    assert not any("STALE_MASTER" in text for text in notifications)
    assert all(len(follower.client.placed) >= 2 for follower in followers)


async def test_hot_add_during_fanout_is_visible_only_to_next_generation(env):
    coordinator, followers, db, _ = env
    entered = asyncio.Event()
    release = asyncio.Event()
    first_balance = followers[0].client.balance

    async def blocking_balance(account_id):
        entered.set()
        await release.wait()
        return await first_balance(account_id)

    followers[0].client.balance = blocking_balance  # type: ignore[method-assign]
    new_runtime = FollowerRuntime("copyc5", FakeClient(), None)
    from copytradeaster.storage.repositories import AccountRepo

    await AccountRepo(db).upsert("copyc5", "follower", "copyc5", "0xcopyc5")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy("0.01", 20, "0.01", allow_new_entries=False),
        "copyc5",
    )

    current_generation = asyncio.create_task(coordinator.on_master_delta(delta_of(master_state())))
    await entered.wait()
    await coordinator.registry.publish_new(new_runtime)
    release.set()
    await current_generation

    assert new_runtime.client.placed == []
    await coordinator.on_master_delta(delta_of(master_state()))
    # The new runtime participates without entering because onboarding keeps
    # it paused.  Enabling it proves the following generation routes correctly.
    await SettingsRepo(db).patch_policy("copyc5", allow_new_entries=True)
    await coordinator.on_master_delta(delta_of(master_state()))
    assert len(new_runtime.client.placed) >= 2


async def test_replacement_during_generation_keeps_old_client_for_old_plan(env):
    coordinator, followers, db, _ = env
    del db
    old_runtime = coordinator.registry.get("copyc1")
    assert old_runtime is not None
    old_client = old_runtime.client
    new_client = FakeClient()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_place = old_client.place_order

    async def blocking_place(account_id, request):
        entered.set()
        await release.wait()
        return await original_place(account_id, request)

    old_client.place_order = blocking_place  # type: ignore[method-assign]
    coordinator._executor._on_confirmed = None
    generation = asyncio.create_task(coordinator.on_master_delta(delta_of(master_state())))
    await entered.wait()
    replacement = FollowerRuntime("copyc1", new_client, None)
    await coordinator.registry.publish_replacement(replacement, expected=old_runtime)
    release.set()
    await generation

    assert old_client.placed
    assert new_client.placed == []


async def test_pause_barrier_waits_for_active_generation_and_closes_admission(env):
    coordinator, followers, db, _ = env
    del db
    first = followers[0].client
    entered = asyncio.Event()
    release = asyncio.Event()
    original_balance = first.balance

    async def blocking_balance(account_id):
        entered.set()
        await release.wait()
        return await original_balance(account_id)

    first.balance = blocking_balance  # type: ignore[method-assign]
    coordinator._executor._on_confirmed = None
    generation = asyncio.create_task(coordinator.on_master_delta(delta_of(master_state())))
    await entered.wait()
    pause = asyncio.create_task(coordinator.pause_barrier(timeout_s=1))
    await asyncio.sleep(0)
    assert not pause.done()
    release.set()
    await generation
    await pause

    before = [len(follower.client.placed) for follower in followers]
    await coordinator.on_master_delta(delta_of(master_state()))
    assert [len(follower.client.placed) for follower in followers] == before
    await coordinator.resume_after_pause()


async def test_postfill_drain_rejects_a_late_confirmation(env):
    coordinator, followers, db, _ = env
    del db, followers
    coordinator._last_master_state = master_state()
    action = ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT"},
        client_order_id="late",
        replaces_client_order_id=None,
        priority=1,
    )

    async def late_confirmation():
        await asyncio.sleep(0)
        await coordinator.on_command_confirmed("copyc1", action)

    late = asyncio.create_task(late_confirmation())
    await coordinator.drain_postfill_tasks(timeout_s=0.1)
    await late
    assert coordinator._postfill_tasks == set()


async def test_postfill_skips_stale_cached_master_state(env):
    """L1 live incident: a cached delta predating a REST-side fill must not
    drive closes. Stale cache -> no task; fresh cache -> task spawned."""
    import time as _time

    coordinator, followers, db, _ = env
    del db, followers
    action = ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT"},
        client_order_id="fresh-open",
        replaces_client_order_id=None,
        priority=1,
    )
    coordinator._last_master_state = master_state()
    coordinator._last_master_state_at = _time.monotonic() - 60.0
    await coordinator.on_command_confirmed("copyc1", action)
    assert coordinator._postfill_tasks == set()
    coordinator._last_master_state_at = _time.monotonic()
    coordinator._last_postfill_recompute.clear()
    await coordinator.on_command_confirmed("copyc1", action)
    assert len(coordinator._postfill_tasks) == 1
    await coordinator.drain_postfill_tasks(timeout_s=10)
    assert coordinator._postfill_tasks == set()


def lifecycle_app(coordinator, db):
    app = App.__new__(App)
    app.follower_registry = coordinator.registry
    app.executor = coordinator._executor
    app.coordinator = coordinator
    app.commands = CommandRepo(db)
    return app


async def test_safe_remove_rejects_nonflat_follower(env):
    coordinator, followers, db, _ = env
    app = lifecycle_app(coordinator, db)
    followers[0].client.positions[PositionKey("BTCUSDT")] = {"qty": D("0.1")}

    with pytest.raises(RuntimeError, match="not flat"):
        await app.unregister_follower("copyc1")
    assert coordinator.registry.get("copyc1") is not None


async def test_safe_remove_rejects_open_orders(env):
    coordinator, followers, db, _ = env
    app = lifecycle_app(coordinator, db)
    followers[0].client.positions[PositionKey("BTCUSDT")] = {
        "qty": D(0),
        "stop": D(64000),
    }

    with pytest.raises(RuntimeError, match="open orders"):
        await app.unregister_follower("copyc1")
    assert coordinator.registry.get("copyc1") is not None


async def test_safe_remove_rejects_inflight_command(env):
    coordinator, _, db, _ = env
    app = lifecycle_app(coordinator, db)
    commands = CommandRepo(db)
    await commands.insert(
        "inflight-remove",
        "copyc1",
        None,
        "MARKET_OPEN",
        "{}",
        1,
        "ct-inflight-remove",
    )
    await commands.set_status("inflight-remove", "SENDING")

    with pytest.raises(RuntimeError, match="in-flight"):
        await app.unregister_follower("copyc1")
    assert coordinator.registry.get("copyc1") is not None


async def test_one_follower_failure_isolated(env):
    coordinator, followers, db, notifications = env

    def boom(account_id):
        raise RuntimeError("boom")

    followers[2].client.balance = boom  # type: ignore[method-assign]
    await coordinator.on_master_delta(delta_of(master_state()))
    ok_placements = [len(fr.client.placed) for i, fr in enumerate(followers) if i != 2]
    assert all(c >= 2 for c in ok_placements)


async def test_three_consecutive_errors_set_degraded(env):
    coordinator, followers, db, notifications = env

    def boom(account_id):
        raise RuntimeError("boom")

    followers[3].client.balance = boom  # type: ignore[method-assign]
    d = delta_of(master_state())
    for _ in range(3):
        await coordinator.on_master_delta(d)
    flag = await SettingsRepo(db).get_flag("degraded")
    assert flag is not None and "copyc4" in flag


async def test_recompute_error_notifications_are_thresholded_and_reopen_after_recovery(env):
    coordinator, followers, _db, notifications = env

    def boom(account_id):
        raise RuntimeError("boom")

    followers[3].client.balance = boom  # type: ignore[method-assign]
    state = delta_of(master_state())
    for _ in range(5):
        await coordinator.on_master_delta(state)

    assert len([n for n in notifications if "Сверка follower copyc4" in n]) == 2

    healthy_client = FakeClient()
    followers[3].client.balance = healthy_client.balance  # type: ignore[method-assign]
    await coordinator.on_master_delta(state)
    followers[3].client.balance = boom  # type: ignore[method-assign]
    await coordinator.on_master_delta(state)

    assert len([n for n in notifications if "Сверка follower copyc4" in n]) == 3


async def test_pause_blocks_open_but_not_close(env):
    coordinator, followers, db, notifications = env
    settings_repo = SettingsRepo(db)
    policy = RiskPolicy(
        risk_percent=D("0.01"),
        max_leverage=20,
        max_entry_deviation_pct=D("0.01"),
        allow_new_entries=False,
    )
    for fr in followers:
        await settings_repo.upsert_policy(policy, fr.account_id)
    # no positions anywhere -> flat target; nothing should be placed at all
    await coordinator.on_master_delta(delta_of(master_state()))
    assert all(len(fr.client.placed) == 0 for fr in followers)
    assert notifications == []

    # follower has an open position, master closed -> close must go through pause
    followers[0].client.positions[PositionKey("BTCUSDT")] = {
        "qty": D("0.05"),
        "stop": D(64500),
    }
    flat_master = MasterState(available_usdt=D(1), positions={})
    await coordinator.on_master_delta(delta_of(flat_master))
    kinds = [p["type"] for p in followers[0].client.placed]
    assert "MARKET" in kinds


async def test_disabled_follower_untouched(env):
    coordinator, followers, db, _ = env
    policy = RiskPolicy(
        risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01"), enabled=False
    )
    await SettingsRepo(db).upsert_policy(policy, "copyc1")
    await coordinator.on_master_delta(delta_of(master_state()))
    assert len(followers[0].client.placed) == 0
    assert len(followers[1].client.placed) >= 2


async def test_whitelist_symbols_only(env):
    coordinator, followers, db, _ = env
    state = MasterState(
        available_usdt=D(1),
        positions={
            PositionKey("SOLUSDT"): PositionState(
                key=PositionKey("SOLUSDT"),
                qty=D(10),
                entry_price=D(150),
                leverage=20,
                mark_price=D(150),
                liquidation_price=D(100),
            ),
        },
    )
    await coordinator.on_master_delta(delta_of(state))
    assert all(len(fr.client.placed) == 0 for fr in followers)


async def test_no_stop_skip_notification(env):
    coordinator, followers, db, notifications = env
    await coordinator.on_master_delta(delta_of(master_state(stop=None)))
    assert any("защитного стопа" in n for n in notifications)
    assert all(len(fr.client.placed) == 0 for fr in followers)


async def test_structured_skip_event_has_explicit_account_and_safe_text(env):
    coordinator, followers, _db, _legacy_notifications = env
    events: list[NotificationEvent] = []

    class Collector:
        async def emit(self, event: NotificationEvent) -> None:
            events.append(event)

    coordinator._on_notify = None
    coordinator._notifier = Collector()
    state = master_state(stop=None)
    state.event_time = 101
    await coordinator.on_master_delta(delta_of(state))
    first_count = len(events)
    state.event_time = 202
    await coordinator.on_master_delta(delta_of(state))

    assert events
    assert len(events) == first_count
    assert all(event.account_id in {f.account_id for f in followers} for event in events)
    assert all(event.kind == "copy.skip" for event in events)
    assert all(event.severity == NotificationSeverity.INFO for event in events)
    assert all("защитного стопа" in event.text for event in events)
    assert all("[" not in event.text and "]" not in event.text for event in events)


async def test_entry_skipped_hook_receives_stable_attempt_and_survives_failure(env):
    coordinator, followers, _db, _legacy_notifications = env
    calls: list[tuple] = []

    async def hook(account_id, symbol, reason, attempt_id, pos_side):
        calls.append((account_id, symbol, reason, attempt_id, pos_side))

    coordinator.set_entry_skipped_hook(hook)
    state = master_state(stop=None)
    state.event_time = 101
    await coordinator.on_master_delta(delta_of(state))
    assert calls, "actionable skips must reach the durable feed hook"
    first = list(calls)
    assert all(call[1] == "BTCUSDT" and call[4] == "BOTH" for call in calls)
    assert {call[0] for call in calls} <= {f.account_id for f in followers}
    # Same master episode re-observed: attempt identity is stable.
    state.event_time = 202
    await coordinator.on_master_delta(delta_of(state))
    assert [call[3] for call in calls] == [call[3] for call in first] * 2

    async def failing(*args):
        raise RuntimeError("feed down")

    coordinator.set_entry_skipped_hook(failing)
    state.event_time = 303
    await coordinator.on_master_delta(delta_of(state))
    assert all(len(fr.client.placed) == 0 for fr in followers)


async def test_actionable_skip_notifies_again_for_a_new_master_episode(env):
    coordinator, followers, db, _legacy_notifications = env
    events: list[NotificationEvent] = []

    class Collector:
        async def emit(self, event: NotificationEvent) -> None:
            events.append(event)

    coordinator._on_notify = None
    coordinator._notifier = Collector()
    trades = CopyTradeRepo(db)
    state = master_state(stop=None)
    first_trade = await trades.open_trade("BTCUSDT", "BOTH", None)
    await coordinator.on_master_delta(delta_of(state))
    await coordinator.on_master_delta(delta_of(state))
    assert len(events) == len(followers)

    await trades.close_trade(first_trade)
    second_trade = await trades.open_trade("BTCUSDT", "BOTH", None)
    await coordinator.on_master_delta(delta_of(state))

    assert second_trade != first_trade
    assert len(events) == 2 * len(followers)
    assert {event.event_key for event in events[: len(followers)]}.isdisjoint(
        {event.event_key for event in events[len(followers) :]}
    )


async def test_structured_notifier_failure_does_not_block_fanout(env):
    coordinator, followers, _db, _legacy_notifications = env

    class BrokenNotifier:
        async def emit(self, event):
            del event
            raise RuntimeError("notification enqueue unavailable")

    coordinator._on_notify = None
    coordinator._notifier = BrokenNotifier()
    await coordinator.on_master_delta(delta_of(master_state()))

    assert all(len(follower.client.placed) >= 2 for follower in followers)


async def test_self_stop_blocks_reentry_until_new_episode(env):
    coordinator, followers, db, notifications = env
    fr = followers[1]  # copyc2 will have its stop filled
    # master episode active
    trade_id = await CopyTradeRepo(db).open_trade("BTCUSDT", "BOTH", None)
    # the episode's stop link for copyc2 exists and query shows FILLED
    await OrderLinkRepo(db).add(trade_id, "copyc2", "PLACE_STOP", "ct-ep-stop")
    fr.client.query_results["ct-ep-stop"] = FakeOrder(
        "STOP_MARKET", D(64500), PositionKey("BTCUSDT"), coid="ct-ep-stop", status="FILLED"
    )
    await coordinator.on_master_delta(delta_of(master_state()))
    # copyc2 did NOT enter; others did
    assert len(fr.client.placed) == 0
    assert len(followers[0].client.placed) >= 2

    # new master episode (old closed) lifts the ban
    trades_repo = CopyTradeRepo(db)
    active = await trades_repo.active_trade("BTCUSDT", "BOTH")
    await trades_repo.close_trade(int(active["trade_id"]))
    await coordinator.on_master_delta(delta_of(master_state()))
    assert len(fr.client.placed) >= 2


async def test_stale_master_blocks_new_entry(env):
    """Live testnet incident: follower already flat (own stop triggered), master
    WS event still in flight -> branch a must NOT open against stale state."""
    import time as _time

    coordinator, followers, db, notifications = env
    state = master_state()
    state.event_time = int(_time.time() * 1000) - 60_000  # far beyond 5s window
    await coordinator.on_master_delta(delta_of(state))
    assert all(len(fr.client.placed) == 0 for fr in followers)
    assert any("сигнал мастера устарел" in n for n in notifications)


async def test_stale_master_blocks_growth(env):
    """Branch b (master increased) must not grow a follower position when the
    master view is stale - observed as 0.321 BTC churn after master close."""
    import time as _time

    coordinator, followers, db, _ = env
    fr = followers[0]
    key = PositionKey("BTCUSDT")
    fr.client.positions[key] = {"qty": D("0.05"), "stop": D(64500)}
    # prime the tracker with the current magnitude
    await coordinator.on_master_delta(delta_of(master_state(qty=D("0.05"))))
    fr.client.placed.clear()
    # stale delta claiming growth
    grown = master_state(qty=D("0.06"))
    grown.event_time = int(_time.time() * 1000) - 60_000
    await coordinator.on_master_delta(delta_of(grown))
    assert all(p["type"] != "MARKET" for p in fr.client.placed)


async def test_fresh_master_still_enters(env):
    """Freshness guard must not over-block: fresh state enters normally."""
    import time as _time

    coordinator, followers, db, _ = env
    state = master_state()
    state.event_time = int(_time.time() * 1000)
    await coordinator.on_master_delta(delta_of(state))
    assert any(len(fr.client.placed) >= 2 for fr in followers)


async def test_master_tp_copied_to_follower(env):
    """Live incident: coordinator never propagated the master's TP level into
    targets (tp_price hardcoded None) so followers never got take profits."""
    import time as _time

    coordinator, followers, db, _ = env
    fr = followers[0]
    key = PositionKey("BTCUSDT")
    fr.client.positions[key] = {"qty": D("0.05"), "stop": D(64500)}
    state = master_state(qty=D("0.05"), stop=D(64500))
    state.positions[key].take_profit = ProtectiveOrder(
        client_order_id="m-tp",
        order_id=2,
        type="TAKE_PROFIT_MARKET",
        side=Side.SELL,
        stop_price=D("65500"),
        reduce_only=True,
        close_position=False,
        working_type="MARK_PRICE",
    )
    state.event_time = int(_time.time() * 1000)
    await coordinator.on_master_delta(delta_of(state))
    assert any(p["type"] == "TAKE_PROFIT_MARKET" for p in fr.client.placed)


async def test_own_tp_fill_blocks_reentry(env):
    """Live incident: follower TP filled, master TRADE event still in flight -
    branch a must not re-open against the ended episode (copyc2 0.041 churn)."""
    coordinator, followers, db, notifications = env
    fr = followers[1]
    trade_id = await CopyTradeRepo(db).open_trade("BTCUSDT", "BOTH", None)
    await OrderLinkRepo(db).add(trade_id, "copyc2", "PLACE_TP", "ct-ep-tp")
    fr.client.query_results["ct-ep-tp"] = FakeOrder(
        "TAKE_PROFIT_MARKET", D(65500), PositionKey("BTCUSDT"), coid="ct-ep-tp", status="FILLED"
    )
    await coordinator.on_master_delta(delta_of(master_state()))
    assert len(fr.client.placed) == 0
    assert not any("OWN_PROTECTIVE_FILLED" in n for n in notifications)


async def test_limit_flavor_stop_mirrored(env):
    """Master uses STOP (stop-limit): followers must mirror the exact flavor."""
    import time as _time

    coordinator, followers, db, _ = env
    fr = followers[0]
    key = PositionKey("BTCUSDT")
    fr.client.positions[key] = {"qty": D("0.05"), "stop": D(64500)}
    state = master_state(qty=D("0.05"), stop=D(64600))
    pos = state.positions[key]
    state.positions[key] = PositionState(
        key=pos.key,
        qty=pos.qty,
        entry_price=pos.entry_price,
        leverage=pos.leverage,
        mark_price=pos.mark_price,
        liquidation_price=pos.liquidation_price,
        stop=pos.stop,
        take_profit=None,
    )
    frozen_stop = pos.stop
    object.__setattr__(frozen_stop, "type", "STOP")
    state.positions[key] = PositionState(
        key=pos.key,
        qty=pos.qty,
        entry_price=pos.entry_price,
        leverage=pos.leverage,
        mark_price=pos.mark_price,
        liquidation_price=pos.liquidation_price,
        stop=frozen_stop,
        take_profit=None,
    )
    state.event_time = int(_time.time() * 1000)
    await coordinator.on_master_delta(delta_of(state))
    stops = [p for p in fr.client.placed if p["type"] in ("STOP_MARKET", "STOP")]
    assert stops and stops[-1]["type"] == "STOP"


class _StubBasis:
    """Minimal sync_basis seam returning one persisted master magnitude."""

    def __init__(self, prev):
        self._prev = prev

    async def get(self, account_id, symbol, pos_side):
        if self._prev is None:
            return None
        return {"master_ref_abs": str(self._prev)}


def _coordinator_with_manager(fr, db, manager, notifications):
    executor = Executor(
        lambda aid: fr.client,
        CommandRepo(db),
        OrderLinkRepo(db),
        {},
        settings=SettingsRepo(db),
    )

    async def notify(text):
        notifications.append(text)

    return Coordinator(
        [fr],
        SettingsRepo(db),
        executor,
        CopyTradeRepo(db),
        OrderLinkRepo(db),
        filters_provider,
        SettingsRepo(db),
        ["BTCUSDT", "ETHUSDT"],
        D("0.001"),
        "MARK_PRICE",
        on_notify=notify,
        leverage=manager,
    )


async def test_snapshot_preserves_flat_symbol_leverage(tmp_path):
    """positionRisk reports the exchange-persisted leverage for flat symbols;
    dropping that row made 'no position' look like 'leverage unknown'."""
    db = Database(tmp_path / "flat.sqlite3")
    await db.connect()
    await db.migrate()
    fr = FollowerRuntime(account_id="copyc1", client=FakeClient(), identity=None)
    fr.client.positions = {PositionKey("BTCUSDT"): {"qty": D(0), "leverage": "100"}}
    try:
        coordinator = Coordinator(
            [fr],
            SettingsRepo(db),
            Executor(lambda aid: fr.client, CommandRepo(db), OrderLinkRepo(db), {}),
            CopyTradeRepo(db),
            OrderLinkRepo(db),
            filters_provider,
            SettingsRepo(db),
            ["BTCUSDT"],
            D("0.001"),
            "MARK_PRICE",
        )
        snap = await coordinator.snapshot_follower(fr)
        assert snap.symbol_leverages == {"BTCUSDT": 100}
        assert snap.positions == {}
    finally:
        await db.close()


async def test_snapshot_preserves_margin_mode_and_omits_conflicting_hedge_rows(tmp_path):
    """Mode is authoritative per symbol; disagreeing HEDGE rows fail closed."""
    db = Database(tmp_path / "margin-snapshot.sqlite3")
    await db.connect()
    await db.migrate()
    fake = FakeClient()
    fake.positions = {
        PositionKey("BTCUSDT", PosSide.LONG): {
            "qty": D(0),
            "leverage": 20,
            "margin_type": "cross",
        },
        PositionKey("BTCUSDT", PosSide.SHORT): {
            "qty": D(0),
            "leverage": 20,
            "margin_type": "isolated",
        },
    }
    fr = FollowerRuntime(account_id="copyc1", client=fake, identity=None)
    try:
        coordinator = Coordinator(
            [fr],
            SettingsRepo(db),
            Executor(lambda aid: fr.client, CommandRepo(db), OrderLinkRepo(db), {}),
            CopyTradeRepo(db),
            OrderLinkRepo(db),
            filters_provider,
            SettingsRepo(db),
            ["BTCUSDT"],
            D("0.001"),
            "MARK_PRICE",
        )
        snap = await coordinator.snapshot_follower(fr)
        assert snap.positions[PositionKey("BTCUSDT", PosSide.LONG)].margin_type == "CROSSED"
        assert snap.positions[PositionKey("BTCUSDT", PosSide.SHORT)].margin_type == "ISOLATED"
        assert snap.symbol_margin_types == {}
    finally:
        await db.close()


async def test_unknown_master_margin_mode_skips_new_entry(env):
    coordinator, followers, db, _ = env
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    master = master_state()
    master.positions[PositionKey("BTCUSDT")].margin_type = None
    actual = AccountState(
        account_id="copyc1",
        available_usdt=D("1000"),
        positions={},
        symbol_margin_types={"BTCUSDT": "CROSSED"},
    )
    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
    assert targets == []
    assert any(skip.reason is SkipReason.MARGIN_TYPE_UNKNOWN for skip in skips)
    assert followers[0].client.placed == []


async def test_conflicting_master_hedge_margin_modes_skip_new_entry(env):
    coordinator, followers, db, _ = env
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    master = master_state()
    long_key = PositionKey("BTCUSDT", PosSide.LONG)
    short_key = PositionKey("BTCUSDT", PosSide.SHORT)
    master.positions[long_key] = master.positions.pop(PositionKey("BTCUSDT"))
    master.positions[long_key].key = long_key
    master.positions[short_key] = PositionState(
        key=short_key,
        qty=D("1"),
        entry_price=D("65000"),
        leverage=20,
        mark_price=D("65000"),
        liquidation_price=D("30000"),
        margin_type="ISOLATED",
        stop=ProtectiveOrder(
            client_order_id="m-short-stop",
            order_id=2,
            type="STOP_MARKET",
            side=Side.BUY,
            stop_price=D("65500"),
            reduce_only=True,
            close_position=False,
            working_type="MARK_PRICE",
        ),
    )
    actual = AccountState(
        account_id="copyc1",
        available_usdt=D("1000"),
        positions={},
        symbol_margin_types={"BTCUSDT": "CROSSED"},
    )
    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
    assert targets == []
    assert len([skip for skip in skips if skip.reason is SkipReason.MARGIN_TYPE_UNKNOWN]) == 2
    assert followers[0].client.placed == []


async def test_isolated_entry_caps_leverage_for_double_fee_buffered_stop_risk(env):
    coordinator, _followers, db, _ = env
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    master = master_state(stop=D("63000"), leverage=20)
    master.positions[PositionKey("BTCUSDT")].margin_type = "ISOLATED"
    actual = AccountState(
        account_id="copyc1",
        available_usdt=D("1000"),
        positions={},
        symbol_margin_types={"BTCUSDT": "ISOLATED"},
    )
    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
    assert skips == []
    assert targets and targets[0].margin_type == "ISOLATED"
    # liquidation cap is 21x here; the isolated 2x-loss collateral cap binds at 16x.
    assert targets[0].leverage == 16


async def test_isolated_entry_rejects_when_one_x_cannot_cover_double_stop_loss(env):
    coordinator, _followers, db, _ = env
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    master = master_state(stop=D("30000"), leverage=20)
    master.positions[PositionKey("BTCUSDT")].margin_type = "ISOLATED"
    actual = AccountState(
        account_id="copyc1",
        available_usdt=D("1000"),
        positions={},
        symbol_margin_types={"BTCUSDT": "ISOLATED"},
    )
    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
    assert targets == []
    assert any(skip.reason is SkipReason.INSUFFICIENT_MARGIN for skip in skips)


async def test_open_growth_is_clamped_until_follower_mode_matches_master(env):
    coordinator, followers, db, _ = env
    coordinator._basis = SyncBasisRepo(db)
    await coordinator._basis.upsert_ref("copyc1", "BTCUSDT", "BOTH", D("1"))
    fr = followers[0]
    key = PositionKey("BTCUSDT")
    fr.client.positions[key] = {
        "qty": D("0.01"),
        "entry": D("65000"),
        "leverage": 20,
        "stop": D("64500"),
        "margin_type": "cross",
    }
    actual = await coordinator.snapshot_follower(fr)
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None
    master = master_state(qty=D("2"), stop=D("64500"), leverage=20)
    master.positions[key].margin_type = "ISOLATED"
    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
    assert targets and targets[0].qty == D("0.01")
    assert targets[0].margin_type == "ISOLATED"
    assert any(skip.reason is SkipReason.MARGIN_TYPE_MISMATCH for skip in skips)


async def test_mode_mismatch_with_other_hedge_leg_still_cancels_flat_orphan_protection(env):
    """Blocked mode changes must not leave a flat leg's bot-owned orders behind."""
    coordinator, _followers, db, _ = env
    key = PositionKey("BTCUSDT", PosSide.LONG)
    other_key = PositionKey("BTCUSDT", PosSide.SHORT)
    orphan = ProtectiveOrder(
        client_order_id="ct-orphan-stop",
        order_id=7,
        type="STOP_MARKET",
        side=Side.SELL,
        stop_price=D("64500"),
        reduce_only=True,
        close_position=True,
        working_type="MARK_PRICE",
    )
    actual = AccountState(
        account_id="copyc1",
        available_usdt=D("1000"),
        positions={
            key: PositionState(
                key=key,
                qty=D(0),
                entry_price=D(0),
                leverage=20,
                mark_price=D(65000),
                liquidation_price=D(0),
                stop=orphan,
            ),
            other_key: PositionState(
                key=other_key,
                qty=D("0.01"),
                entry_price=D("65000"),
                leverage=20,
                mark_price=D(65000),
                liquidation_price=D("30000"),
            ),
        },
        symbol_leverages={"BTCUSDT": 20},
        symbol_margin_types={"BTCUSDT": "CROSSED"},
    )
    master = master_state()
    master_pos = master.positions.pop(PositionKey("BTCUSDT"))
    master_pos.key = key
    master_pos.margin_type = "ISOLATED"
    master.positions[key] = master_pos
    policy = await SettingsRepo(db).get_policy("copyc1")
    assert policy is not None

    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)

    assert any(skip.reason is SkipReason.MARGIN_TYPE_MISMATCH for skip in skips)
    orphan_target = next(target for target in targets if target.key == key)
    assert orphan_target.qty == D(0)
    from copytradeaster.trading.planner import build_plan

    plan = build_plan(
        actual,
        TargetState(account_id="copyc1", targets=targets),
        filters_provider(),
        coordinator._coid_factory(),
        working_type="MARK_PRICE",
    )
    assert any(
        action.kind is ActionKind.CANCEL_ORDER
        and action.payload.get("origClientOrderId") == "ct-orphan-stop"
        for action in plan.actions
    )
    assert not any(action.kind is ActionKind.SET_MARGIN_TYPE for action in plan.actions)


@pytest.fixture
async def managed_env(tmp_path):
    from copytradeaster.storage.repositories import AccountRepo
    from copytradeaster.trading.leverage import LeverageManager
    from tests.unit.trading.test_leverage_manager import FakeLevClient

    db = Database(tmp_path / "managed.sqlite3")
    await db.connect()
    await db.migrate()
    fake = FakeClient()
    fake.flat_leverage = 0  # preserve the explicit RAM control-plane test value
    fr = FollowerRuntime(account_id="copyc1", client=fake, identity=None)
    await AccountRepo(db).upsert("copyc1", "follower", "copyc1", "0xu")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
        "copyc1",
    )
    lev_clients = {"main": FakeLevClient(), "copyc1": FakeLevClient()}
    manager = LeverageManager(
        client_for=lambda account_id: lev_clients[account_id],
        symbols=["BTCUSDT"],
        accounts=lambda: [],
        pacing_s=0.0,
    )
    notifications: list[str] = []
    coordinator = _coordinator_with_manager(fr, db, manager, notifications)
    yield coordinator, manager, lev_clients, fr, db, notifications
    await db.close()


async def test_entry_skipped_when_leverage_not_ready(managed_env):
    coordinator, manager, lev_clients, fr, db, notifications = managed_env
    await coordinator.on_master_delta(delta_of(master_state(leverage=20)))

    # No candidate yet -> fail closed with a typed skip, never an order.
    assert fr.client.placed == []
    assert any("не подготовлено" in text for text in notifications)
    # The miss enqueued a bounded background preparation instead.
    assert ("copyc1", "BTCUSDT") in manager._pending  # noqa: SLF001


async def test_ready_manager_sizes_entry_without_network_reads(managed_env):
    coordinator, manager, lev_clients, fr, db, notifications = managed_env
    await manager.refresh_catalog()
    manager.observe("copyc1", {"BTCUSDT": 200})
    assert manager.is_ready("copyc1", "BTCUSDT")

    await coordinator.on_master_delta(delta_of(master_state(leverage=20)))

    kinds = [p["type"] for p in fr.client.placed]
    assert "MARKET" in kinds
    assert fr.client.leverage_calls == [("BTCUSDT", 44)]
    # Hot path stayed on RAM reads: no follower bracket GET, no extra POSTs.
    assert lev_clients["copyc1"].bracket_calls == []
    assert lev_clients["copyc1"].set_calls == []


async def test_increase_clamped_while_not_ready_keeps_reductions(managed_env):
    coordinator, manager, lev_clients, fr, db, notifications = managed_env
    await manager.refresh_catalog()
    manager.observe("copyc1", {"BTCUSDT": 200})
    assert manager.is_ready("copyc1", "BTCUSDT")
    # A catalog downshift opens a new generation; the observed confirmation
    # belongs to the old one -> not ready for NEW exposure.
    lev_clients["main"] = lev_clients.pop("copyc1")
    lev_clients["main"].account_max = 100
    await manager.refresh_catalog()
    assert not manager.is_ready("copyc1", "BTCUSDT")

    fr.client.positions = {PositionKey("BTCUSDT"): {"qty": D("0.001"), "stop": D(64500)}}
    import time as _time

    coordinator._basis = _StubBasis(D("1"))  # type: ignore[attr-defined]

    master = master_state(qty=D("2"), stop=D(64500))
    master.event_time = int(_time.time() * 1000)
    policy = await SettingsRepo(db).get_policy("copyc1")
    actual = await coordinator.snapshot_follower(fr)

    manager.invalidate("copyc1", "BTCUSDT")
    targets, skips = await coordinator.targets_for("copyc1", master, actual, policy)
    # Growth blocked: target stays at the CURRENT size instead of growing to
    # the risk cap; the leg is NOT skipped and preparation got enqueued.
    assert skips == []
    assert targets and targets[0].qty == D("0.001")
    assert ("copyc1", "BTCUSDT") in manager._pending  # noqa: SLF001
