"""User activity events: immutable owned facts with stable feed cursors."""

from __future__ import annotations

import pytest

from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope


def repo_for(db):
    return EventRepository(db)


def facts(**updates):
    base = {"symbol": "BTCUSDT", "qty": "1", "price": "100"}
    base.update(updates)
    return base


async def record(repo, scope, **updates):
    params = {
        "kind": "POSITION_OPENED",
        "occurred_at": 60,
        "recorded_at": 70,
        "facts": facts(),
        "dedup_key": "cmd:open:1",
    }
    params.update(updates)
    return await repo.record(scope, **params)


async def test_record_returns_opaque_ref_and_canonical_facts(storage):
    db, _, scope = storage
    stored = await record(repo_for(db), scope)
    assert stored.scope == scope and stored.seq == 1
    event = stored.event
    assert len(event.event_ref) == 32 and event.event_ref != "cmd:open:1"
    assert event.facts == facts() and event.trade_ref is None
    assert event.notify_eligible is False


async def test_replay_with_same_dedup_key_returns_first_write(storage):
    db, _, scope = storage
    repo = repo_for(db)
    first = await record(repo, scope)
    second = await repo.record(
        scope,
        kind="POSITION_CLOSED",
        occurred_at=90,
        recorded_at=95,
        facts=facts(qty="0"),
        dedup_key="cmd:open:1",
        trade_ref="other",
        notify_eligible=True,
    )
    assert second == first
    assert len(await db.fetchall("SELECT * FROM user_activity_events")) == 1


async def test_distinct_keys_store_separate_events_in_seq_order(storage):
    db, _, scope = storage
    repo = repo_for(db)
    first = await record(repo, scope)
    second = await record(
        repo,
        scope,
        kind="POSITION_CLOSED",
        occurred_at=90,
        recorded_at=95,
        facts=facts(qty="0"),
        dedup_key="cmd:close:1",
        trade_ref="trade-1",
        notify_eligible=True,
    )
    assert second.seq == first.seq + 1
    assert second.event.trade_ref == "trade-1" and second.event.notify_eligible is True


async def test_owned_reads_reject_foreign_scope(storage):
    db, _, scope = storage
    repo = repo_for(db)
    stored = await record(repo, scope)
    assert await repo.get(HistoryScope("b", "testnet"), stored.event.event_ref) is None
    assert await repo.get(HistoryScope("a", "mainnet"), stored.event.event_ref) is None
    assert await repo.get(scope, "missing") is None
    assert await repo.list_owned(HistoryScope("b", "testnet")) == []
    assert await repo.list_by_trade(HistoryScope("b", "testnet"), "trade-1") == []


async def test_feed_cursor_is_stable_and_bounded(storage):
    db, _, scope = storage
    repo = repo_for(db)
    refs = []
    for index in range(5):
        stored = await record(repo, scope, dedup_key=f"cmd:{index}")
        refs.append(stored.event.event_ref)
    page = await repo.list_owned(scope, after_seq=2, limit=2)
    assert [item.event.event_ref for item in page] == refs[2:4]
    # Late backfill appends at the end; opened pages never shift.
    late = await record(
        repo,
        scope,
        kind="POSITION_REDUCED",
        occurred_at=10,
        recorded_at=500,
        facts=facts(qty="0.5"),
        dedup_key="cmd:late",
    )
    assert late.seq == 6
    assert [item.event.event_ref for item in await repo.list_owned(scope)] == refs + [
        late.event.event_ref
    ]
    with pytest.raises(ValueError):
        await repo.list_owned(scope, after_seq=-1)
    with pytest.raises(ValueError):
        await repo.list_owned(scope, limit=0)
    with pytest.raises(ValueError):
        await repo.list_owned(scope, limit=201)


async def test_kind_filter_rejects_unknown(storage):
    db, _, scope = storage
    repo = repo_for(db)
    await record(repo, scope)
    page = await repo.list_owned(scope, kinds=["POSITION_OPENED"])
    assert len(page) == 1
    assert await repo.list_owned(scope, kinds=["POSITION_CLOSED"]) == []
    with pytest.raises(ValueError):
        await repo.list_owned(scope, kinds=["MARKET_CLOSE"])
    with pytest.raises(ValueError):
        await repo.list_owned(scope, kinds=[])


async def test_list_by_trade_links_detail_sequence(storage):
    db, _, scope = storage
    repo = repo_for(db)
    await record(repo, scope)
    linked = await record(
        repo, scope, kind="POSITION_CLOSED", dedup_key="cmd:close:1", trade_ref="trade-1"
    )
    assert [item.event.event_ref for item in await repo.list_by_trade(scope, "trade-1")] == [
        linked.event.event_ref
    ]
    assert await repo.list_by_trade(scope, "trade-unknown") == []
    with pytest.raises(ValueError):
        await repo.list_by_trade(scope, "")


async def test_validation_rejects_unsafe_or_malformed_input(storage):
    db, _, scope = storage
    repo = repo_for(db)
    with pytest.raises(ValueError):
        await record(repo, scope, kind="MARKET_CLOSE")
    with pytest.raises(ValueError):
        await record(repo, scope, facts={})
    with pytest.raises(ValueError):
        await record(repo, scope, facts={"qty": 1.5})
    with pytest.raises(ValueError):
        await record(repo, scope, facts={"qty": ["1"]})
    with pytest.raises(ValueError):
        await record(repo, scope, occurred_at=-1)
    with pytest.raises(ValueError):
        await record(repo, scope, dedup_key="")
    with pytest.raises(ValueError):
        await record(repo, scope, trade_ref="")
    assert await db.fetchall("SELECT * FROM user_activity_events") == []


async def test_events_survive_restart_without_ref_churn(storage):
    db, _, scope = storage
    repo = repo_for(db)
    stored = await record(repo, scope)
    await db.close()
    await db.connect()
    assert await EventRepository(db).get(scope, stored.event.event_ref) == stored


async def test_concurrent_replays_resolve_to_one_row(storage):
    import asyncio

    db, _, scope = storage
    repo = repo_for(db)
    first, second = await asyncio.gather(record(repo, scope), record(repo, scope))
    assert first == second
    assert len(await db.fetchall("SELECT * FROM user_activity_events")) == 1
