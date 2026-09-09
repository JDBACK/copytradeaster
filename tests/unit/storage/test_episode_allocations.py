"""C2 allocation index: published shares, scoped joins, stable paging."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import (
    AllocatedFill,
    EpisodeRepository,
    EpisodeRevisionConflict,
)
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from tests.unit.history.test_projector import fill


@pytest.fixture
async def alloc_env(tmp_path):
    db = Database(tmp_path / "allocations.sqlite3")
    await db.connect()
    await db.migrate()
    for name in ("a", "b"):
        await db.execute_write(
            "INSERT INTO accounts(id,role,label,user_address) VALUES (?, 'follower',?,?)",
            (name, name, name),
        )
    yield db, HistoryScope("a", "testnet")
    await db.close()


def both_project(rows, seed_qty=D(0)):
    return project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(seed_qty)},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )


async def publish_all(repo, scope, episodes):
    refs = []
    for episode in episodes:
        ref = await repo.reserve_ref(scope, "BTCUSDT", episode.position_side, episode.public_ref)
        refs.append(ref)
    return await repo.publish(
        scope,
        "BTCUSDT",
        [replace(episode, public_ref=ref) for episode, ref in zip(episodes, refs, strict=True)],
        {ref: 0 for ref in refs},
    )


async def commit_rows(history, scope, rows):
    start = min(row.time for row in rows)
    end = max(row.time for row in rows)
    window = Window(start, end)
    return await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(tuple(rows), (window,), (), (), 1), checked_at=end + 1
    )


async def test_publish_indexes_exit_gross_pro_rata_with_exact_sum(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    (episode,) = both_project(
        [fill(1, "BUY", "2"), fill(2, "BUY", "1", "130"), fill(3, "SELL", "3", "140", "70")]
    )
    (stored,) = await publish_all(repo, scope, [episode])
    rows = await db.fetchall(
        "SELECT exchange_trade_id,role,allocated_qty,allocated_realized_gross"
        " FROM follower_episode_allocations WHERE trade_ref=? ORDER BY exchange_trade_id",
        (stored.episode.public_ref,),
    )
    assert [(r["exchange_trade_id"], r["role"]) for r in rows] == [
        ("1", "entry"),
        ("2", "entry"),
        ("3", "exit"),
    ]
    assert [r["allocated_qty"] for r in rows] == ["2", "1", "3"]
    gross = [D(str(r["allocated_realized_gross"])) for r in rows]
    assert gross[0] == 0 and gross[1] == 0 and gross[2] == D("70")


async def test_partial_exits_split_gross_and_sum_exactly(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    rows = [
        fill(1, "BUY", "3"),
        fill(2, "SELL", "1", "110", "10"),
        fill(3, "SELL", "2", "120", "30"),
    ]
    (episode,) = both_project(rows)
    (stored,) = await publish_all(repo, scope, [episode])
    (shown, _) = await repo.list_allocated_fills(scope, [stored.episode.public_ref])
    # No fills committed yet: the join needs exchange facts, so nothing shows.
    assert shown == []
    await commit_rows(HistoryRepository(db), scope, rows)
    (shown, _) = await repo.list_allocated_fills(scope, [stored.episode.public_ref])
    exits = [item for item in shown if item.role == "exit"]
    assert [item.allocated_qty for item in exits] == [D("1"), D("2")]
    assert sum(item.allocated_realized_gross for item in exits) == D("40")
    assert all(item.allocated_realized_gross == 0 for item in shown if item.role == "entry")


async def test_unknown_role_carries_no_gross(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    (episode,) = project_episodes(
        [fill(1, "BUY", "2"), fill(2, "SELL", "2", "110", "20")],
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(None, "unknown-position")},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    assert all(a.role == "unknown" for a in episode.allocations)
    (stored,) = await publish_all(repo, scope, [episode])
    rows = [fill(1, "BUY", "2"), fill(2, "SELL", "2", "110", "20")]
    await commit_rows(HistoryRepository(db), scope, rows)
    (shown, _) = await repo.list_allocated_fills(scope, [stored.episode.public_ref])
    assert len(shown) == 2
    assert all(item.allocated_realized_gross == 0 for item in shown)


async def test_hedge_legs_never_share_fills(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    episodes = project_episodes(
        [fill(1, "BUY", "2", leg="LONG"), fill(2, "SELL", "3", leg="SHORT")],
        symbol="BTCUSDT",
        seeds={PosSide.LONG: PositionSeed(D(0)), PosSide.SHORT: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    long, short = sorted(episodes, key=lambda e: e.direction)
    (stored_long, stored_short) = await publish_all(repo, scope, [long, short])
    await commit_rows(
        HistoryRepository(db),
        scope,
        [fill(1, "BUY", "2", leg="LONG"), fill(2, "SELL", "3", leg="SHORT")],
    )
    (long_fills, _) = await repo.list_allocated_fills(scope, [stored_long.episode.public_ref])
    (short_fills, _) = await repo.list_allocated_fills(scope, [stored_short.episode.public_ref])
    assert [item.exchange_trade_id for item in long_fills] == ["1"]
    assert [item.exchange_trade_id for item in short_fills] == ["2"]
    assert long_fills[0].position_side == "LONG"
    assert short_fills[0].position_side == "SHORT"


async def test_shared_flip_fill_splits_and_sums_to_source(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    long, short = both_project([fill(1, "BUY", "2"), fill(2, "SELL", "3", "110", "20")])
    (stored_long, stored_short) = await publish_all(repo, scope, [long, short])
    await commit_rows(
        HistoryRepository(db), scope, [fill(1, "BUY", "2"), fill(2, "SELL", "3", "110", "20")]
    )
    (long_fills, _) = await repo.list_allocated_fills(scope, [stored_long.episode.public_ref])
    (short_fills, _) = await repo.list_allocated_fills(scope, [stored_short.episode.public_ref])
    shared_long = next(item for item in long_fills if item.exchange_trade_id == "2")
    shared_short = next(item for item in short_fills if item.exchange_trade_id == "2")
    assert (shared_long.role, shared_long.allocated_qty) == ("exit", D("2"))
    assert (shared_short.role, shared_short.allocated_qty) == ("entry", D("1"))
    assert shared_long.allocated_qty + shared_short.allocated_qty == D("3")
    assert shared_long.allocated_realized_gross == D("20")
    assert shared_short.allocated_realized_gross == 0
    assert isinstance(shared_long, AllocatedFill)


async def test_allocated_fills_are_owned_by_scope(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    (episode,) = both_project([fill(1, "BUY", "1")])
    (stored,) = await publish_all(repo, scope, [episode])
    await commit_rows(HistoryRepository(db), scope, [fill(1, "BUY", "1")])
    foreign = HistoryScope("b", "testnet")
    (shown, _) = await repo.list_allocated_fills(foreign, [stored.episode.public_ref])
    assert shown == []


async def test_revision_change_trips_page_conflict(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    (episode,) = both_project([fill(1, "BUY", "1"), fill(2, "BUY", "1")])
    (stored,) = await publish_all(repo, scope, [episode])
    await commit_rows(HistoryRepository(db), scope, [fill(1, "BUY", "1"), fill(2, "BUY", "1")])
    ref = stored.episode.public_ref
    (_, cursor) = await repo.list_allocated_fills(scope, [ref], limit=1)
    assert cursor is not None
    changed = replace(stored.episode, source="external")
    await repo.publish(scope, "BTCUSDT", [changed], {ref: 1})
    with pytest.raises(EpisodeRevisionConflict):
        await repo.list_allocated_fills(scope, [ref], cursor, limit=1)


async def test_malformed_cursor_is_value_error(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    (episode,) = both_project([fill(1, "BUY", "1")])
    (stored,) = await publish_all(repo, scope, [episode])
    with pytest.raises(ValueError):
        await repo.list_allocated_fills(scope, [stored.episode.public_ref], "not-a-cursor")
    with pytest.raises(ValueError):
        await repo.list_allocated_fills(scope, [stored.episode.public_ref], limit=0)
    with pytest.raises(ValueError):
        await repo.list_allocated_fills(scope, [])


async def test_rebuild_refills_index_from_payloads(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    (episode,) = both_project([fill(1, "BUY", "2"), fill(2, "SELL", "2", "140", "60")])
    (stored,) = await publish_all(repo, scope, [episode])
    await db.execute_write(
        "DELETE FROM follower_episode_allocations WHERE trade_ref=?", (stored.episode.public_ref,)
    )
    (empty, _) = await repo.list_allocated_fills(scope, [stored.episode.public_ref])
    assert empty == []
    count = await repo.rebuild_allocations(scope, "BTCUSDT")
    assert count == 2
    (shown, _) = await repo.list_allocated_fills(scope, [stored.episode.public_ref])
    # Still empty without exchange facts; the index itself is refilled.
    assert shown == []
    rows = await db.fetchall(
        "SELECT COUNT(*) AS n FROM follower_episode_allocations WHERE trade_ref=?",
        (stored.episode.public_ref,),
    )
    assert rows[0]["n"] == 2


async def test_merge_drops_superseded_allocations(alloc_env):
    db, scope = alloc_env
    repo = EpisodeRepository(db)
    rows = [
        fill(1, "BUY", "1"),
        fill(2, "SELL", "1", "110", "10"),
        fill(3, "BUY", "1"),
        fill(4, "SELL", "1", "110", "10"),
    ]
    first, second = both_project(rows)
    a, b = await publish_all(repo, scope, [first, second])
    await repo.merge_ref(scope, b.episode.public_ref, a.episode.public_ref)
    rows = await db.fetchall(
        "SELECT COUNT(*) AS n FROM follower_episode_allocations WHERE trade_ref=?",
        (b.episode.public_ref,),
    )
    assert rows[0]["n"] == 0
    family = await repo.alias_family(scope, a.episode.public_ref)
    assert family == (a.episode.public_ref, b.episode.public_ref)
