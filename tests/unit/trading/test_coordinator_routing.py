"""Coordinator routing tests: fan-out, error isolation, pause, branches a-e,
self-stop re-entry guard - all against fake clients, no HTTP."""

from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import (
    PositionKey,
    PositionState,
    ProtectiveOrder,
    RiskPolicy,
    Side,
    SymbolFilters,
)
from copytradeaster.master.projector import MasterDelta, MasterState
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CommandRepo,
    CopyTradeRepo,
    OrderLinkRepo,
    SettingsRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor


class FakeClient:
    def __init__(self, available="1000"):
        self.available = D(available)
        self.positions: dict[PositionKey, dict] = {}
        self.placed: list[dict] = []
        self.query_results: dict[str, object] = {}

    async def balance(self, account_id):
        return [FakeBalance(self.available)]

    async def position_risk(self, account_id):
        return [FakePosition(k, v) for k, v in self.positions.items()]

    async def open_orders(self, account_id, symbol=None):
        out = []
        for k, v in self.positions.items():
            if v.get("stop") is not None:
                out.append(FakeOrder("STOP_MARKET", v["stop"], k))
            if v.get("tp") is not None:
                out.append(FakeOrder("TAKE_PROFIT_MARKET", v["tp"], k))
        return out

    async def place_order(self, account_id, req):
        self.placed.append(req.to_params())
        return FakePlaced(req)

    async def cancel_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        return FakePlaced(None)

    async def query_order(self, account_id, symbol, *, orig_client_order_id=None, order_id=None):
        return self.query_results.get(orig_client_order_id)


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


class FakeOrder:
    def model_dump(self):
        return {"coid": self.client_order_id}

    def __init__(self, type_, stop_price, key, coid="ct-stop", status="NEW", ut=1):
        self.type = type_
        self.stop_price = stop_price
        self.symbol = key.symbol
        self.position_side = key.pos_side
        self.client_order_id = coid
        self.status = status
        self.reduce_only = True
        self.close_position = False
        self.side = Side.SELL
        self.working_type = "MARK_PRICE"
        self.order_id = 5
        self.update_time = ut


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
    executor = Executor(
        lambda aid: next(f.client for f in followers if f.account_id == aid),
        commands,
        links,
        {},
        settings=settings,
    )
    notifications: list[str] = []

    async def notify(text):
        notifications.append(text)

    coordinator = Coordinator(
        followers=followers,
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


async def test_pause_blocks_open_but_not_close(env):
    coordinator, followers, db, _ = env
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
    assert any("NO_STOP" in n for n in notifications)
    assert all(len(fr.client.placed) == 0 for fr in followers)


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
    assert any("STALE_MASTER" in n for n in notifications)


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
    assert any("OWN_PROTECTIVE_FILLED" in n for n in notifications)
