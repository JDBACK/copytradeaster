from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.application.models import ActivityFilter
from copytradeaster.application.service import (
    _activity_kind_filter,
    _safe_activity_category,
    _safe_code,
)
from copytradeaster.config import Settings
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import CommandRepo


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


async def test_real_adapter_pushes_activity_category_filter_into_sql(tmp_path: Path):
    """``AppStage2RuntimeAdapter.read_activity`` must filter before LIMIT/OFFSET.

    Regression for the P1 pagination bug.  ``UserApplicationService._activity_rows``
    delegates to this real adapter whenever the runtime provides it, so the
    unit tests (which fall back to ``db.fetchall`` because ``FakeRuntime`` has
    no ``read_activity``) do not exercise the production SQL.  This test drives
    the real adapter against a real migrated SQLite database.

    It also pins the invariant that keeps both code paths honest: the SQL
    clause produced for a category must select exactly the rows that
    ``_safe_activity_category`` classifies into that category — including the
    unrecognised-kind fallback, which must land in ``system`` rather than
    silently vanishing from both filters.
    """
    db = Database(tmp_path / "stage2-activity.sqlite3")
    await db.connect()
    await db.migrate()
    app = App(
        Settings(
            encryption_key="22" * 32,
            db_path=tmp_path / "stage2-activity.sqlite3",
            log_path=tmp_path / "logs" / "stage2-activity.log",
        ),
        db=db,
    )
    try:
        runtime = build_stage2_composition(app, admin_ids=[]).runtime
        commands = CommandRepo(db)
        base = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)

        # Oldest -> newest: 14 system rows, then 14 trade rows, then one row
        # with an unrecognised kind (must still classify as system).
        specs: list[tuple[str, str]] = [(f"sys-{i:02d}", "SET_LEVERAGE") for i in range(14)]
        specs += [(f"trd-{i:02d}", "MARKET_OPEN") for i in range(14)]
        specs += [("weird-00", "SOME_FUTURE_KIND")]

        for position, (command_key, kind) in enumerate(specs):
            payload: dict[str, object] = {"symbol": "BTCUSDT"}
            if kind == "SET_LEVERAGE":
                payload["leverage"] = str(10 + position)
            await commands.insert(
                command_key,
                "acct-1",
                None,
                kind,
                json.dumps(payload, sort_keys=True),
                1,
                f"ct-{command_key}",
            )
            await db.execute_write(
                "UPDATE execution_commands SET status='CONFIRMED', updated_at=? "
                "WHERE command_key=?",
                ((base + timedelta(minutes=position)).isoformat(), command_key),
            )

        limit = 11  # page_size + 1, i.e. the window the service asks for

        # Unfiltered: the newest row is the unrecognised kind, the rest of the
        # window is trade rows — this is the window the old code filtered in
        # Python, and it held no SET_LEVERAGE row at all.
        unfiltered = await runtime.read_activity("acct-1", limit, 0)
        assert len(unfiltered) == limit
        assert {row["kind"] for row in unfiltered} == {"SOME_FUTURE_KIND", "MARKET_OPEN"}
        assert "SET_LEVERAGE" not in {row["kind"] for row in unfiltered}

        # The exact old failure: with the predicate applied after LIMIT/OFFSET
        # this window contains no system row at all.
        system = await runtime.read_activity(
            "acct-1", limit, 0, kind_filter=_activity_kind_filter(ActivityFilter.SYSTEM)
        )
        assert len(system) == limit, "system rows beyond the general window were hidden"
        assert {row["kind"] for row in system} == {"SET_LEVERAGE", "SOME_FUTURE_KIND"}

        trade = await runtime.read_activity(
            "acct-1", limit, 0, kind_filter=_activity_kind_filter(ActivityFilter.TRADE)
        )
        assert len(trade) == limit
        assert {row["kind"] for row in trade} == {"MARKET_OPEN"}

        # OFFSET walks the *filtered* set, not the general journal.
        system_page1 = await runtime.read_activity(
            "acct-1", limit, limit, kind_filter=_activity_kind_filter(ActivityFilter.SYSTEM)
        )
        assert len(system_page1) == 4, "offset must skip filtered rows, not general rows"
        trade_page1 = await runtime.read_activity(
            "acct-1", limit, limit, kind_filter=_activity_kind_filter(ActivityFilter.TRADE)
        )
        assert len(trade_page1) == 3

        # Default (no filter) keeps the previous three-argument contract.
        assert len(await runtime.read_activity("acct-1", limit, 0)) == limit

        # Invariant: for every category the SQL predicate selects exactly the
        # rows the safe mapper classifies into that category.
        every = await db.fetchall("SELECT kind FROM execution_commands WHERE account_id='acct-1'")
        for activity_filter, category in (
            (ActivityFilter.TRADE, "trade"),
            (ActivityFilter.SYSTEM, "system"),
        ):
            selected = await runtime.read_activity(
                "acct-1",
                1000,
                0,
                kind_filter=_activity_kind_filter(activity_filter),
            )
            assert {row["command_key"] for row in selected} == {
                row["command_key"]
                for row in await db.fetchall(
                    "SELECT command_key, kind FROM execution_commands WHERE account_id='acct-1'"
                )
                if _safe_activity_category(_safe_code(row["kind"], "command")).value == category
            }
        assert len(every) == 29
    finally:
        await app.stop()
