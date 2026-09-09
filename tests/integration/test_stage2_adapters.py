from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.application.models import ActivityFilter
from copytradeaster.application.trade_views import feed_kinds
from copytradeaster.config import Settings
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import AccountRepo


async def test_real_app_composition_exposes_stage2_adapters(tmp_path: Path):
    db = Database(tmp_path / "stage2-composition.sqlite3")
    await db.connect()
    await db.migrate()
    app = App(
        Settings(
            encryption_key="11" * 32,
            db_path=tmp_path / "stage2-composition.sqlite3",
            log_path=tmp_path / "logs" / "stage2.log",
        ),
        db=db,
    )
    try:
        composition = build_stage2_composition(app, admin_ids=[])
        assert composition.runtime.testnet is True
        assert composition.runtime.ready is False
        assert composition.application is not None
        assert await composition.runtime.is_account_registered("tg-missing") is False
    finally:
        await app.stop()


async def test_real_feed_page_filters_kinds_in_sql_before_limit(tmp_path: Path):
    """``EventRepository.list_feed_page`` must filter kinds before LIMIT.

    Adapted from the removed runtime ``read_activity`` regression: the S3
    feed read path maps an ``ActivityFilter`` through ``feed_kinds`` and the
    repository applies the kind predicate in SQL.  If it were applied after
    the LIMIT window, a trade-filtered page would come back empty whenever
    the newest rows overall are system kinds.  The final invariant keeps
    both honest: the SQL predicate must select exactly the rows the shared
    kind map classifies into that filter.
    """
    db = Database(tmp_path / "stage2-feed.sqlite3")
    await db.connect()
    await db.migrate()
    try:
        events = EventRepository(db)
        scope = HistoryScope("acct-1", "testnet")
        await AccountRepo(db).upsert("acct-1", "follower", "acct-1", "0x" + "1" * 40)
        base_ms = int(datetime(2026, 9, 2, 10, 0, tzinfo=UTC).timestamp() * 1000)

        # Oldest -> newest: 14 trade rows, then 14 system rows on top, so the
        # newest unfiltered window holds no trade row at all.
        specs: list[tuple[str, str]] = [(f"trd-{i:02d}", "POSITION_CLOSED") for i in range(14)]
        specs += [(f"sys-{i:02d}", "ENTRY_SKIPPED") for i in range(14)]
        for position, (name, kind) in enumerate(specs):
            await events.record(
                scope,
                kind=kind,
                occurred_at=base_ms + position,
                recorded_at=base_ms + position,
                facts={"symbol": "BTCUSDT"},
                dedup_key=f"feed:{name}",
                trade_ref="ref-1" if kind == "POSITION_CLOSED" else None,
                notify_eligible=False,
            )

        limit = 11  # page_size + 1, i.e. the window the service asks for

        # Unfiltered: the newest window holds only system rows — this is the
        # window the old code filtered in Python.
        unfiltered, pinned = await events.list_feed_page(scope, high_water=None, limit=limit)
        assert len(unfiltered) == limit
        assert {row.event.kind for row in unfiltered} == {"ENTRY_SKIPPED"}

        # The exact old failure: with the predicate applied after LIMIT this
        # window contains no trade row at all.
        trade, _ = await events.list_feed_page(
            scope, kinds=feed_kinds(ActivityFilter.TRADE), high_water=pinned, limit=limit
        )
        assert len(trade) == limit, "trade rows beyond the general window were hidden"
        assert {row.event.kind for row in trade} == {"POSITION_CLOSED"}

        system, _ = await events.list_feed_page(
            scope, kinds=feed_kinds(ActivityFilter.SYSTEM), high_water=pinned, limit=limit
        )
        assert len(system) == limit
        assert {row.event.kind for row in system} == {"ENTRY_SKIPPED"}

        # Invariant: for every filter the SQL predicate selects exactly the
        # rows the shared kind map classifies into that filter.
        every = await events.list_owned(scope)
        assert len(every) == 28
        for activity_filter in (ActivityFilter.TRADE, ActivityFilter.SYSTEM):
            kinds = feed_kinds(activity_filter)
            assert kinds is not None
            selected, _ = await events.list_feed_page(scope, kinds=kinds, limit=200)
            assert {row.event.event_ref for row in selected} == {
                row.event.event_ref for row in every if row.event.kind in kinds
            }
    finally:
        await db.close()
