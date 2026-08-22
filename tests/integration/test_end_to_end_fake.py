"""End-to-end scenarios over FakeAster + real projector/coordinator/planner/executor."""

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

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
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor, resolve_boot_unknowns
from tests.integration.fake_aster import FakeAster, FakeOrder, btc_filters

D1 = D


def order_trade(**o):
    base = {
        "s": "BTCUSDT",
        "c": "coid-1",
        "S": "BUY",
        "o": "MARKET",
        "q": "1",
        "p": "0",
        "ap": "0",
        "sp": "0",
        "x": "NEW",
        "X": "NEW",
        "i": 100,
        "l": "0",
        "z": "0",
        "L": "0",
        "N": "USDT",
        "n": "0",
        "R": False,
        "wt": "MARK_PRICE",
        "ot": "MARKET",
        "ps": "BOTH",
        "rp": "0",
    }
    base.update(o)
    return {"e": "ORDER_TRADE_UPDATE", "E": base.get("E", 1000), "T": 999, "o": base}


def stop_new(**o):
    base = {
        "s": "BTCUSDT",
        "c": "m-stop",
        "S": "SELL",
        "o": "STOP_MARKET",
        "ot": "STOP_MARKET",
        "q": "1",
        "sp": "64500",
        "x": "NEW",
        "X": "NEW",
        "i": 101,
        "R": True,
        "wt": "MARK_PRICE",
        "ps": "BOTH",
        "ap": "0",
        "p": "0",
        "L": "0",
        "l": "0",
        "z": "0",
        "n": "0",
        "N": "USDT",
        "rp": "0",
    }
    base.update(o)
    return {"e": "ORDER_TRADE_UPDATE", "E": base.get("E", 1100), "T": 1099, "o": base}


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(tmp_path / "e2e.sqlite3")
    await db.connect()
    await db.migrate()
    fake = FakeAster(fill_price=D("65000"))
    fake.add_account("main", D("100000"))
    follower_ids = ["copyc1", "copyc2", "copyc3", "copyc4"]
    for fid in follower_ids:
        fake.add_account(fid, D("1000"))
        await AccountRepo(db).upsert(fid, "follower", fid, f"0x{fid}")
        await SettingsRepo(db).upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("0.01")),
            fid,
        )
    followers = [
        FollowerRuntime(account_id=fid, client=fake, identity=None) for fid in follower_ids
    ]
    executor = Executor(
        lambda aid: fake, CommandRepo(db), OrderLinkRepo(db), {}, settings=SettingsRepo(db)
    )
    deltas: list = []

    async def on_delta(delta):
        deltas.append(delta)
        await coordinator.on_master_delta(delta)

    projector = MasterProjector(
        MasterEventRepo(db), CopyTradeRepo(db), OrderLinkRepo(db), SettingsRepo(db), on_delta
    )
    coordinator = Coordinator(
        followers=followers,
        settings=SettingsRepo(db),
        executor=executor,
        trades=CopyTradeRepo(db),
        links=OrderLinkRepo(db),
        filters_provider=lambda: {"BTCUSDT": btc_filters()},
        app_flags=SettingsRepo(db),
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        fee_buffer_pct=D("0.001"),
        working_type="MARK_PRICE",
    )
    yield projector, coordinator, fake, db, deltas, follower_ids
    await db.close()


def follower_qty(fake: FakeAster, account_id: str) -> D:
    return fake.positions.get((account_id, "BTCUSDT:BOTH"), D(0))


def follower_stop(fake: FakeAster, account_id: str) -> FakeOrder | None:
    for key, order in fake.orders.items():
        if (
            key.startswith(f"{account_id}|")
            and order.type == "STOP_MARKET"
            and order.status == "NEW"
        ):
            return order
    return None


async def open_master_long(projector, fake, E=1000, qty="1"):
    await projector.apply_raw(
        order_trade(E=E, x="TRADE", X="FILLED", l=qty, z=qty, ap="65000", L="65000", q=qty)
    )
    await projector.apply_raw(stop_new(E=E + 10, c="m-stop", sp="64500"))


