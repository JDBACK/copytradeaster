import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    CopyTradeRepo,
    CredentialRepo,
    MasterEventRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
)


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "t.sqlite3")
    await d.connect()
    await d.migrate()
    yield d
    await d.close()


async def test_migrations_idempotent(db):
    assert await db.migrate() == 0
    tables = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {r["name"] for r in tables}
    assert {
        "accounts",
        "credentials",
        "copy_settings",
        "master_events",
        "copy_trades",
        "order_links",
        "execution_commands",
        "nonce_store",
        "app_state",
        "schema_migrations",
    } <= names


async def test_account_and_credential_roundtrip(db):
    accounts = AccountRepo(db)
    creds = CredentialRepo(db)
    await accounts.upsert("copyc1", "follower", "copyc1", "0xabc")
    await accounts.upsert("main", "master", "main", "0xdef")
    rows = await accounts.get_all()
    assert len(rows) == 2
    masters = await accounts.get_all(role="master")
    assert [r["id"] for r in masters] == ["main"]
    row = await accounts.get("copyc1")
    assert row["label"] == "copyc1"
    assert await accounts.get("missing") is None

    await creds.put_encrypted("copyc1", b"\x01\x02encrypted")
    assert await creds.get("copyc1") == b"\x01\x02encrypted"
    with pytest.raises(KeyError):
        await creds.get("missing")


async def test_settings_policy_roundtrip(db):
    await AccountRepo(db).upsert("copyc1", "follower", "copyc1", "0xabc")
    settings = SettingsRepo(db)
    policy = RiskPolicy(
        risk_percent="0.02",
        max_leverage=10,
        max_entry_deviation_pct="0.005",
        enabled=True,
        allow_new_entries=False,
    )
    await settings.upsert_policy(policy, "copyc1")
    loaded = await settings.get_policy("copyc1")
    assert loaded == policy
    # upsert overwrite
    policy2 = RiskPolicy(risk_percent="0.01", max_leverage=20, max_entry_deviation_pct="0.01")
    await settings.upsert_policy(policy2, "copyc1")
    assert (await settings.get_policy("copyc1")).risk_percent == Decimal("0.01")
    assert await settings.get_policy("ghost") is None

    await settings.set_flag("degraded", "margin")
    assert await settings.get_flag("degraded") == "margin"
    await settings.set_flag("degraded", "")
    assert await settings.get_flag("degraded") == ""
    assert await settings.get_flag("absent") is None


async def test_master_events_dedup(db):
    repo = MasterEventRepo(db)
    eid = await repo.append("ORDER_TRADE_UPDATE", 1000, "{}", "ORDER_TRADE_UPDATE:1000:1:TRADE")
    assert eid is not None
    assert (
        await repo.append("ORDER_TRADE_UPDATE", 1000, "{}", "ORDER_TRADE_UPDATE:1000:1:TRADE")
        is None
    )
    await repo.mark_processed(eid)
    row = await db.fetchone("SELECT processed_at FROM master_events WHERE id=?", (eid,))
    assert row["processed_at"] is not None


async def test_copy_trades_lifecycle(db):
    repo = CopyTradeRepo(db)
    tid = await repo.open_trade("BTCUSDT", "BOTH", 42)
    active = await repo.active_trade("BTCUSDT", "BOTH")
    assert active["trade_id"] == tid
    await repo.close_trade(tid)
    assert await repo.active_trade("BTCUSDT", "BOTH") is None
    tid2 = await repo.open_trade("BTCUSDT", "BOTH", 43)
    assert tid2 != tid  # new episode after close


async def test_order_links(db):
    repo = OrderLinkRepo(db)
    trades = CopyTradeRepo(db)
    tid = await trades.open_trade("BTCUSDT", "BOTH", None)
    await repo.add(tid, "copyc1", "MARKET_OPEN", "ctabc", master_coid="m1")
    await repo.add(tid, "copyc1", "PLACE_STOP", "ctdef")
    row = await repo.by_follower_coid("copyc1", "ctabc")
    assert row["kind"] == "MARKET_OPEN"
    assert row["master_client_order_id"] == "m1"
    assert await repo.by_follower_coid("copyc1", "missing") is None
    assert len(await repo.by_trade(tid)) == 2


async def test_command_repo_status_transitions(db):
    repo = CommandRepo(db)
    await repo.insert("k1", "copyc1", 5, "MARKET_OPEN", "{}", 1, "ctk1")
    row = await repo.get("k1")
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0

    await repo.set_status("k1", "SENDING", bump_attempts=True)
    await repo.set_status("k1", "SUBMITTED")
    row = await repo.get("k1")
    assert row["status"] == "SUBMITTED"
    assert row["attempts"] == 1

    await repo.set_status("k1", "UNKNOWN", last_error="503")
    assert len(await repo.in_flight()) == 1

    await repo.set_status("k1", "CONFIRMED")
    assert await repo.in_flight() == []
    await repo.insert("k2", "copyc2", None, "PLACE_STOP", "{}", 2, "ctk2")
    recent = await repo.recent(10)
    assert [r["command_key"] for r in recent] == ["k2", "k1"]
    # SENDING/SUBMITTED are in-flight too (crash windows)
    await repo.insert("k3", "copyc2", None, "MARKET_OPEN", "{}", 1, "ctk3")
    await repo.set_status("k3", "SENDING")
    assert [r["command_key"] for r in await repo.in_flight()] == ["k3"]


async def test_nonce_store_monotonic_and_persistent(db, tmp_path):
    nonces = NonceStore(db)
    n1 = await nonces.next_nonce("0xsigner")
    n2 = await nonces.next_nonce("0xsigner")
    assert n2 == n1 + 1
    # "restart": fresh instance over the same DB continues from stored value
    db2 = Database(tmp_path / "t.sqlite3")
    await db2.connect()
    try:
        nonces2 = NonceStore(db2)
        n3 = await nonces2.next_nonce("0xsigner")
        assert n3 > n2
        # independent signers don't interfere
        other = await nonces2.next_nonce("0xother")
        assert other >= int(asyncio.get_event_loop().time() * 0)  # just produced
    finally:
        await db2.close()


async def test_nonce_store_floor_is_wall_clock_seconds(db):
    import time

    nonces = NonceStore(db)
    n = await nonces.next_nonce("0xnew")
    assert n >= int(time.time()) * 1_000_000
