"""C3 review repros: deterministic transition events end to end.

One batch and four cumulative publications of the same fills must yield
identical semantic histories; a re-publish (including a costs-only
finalization, which changes no allocation) emits zero new drafts; a
week-old fill at a fresh frontier is recorded but never pushes; legacy
``evt:*`` rows are never rewritten — they are re-derived under modern
keys and linked with one silent ``HISTORY_CORRECTED``.
"""

from __future__ import annotations

from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.projector import project_with_transitions
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import MIGRATIONS, Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository
from tests.unit.history.test_projector import fill

ZERO_SEED = {PosSide.BOTH: PositionSeed(D(0))}
SOURCES = {1: "copy", 2: "copy", 3: "copy", 4: "copy"}


def repro_rows():
    return [
        fill(1, "BUY", "2"),
        fill(2, "BUY", "1", "130"),
        fill(3, "SELL", "1", "120", "10"),
        fill(4, "SELL", "2", "140", "60"),
    ]


async def commit_all(history, scope):
    rows = repro_rows()
    window = Window(0, 100)
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(tuple(rows), (window,), (), (), 1), checked_at=5
    )
    return rows


def publish_kwargs(cutoff, ending, boundary_version, recorded_at=10):
    return dict(
        replay_start_ms=0,
        cutoff_ms=cutoff,
        seeds=dict(ZERO_SEED),
        ending={PosSide.BOTH: ending},
        coverage_revision=1,
        boundary_version=boundary_version,
        origin=(0, dict(ZERO_SEED)),
        order_sources=dict(SOURCES),
        recorded_at=recorded_at,
        notify_eligible=True,
    )


async def fresh_db(tmp_path: Path, name: str):
    db = Database(tmp_path / name)
    await db.connect()
    await db.migrate()
    await db.execute_write(
        "INSERT INTO accounts(id,role,label,user_address) VALUES ('a','follower','a','a')"
    )
    return db


def projection(db, history, scope):
    return HistoryProjection(history, EpisodeRepository(db), EventRepository(db))


def stream(rows):
    return [
        (row.event.kind, row.event.dedup_key, row.event.facts, row.event.notify_eligible)
        for row in rows
    ]


async def test_batch_and_four_sequential_publications_agree(storage, tmp_path):
    db, history, scope = storage
    await commit_all(history, scope)
    sequential = projection(db, history, scope)
    endings = [D(2), D(3), D(2), D(0)]
    for position, (trade_id, ending) in enumerate(zip((1, 2, 3, 4), endings, strict=True)):
        version = None if position == 0 else position
        await sequential.publish_projection(
            scope, "BTCUSDT", **publish_kwargs(trade_id, ending, version)
        )
    sequential_rows = await EventRepository(db).list_owned(scope)

    batch_db = await fresh_db(tmp_path, "batch.sqlite3")
    try:
        batch_history = HistoryRepository(batch_db)
        batch_scope = scope
        await commit_all(batch_history, batch_scope)
        await projection(batch_db, batch_history, batch_scope).publish_projection(
            batch_scope, "BTCUSDT", **publish_kwargs(4, D(0), None)
        )
        batch_rows = await EventRepository(batch_db).list_owned(batch_scope)
    finally:
        await batch_db.close()

    assert [row.event.kind for row in sequential_rows] == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
    ]
    assert stream(sequential_rows) == stream(batch_rows)
    opened, increased, reduced, closed = (row.event for row in sequential_rows)
    assert (opened.facts["entry_qty"], opened.facts["entry_price"]) == ("2", "100")
    assert (increased.facts["added_qty"], increased.facts["price"]) == ("1", "130")
    assert reduced.facts["realized_gross"] == "10" and reduced.facts["remaining_qty"] == "2"
    assert closed.facts["entry_qty"] == "3" and closed.facts["realized_gross"] == "70"
    assert all(row.notify_eligible for row in (opened, increased, reduced, closed))


async def test_republish_after_costs_finalization_emits_nothing_new(storage):
    db, history, scope = storage
    await commit_all(history, scope)
    publisher = projection(db, history, scope)
    await publisher.publish_projection(scope, "BTCUSDT", **publish_kwargs(4, D(0), None))
    before = await EventRepository(db).list_owned(scope)
    assert len(before) == 4
    # Same transitions, new publication round: zero new drafts, same revisions.
    await publisher.publish_projection(scope, "BTCUSDT", **publish_kwargs(4, D(0), 1))
    after = await EventRepository(db).list_owned(scope)
    assert stream(after) == stream(before)


