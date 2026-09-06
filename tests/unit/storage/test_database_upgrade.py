"""S1.6 upgrade regression: pre-S1 databases must migrate cleanly.

36c55d3 inserted the history schema mid-list, so databases migrated before
S1 recorded a ghost version 33 and skip the real CREATE TABLE
follower_fills at index 33. This models exactly that state (statements
0..32 applied, 33 recorded but never applied) and requires migrate() to
converge to the fresh schema with no missing tables or indexes.
"""

from __future__ import annotations

from pathlib import Path

from copytradeaster.storage.database import MIGRATIONS, Database


async def _build_ghost_v33(path: Path) -> Database:
    database = Database(path)
    await database.connect()
    assert database.conn is not None
    await database.conn.execute(MIGRATIONS[0])
    await database.conn.commit()
    for version in range(1, 33):
        await database.conn.execute("BEGIN")
        await database.conn.execute(MIGRATIONS[version])
        await database.conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        await database.conn.commit()
    # Ghost 33: recorded by pre-S1 code, never the real fills table.
    await database.conn.execute("INSERT INTO schema_migrations(version) VALUES (33)")
    await database.conn.commit()
    return database


async def _schema_names(database: Database) -> tuple[set[str], set[str]]:
    tables = await database.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    indexes = await database.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    )
    return ({row["name"] for row in tables}, {row["name"] for row in indexes})


async def test_ghost_v33_upgrades_to_fresh_schema(tmp_path: Path):
    database = await _build_ghost_v33(tmp_path / "ghost.sqlite3")
    try:
        applied = await database.migrate()
        assert applied == len(MIGRATIONS) - 34
        tables, indexes = await _schema_names(database)
        assert "follower_fills" in tables
        assert "idx_follower_fills_time" in indexes
        assert "idx_follower_fills_order" in indexes
        assert "user_activity_events" in tables
        assert "history_capture_state" in tables
        # Fills are writable immediately after the upgrade.
        await database.execute_write(
            "INSERT INTO accounts(id, role, label, user_address) VALUES "
            "('a', 'follower', 'a', '0x1111111111111111111111111111111111111111')"
        )
        await database.execute_write(
            "INSERT INTO follower_fills(account_id,environment,symbol,"
            "exchange_trade_id,order_id,side,position_side,qty,price,quote_qty,"
            "realized_pnl,commission,commission_asset,executed_at) VALUES "
            "('a','testnet','BTCUSDT','1','1','BUY','BOTH','1','100','100',"
            "'0','0.1','USDT',1)"
        )
        assert await database.migrate() == 0
    finally:
        await database.close()


async def test_fresh_and_upgraded_schemas_converge(tmp_path: Path):
    ghost = await _build_ghost_v33(tmp_path / "ghost.sqlite3")
    try:
        await ghost.migrate()
        ghost_tables, ghost_indexes = await _schema_names(ghost)
    finally:
        await ghost.close()
    fresh = Database(tmp_path / "fresh.sqlite3")
    await fresh.connect()
    try:
        await fresh.migrate()
        fresh_tables, fresh_indexes = await _schema_names(fresh)
    finally:
        await fresh.close()
    assert ghost_tables == fresh_tables
    assert ghost_indexes == fresh_indexes
