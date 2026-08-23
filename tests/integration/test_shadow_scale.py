"""Non-certifying scale harness for the real coordinator/journal stack.

Run explicitly with ``CT_LOAD=1``. The master events are deterministic here so
the test is a fast preflight for 30 shadow followers; signed exchange evidence
comes from ``scripts/hybrid_load_test.py``.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.aster.shadow import ShadowExchange
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.master.projector import MasterProjector
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    CopyTradeRepo,
    MasterEventRepo,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor
from tests.integration.fake_aster import btc_filters
from tests.integration.test_end_to_end_fake import order_trade, stop_new

pytestmark = pytest.mark.skipif(
    os.environ.get("CT_LOAD") != "1", reason="explicit load run requires CT_LOAD=1"
)


async def test_thirty_shadow_followers_one_hundred_round_trips(tmp_path: Path):
    db = Database(tmp_path / "shadow-scale.sqlite3")
    await db.connect()
    await db.migrate()
    exchange = ShadowExchange(hedge=False, fill_price=D("65000"))
    follower_ids = [f"shadow-{index:03d}" for index in range(1, 31)]
    try:
        await AccountRepo(db).upsert("main", "master", "main", "shadow://main")
        for account_id in follower_ids:
            exchange.add_account(account_id, D("1000"))
            await AccountRepo(db).upsert(
                account_id,
                "follower",
                account_id,
                f"shadow://{account_id}",
                execution_mode="SHADOW",
            )
            await SettingsRepo(db).upsert_policy(RiskPolicy(D("0.01"), 20, D("0.01")), account_id)
        followers = [
            FollowerRuntime(account_id, exchange, None, execution_mode="SHADOW")
            for account_id in follower_ids
        ]

        async def on_confirmed(account_id, action):
            await coordinator.on_command_confirmed(account_id, action)

        executor = Executor(
            lambda _account_id: exchange,
            CommandRepo(db),
            OrderLinkRepo(db),
            {},
            settings=SettingsRepo(db),
            on_confirmed=on_confirmed,
        )
        coordinator = Coordinator(
            followers=followers,
            settings=SettingsRepo(db),
            executor=executor,
            trades=CopyTradeRepo(db),
            links=OrderLinkRepo(db),
            filters_provider=lambda: {"BTCUSDT": btc_filters()},
            app_flags=SettingsRepo(db),
            symbol_whitelist=["BTCUSDT"],
            fee_buffer_pct=D("0.001"),
            working_type="MARK_PRICE",
            basis=SyncBasisRepo(db),
        )
        projector = MasterProjector(
            MasterEventRepo(db),
            CopyTradeRepo(db),
            OrderLinkRepo(db),
            SettingsRepo(db),
            coordinator.on_master_delta,
        )
        started = time.monotonic()
        for cycle in range(1, 101):
            base_event = cycle * 10_000
            await projector.apply_raw(
                order_trade(
                    E=base_event,
                    c=f"master-open-{cycle}",
                    x="TRADE",
                    X="FILLED",
                    l="1",
                    z="1",
                    ap="65000",
                    L="65000",
                    q="1",
                )
            )
            await projector.apply_raw(
                stop_new(
                    E=base_event + 1,
                    c=f"master-stop-{cycle}",
                    sp="64500",
                    i=cycle * 2,
                )
            )
            for account_id in follower_ids:
                audit = exchange.audit(account_id)
                assert len(audit["positions"]) == 1, account_id
                assert len(audit["open_orders"]) == 1, account_id
            await projector.apply_raw(
                order_trade(
                    E=base_event + 2,
                    c=f"master-stop-{cycle}",
                    o="STOP_MARKET",
                    ot="STOP_MARKET",
                    x="CANCELED",
                    X="CANCELED",
                    sp="64500",
                    i=cycle * 2,
                )
            )
            await projector.apply_raw(
                order_trade(
                    E=base_event + 3,
                    c=f"master-close-{cycle}",
                    S="SELL",
                    x="TRADE",
                    X="FILLED",
                    l="1",
                    z="1",
                    ap="65000",
                    L="65000",
                    q="1",
                    rp="0",
                )
            )
            for account_id in follower_ids:
                assert exchange.audit(account_id) == {"positions": [], "open_orders": []}
        elapsed = time.monotonic() - started
        rows = await db.fetchall(
            "SELECT status, COUNT(*) AS n FROM execution_commands GROUP BY status"
        )
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        assert counts.get("FAILED", 0) == 0
        assert counts.get("UNKNOWN", 0) == 0
        assert exchange.signed_requests == 0
        assert elapsed < 60, f"shadow load took {elapsed:.2f}s"
    finally:
        await db.close()