async def test_week_old_fill_at_fresh_frontier_does_not_push(storage):
    db, history, scope = storage
    await commit_all(history, scope)
    await projection(db, history, scope).publish_projection(
        scope, "BTCUSDT", **publish_kwargs(4, D(0), None, recorded_at=8 * 86400_000)
    )
    rows = await EventRepository(db).list_owned(scope)
    assert [row.event.kind for row in rows] == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
    ]
    assert [row.event.notify_eligible for row in rows] == [False] * 4


async def test_legacy_upgrade_republishes_and_links_correction_once(storage):
    db, history, scope = storage
    await commit_all(history, scope)
    episodes = EpisodeRepository(db)
    events = EventRepository(db)
    silent = HistoryProjection(history, episodes, None)
    (stored,) = await silent.publish_projection(
        scope, "BTCUSDT", **publish_kwargs(4, D(0), None, recorded_at=6)
    )
    ref = stored.episode.public_ref
    await events.record(
        scope,
        kind="POSITION_OPENED",
        trade_ref=ref,
        occurred_at=1,
        recorded_at=6,
        facts={"symbol": "BTCUSDT"},
        dedup_key=f"evt:open:{ref}",
        notify_eligible=True,
    )
    legacy_before = await events.list_owned(scope)
    assert [row.event.dedup_key for row in legacy_before] == [f"evt:open:{ref}"]

    await projection(db, history, scope).publish_projection(
        scope, "BTCUSDT", **publish_kwargs(4, D(0), 1, recorded_at=7)
    )
    rows = await events.list_owned(scope)
    kinds = [row.event.kind for row in rows]
    assert kinds == [
        "POSITION_OPENED",
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
        "HISTORY_CORRECTED",
    ]
    modern_opened = rows[1].event
    assert (modern_opened.facts["entry_qty"], modern_opened.facts["entry_price"]) == (
        "2",
        "100",
    )
    correction = rows[-1].event
    assert correction.trade_ref == ref and correction.notify_eligible is False
    assert correction.dedup_key == f"hist1:corrected:a:testnet:BTCUSDT:{ref}"
    # The legacy row is untouched: first-wins keeps the proven fact.
    assert rows[0].event.dedup_key == f"evt:open:{ref}"

    await projection(db, history, scope).publish_projection(
        scope, "BTCUSDT", **publish_kwargs(4, D(0), 2, recorded_at=8)
    )
    assert len(await events.list_owned(scope)) == len(rows)


async def test_flip_exit_carries_pro_rata_closed_share():
    rows = [fill(1, "BUY", "2"), fill(2, "SELL", "3", "110", "20")]
    result = project_with_transitions(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    closing = next(item for item in result.transitions if item.role == "exit")
    assert closing.qty == D(2) and closing.remainder == D(0)
    with localcontext() as context:
        context.prec = 80
        assert closing.closed_gross == D(20) * D(2) / D(3)
    opening = next(item for item in result.transitions if item.role == "entry" and item.qty == 1)
    assert opening.closed_gross == D(0) and opening.remainder == D(1)


async def test_unknown_and_empty_projections_yield_no_transitions():
    empty = project_with_transitions(
        [],
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(2), "placeholder")},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    assert empty.transitions == ()
    unknown = project_with_transitions(
        [fill(1, "BUY", "2")],
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(None, "unknown-position")},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    assert unknown.transitions == ()


async def test_pre56_database_upgrades_with_facts_preserved(tmp_path):
    path = tmp_path / "pre56.sqlite3"
    db = Database(path)
    await db.connect()
    assert db.conn is not None
    await db.conn.execute(MIGRATIONS[0])
    await db.conn.commit()
    for version in range(1, 56):
        await db.conn.execute("BEGIN")
        await db.conn.execute(MIGRATIONS[version])
        await db.conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        await db.conn.commit()
    await db.execute_write(
        "INSERT INTO accounts(id,role,label,user_address) VALUES ('a','follower','a','a')"
    )
    await db.execute_write(
        "INSERT INTO user_activity_events(event_ref,account_id,environment,trade_ref,"
        "kind,occurred_at,recorded_at,facts,dedup_key,notify_eligible)"
        " VALUES ('e1','a','testnet','r1','POSITION_OPENED',1,2,'{}','evt:open:r1',1)"
    )
    try:
        assert await db.migrate() == len(MIGRATIONS) - 56
        rows = await db.fetchall("SELECT kind,dedup_key FROM user_activity_events")
        assert [(row["kind"], row["dedup_key"]) for row in rows] == [
            ("POSITION_OPENED", "evt:open:r1")
        ]
        await db.execute_write(
            "INSERT INTO user_activity_events(event_ref,account_id,environment,trade_ref,"
            "kind,occurred_at,recorded_at,facts,dedup_key,notify_eligible)"
            " VALUES ('e2','a','testnet','r1','HISTORY_CORRECTED',1,3,'{}','k2',0)"
        )
        assert await db.migrate() == 0
    finally:
        await db.close()