class TestOpenAndClose:
    async def test_master_open_copied_with_qty_and_stop(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        for fid in fids:
            assert follower_qty(fake, fid) == D("0.019"), fid
            stop = follower_stop(fake, fid)
            assert stop is not None and stop.stop_price == D("64500")
            assert stop.reduce_only and stop.side.value == "SELL"

    async def test_master_close_flattens_everyone(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="1",
                z="1",
                ap="65500",
                L="65500",
                q="1",
                rp="5",
            )
        )
        await asyncio.sleep(0)
        for fid in fids:
            assert follower_qty(fake, fid) == 0, fid
            assert follower_stop(fake, fid) is None, fid


class TestReduceAndStops:
    async def test_master_partial_reduce_scales_followers(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        await projector.apply_raw(
            order_trade(
                E=1200,
                S="SELL",
                x="TRADE",
                X="FILLED",
                l="0.3",
                z="0.3",
                ap="65200",
                L="65200",
                q="0.3",
            )
        )
        for fid in fids:
            # scaled_reduce(0.019, 1.0, 0.7) = floor(0.0133, 0.001) = 0.013
            assert follower_qty(fake, fid) == D("0.013"), fid

    async def test_stop_farther_reduces_qty(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        qty_before = follower_qty(fake, "copyc1")
        # master moves stop farther: 64500 -> 64000
        await projector.apply_raw(stop_new(E=1300, c="m-stop-2", sp="64000", i=102))
        await projector.apply_raw(
            order_trade(
                E=1301,
                c="m-stop",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64500",
                i=101,
            )
        )
        qty_after = follower_qty(fake, "copyc1")
        assert qty_after < qty_before
        # new cap = budget 10 / (1000 * 1.001) = 0.00999 -> 0.009
        assert qty_after == D("0.009")
        stop = follower_stop(fake, "copyc1")
        assert stop is not None and stop.stop_price == D("64000")

    async def test_stop_closer_keeps_qty(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        qty_before = follower_qty(fake, "copyc1")
        await projector.apply_raw(stop_new(E=1400, c="m-stop-3", sp="64900", i=103))
        await projector.apply_raw(
            order_trade(
                E=1401,
                c="m-stop",
                x="CANCELED",
                X="CANCELED",
                o="STOP_MARKET",
                ot="STOP_MARKET",
                sp="64500",
                i=101,
            )
        )
        assert follower_qty(fake, "copyc1") == qty_before  # no auto-increase


class TestFailureIsolation:
    async def test_margin_error_isolates_follower_and_degrades(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        fake.fail_next["copyc3"] = "-2019"
        await open_master_long(projector, fake)
        for fid in fids:
            if fid == "copyc3":
                assert follower_qty(fake, fid) == 0
            else:
                assert follower_qty(fake, fid) == D("0.019"), fid
        flag = await SettingsRepo(db).get_flag("degraded")
        assert flag is not None and "copyc3" in flag
        row = await db.fetchone(
            "SELECT status, last_error FROM execution_commands WHERE account_id='copyc3'"
            " AND status='FAILED'"
        )
        assert row is not None and "-2019" in row["last_error"]

    async def test_503_retries_and_succeeds(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        fake.fail_next["copyc2"] = "503"
        await open_master_long(projector, fake)
        assert follower_qty(fake, "copyc2") == D("0.019")
        # retry used a suffixed coid
        coids = [c for a, c in fake.placed_log if a == "copyc2"]
        assert any(c.endswith("r2") for c in coids)


class TestRestartRecovery:
    async def test_boot_resolve_unknown_no_duplicates(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        # simulate a crash after the exchange accepted an order but before
        # the journal recorded CONFIRMED: UNKNOWN row + live order on exchange
        coid = "ct-crash0000000000000001"
        fake.orders[f"copyc1|{coid}"] = FakeOrder(
            symbol="BTCUSDT",
            side="SELL",
            type="STOP_MARKET",
            orig_qty=D("0.019"),
            stop_price=D(64500),
            reduce_only=True,
            client_order_id=coid,
            order_id=999,
            update_seq=1,
        )
        await db.execute(
            "INSERT INTO execution_commands(command_key, account_id, trade_id, kind,"
            " payload, priority, status, attempts, client_order_id, created_at,"
            " updated_at) VALUES ('k-crash', 'copyc1', NULL, 'PLACE_STOP', ?, 2,"
            " 'UNKNOWN', 1, ?, datetime('now'), datetime('now'))",
            ('{"symbol": "BTCUSDT"}', coid),
        )
        await db.conn.commit()
        confirmed = await resolve_boot_unknowns(lambda aid: fake, CommandRepo(db))
        assert confirmed == 1
        # no duplicate order was sent
        assert ("copyc1", coid) not in [(a, c) for a, c in fake.placed_log]
        row = await db.fetchone("SELECT status FROM execution_commands WHERE command_key='k-crash'")
        assert row["status"] == "CONFIRMED"


class TestPauseAndWhitelist:
    async def test_pause_blocks_entries_close_passes(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        for fid in fids:
            await SettingsRepo(db).upsert_policy(
                RiskPolicy(
                    risk_percent=D("0.01"),
                    max_leverage=20,
                    max_entry_deviation_pct=D("0.01"),
                    allow_new_entries=False,
                ),
                fid,
            )
        await open_master_long(projector, fake)
        assert all(follower_qty(fake, fid) == 0 for fid in fids)
        # now give copyc1 a position and close the master: close must pass
        fake.positions[("copyc1", "BTCUSDT:BOTH")] = D("0.019")
        await projector.apply_raw(
            order_trade(E=1500, S="SELL", x="TRADE", X="FILLED", l="1", z="1", ap="65000", q="1")
        )
        assert follower_qty(fake, "copyc1") == 0

    async def test_non_whitelisted_symbol_ignored(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        raw = order_trade(
            E=1600, s="SOLUSDT", x="TRADE", X="FILLED", l="10", z="10", ap="150", L="150", q="10"
        )
        raw["o"]["s"] = "SOLUSDT"
        await projector.apply_raw(raw)
        assert all(len(fake.placed_log) == 0 for _ in [0])
        assert all(follower_qty(fake, fid) == 0 for fid in fids)


class TestSelfStopGuard:
    async def test_follower_stop_fill_blocks_reentry_same_episode(self, env):
        projector, coordinator, fake, db, deltas, fids = env
        await open_master_long(projector, fake)
        # copyc2's stop is touched manually (price reached its stop)
        fake.trigger_stop_fill("copyc2")
        assert follower_qty(fake, "copyc2") == 0
        # master still long -> next recompute must NOT re-enter copyc2
        await projector.apply_raw(stop_new(E=1700, c="m-stop-4", sp="64400", i=104))
        assert follower_qty(fake, "copyc2") == 0
        assert len(follower_qty_all(fake, "copyc2")) == 0

        # master closes and opens a NEW episode -> copyc2 may enter again
        await projector.apply_raw(
            order_trade(E=1800, S="SELL", x="TRADE", X="FILLED", l="1", z="1", ap="65000", q="1")
        )
        await asyncio.sleep(0)
        trade = await CopyTradeRepo(db).active_trade("BTCUSDT", "BOTH")
        assert trade is None  # episode closed
        await projector.apply_raw(
            order_trade(E=1900, x="TRADE", X="FILLED", l="1", z="1", ap="65000", q="1", i=200)
        )
        # new episode must carry its OWN stop: the dead level from the previous
        # episode is cleared on close and naked entries are refused
        await projector.apply_raw(stop_new(E=1905, c="m-stop-5", sp="64400", i=205))
        await asyncio.sleep(0)
        # new episode entry is sized from the CURRENT master stop (64400):
        # budget 10 / (600 * 1.001) -> 0.016
        assert follower_qty(fake, "copyc2") == D("0.016")


def follower_qty_all(fake, account_id):
    return [k for k in fake.positions if k[0] == account_id]
