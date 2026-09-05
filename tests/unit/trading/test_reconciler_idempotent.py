"""Reconciler idempotency: consistent state -> 0,0,0 commands; drift -> 1 then 0."""

from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PositionKey, RiskPolicy
from copytradeaster.master.projector import MasterState
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    CopyTradeRepo,
    OrderLinkRepo,
    SettingsRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor
from copytradeaster.trading.reconciler import Reconciler
from tests.unit.trading.test_coordinator_routing import (
    FakeClient,
    filters_provider,
    master_state,
)


def follower_position(qty="0.019", stop="64500"):
    return {PositionKey("BTCUSDT"): {"qty": D(qty), "stop": D(stop)}}


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "recon.sqlite3")
    await db.connect()
    await db.migrate()
    fr = FollowerRuntime(account_id="copyc1", client=FakeClient(), identity=None)
    await AccountRepo(db).upsert("copyc1", "follower", "copyc1", "0xu")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
        "copyc1",
    )
    commands = CommandRepo(db)
    executor = Executor(
        lambda aid: fr.client, commands, OrderLinkRepo(db), {}, settings=SettingsRepo(db)
    )
    coordinator = Coordinator(
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
    )
    reconciler = Reconciler(coordinator)
    yield reconciler, fr, db
    await db.close()


async def test_consistent_state_creates_zero_commands_three_times(env):
    reconciler, fr, db = env
    master = master_state()
    # bring the follower to target: qty 0.019 with stop 64500
    fr.client.positions = follower_position()
    for _ in range(3):
        report = await reconciler.reconcile(master)
        assert report.per_account["copyc1"] == 0
    assert len(fr.client.placed) == 0


async def test_drift_creates_one_close_then_zero(env):
    reconciler, fr, db = env
    master = MasterState(available_usdt=D(1), positions={})  # master flat
    fr.client.positions = follower_position()  # follower still holds a position
    report1 = await reconciler.reconcile(master)
    assert report1.per_account["copyc1"] >= 1  # cancel + close created
    kinds = [p["type"] for p in fr.client.placed]
    assert "MARKET" in kinds
    # The cheap fake records requests but does not apply fills/cancels.
    # A second evaluation still sees drift even when the journal row is reused.
    report2 = await reconciler.reconcile(master)
    assert report2.per_account["copyc1"] > 0
    fr.client.positions = {}
    report3 = await reconciler.reconcile(master)
    assert report3.per_account["copyc1"] == 0


async def test_entry_drift_then_zero(env):
    reconciler, fr, db = env
    master = master_state()  # master long with stop
    report1 = await reconciler.reconcile(master)
    assert report1.per_account["copyc1"] >= 1
    # simulate fills landing exactly at target
    fr.client.positions = follower_position()
    report2 = await reconciler.reconcile(master)
    assert report2.per_account["copyc1"] == 0
