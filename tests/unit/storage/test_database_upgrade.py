"""S1.6 upgrade regression: pre-S1 databases must migrate cleanly.

36c55d3 inserted the history schema mid-list, so databases migrated before
S1 recorded a ghost version 33 and skip the real CREATE TABLE
follower_fills at index 33. This models exactly that state (statements
0..32 applied, 33 recorded but never applied) and requires migrate() to
converge to the fresh schema with no missing tables or indexes.
"""

from __future__ import annotations

from pathlib import Path

from copytradeaster.storage.database import (
    EXTERNAL_CLOSE_INDEX_VERSION,
    MIGRATIONS,
    RECOVERY_CONTEXT_INDEX_VERSION,
    Database,
)


async def _apply_through(database: Database, last_version: int) -> None:
    """Apply the final-list statements through one historical version."""
    assert database.conn is not None
    for version, entry in enumerate(MIGRATIONS[: last_version + 1]):
        await database.conn.execute("BEGIN")
        steps = entry if isinstance(entry, tuple) else (entry,)
        for step in steps:
            await database.conn.execute(step)
        await database.conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        await database.conn.commit()


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


async def _build_previous_schema(path: Path) -> Database:
    database = Database(path)
    await database.connect()
    dirty_version = next(
        version
        for version, entry in enumerate(MIGRATIONS)
        if isinstance(entry, tuple) and any("dirty_from_ms" in step for step in entry)
    )
    await _apply_through(database, dirty_version - 1)
    return database


async def test_corrective_g1_upgrade_marks_retained_history_dirty(tmp_path: Path):
    database = await _build_previous_schema(tmp_path / "v57-history.sqlite3")
    try:
        assert database.conn is not None
        for account in ("a", "b"):
            await database.conn.execute(
                "INSERT INTO accounts(id,role,label,user_address) VALUES (?, 'follower',?,?)",
                (account, account, account),
            )
        # Existing state keeps its revision; earliest retained complete
        # coverage (10) precedes the earliest fill (40), so migration must not
        # use COALESCE(fill, coverage) and accidentally mark 40.
        await database.conn.execute(
            "INSERT INTO history_scope_state VALUES ('a','testnet','BTCUSDT',7,100)"
        )
        for account in ("a", "b"):
            await database.conn.execute(
                "INSERT INTO follower_fills(account_id,environment,symbol,exchange_trade_id,"
                "order_id,side,position_side,qty,price,quote_qty,realized_pnl,commission,"
                "commission_asset,executed_at) VALUES (?, 'testnet','BTCUSDT',?,?,'BUY','BOTH',"
                "'1','100','100','0','0.1','USDT',40)",
                (account, f"t-{account}", f"o-{account}"),
            )
            await database.conn.execute(
                "INSERT INTO history_sync_state VALUES (?, 'testnet','fills','BTCUSDT',"
                "10,50,'complete',100)",
                (account,),
            )
            await database.conn.execute(
                "INSERT INTO history_sync_state VALUES (?, 'testnet','fills','BTCUSDT',"
                "70,100,'complete',100)",
                (account,),
            )
        await database.conn.execute(
            "INSERT INTO follower_episode_refs(public_ref,account_id,environment,symbol,"
            "position_side,anchor) VALUES ('stable-ref','a','testnet','BTCUSDT','BOTH','legacy')"
        )
        await database.conn.commit()

        assert await database.migrate() == 5
        a = await database.fetchone(
            "SELECT facts_revision,dirty_from_ms FROM history_scope_state "
            "WHERE account_id='a' AND environment='testnet' AND symbol='BTCUSDT'"
        )
        b = await database.fetchone(
            "SELECT facts_revision,dirty_from_ms FROM history_scope_state "
            "WHERE account_id='b' AND environment='testnet' AND symbol='BTCUSDT'"
        )
        assert (a["facts_revision"], a["dirty_from_ms"]) == (7, 10)
        # Scope b had retained facts/coverage but no C1 state row: migration
        # creates a conservative pending revision rather than losing it.
        assert (b["facts_revision"], b["dirty_from_ms"]) == (1, 10)
        assert (
            await database.fetchone(
                "SELECT public_ref FROM follower_episode_refs WHERE public_ref='stable-ref'"
            )
            is not None
        )
        assert len(await database.fetchall("SELECT * FROM follower_fills")) == 2
        coverage = await database.fetchall(
            "SELECT start_ms,end_ms,state FROM history_sync_state WHERE account_id='a' "
            "AND stream='fills' ORDER BY start_ms"
        )
        assert [(r["start_ms"], r["end_ms"], r["state"]) for r in coverage] == [
            (10, 50, "complete"),
            (70, 100, "complete"),
        ]
    finally:
        await database.close()


async def test_hardening_v33_lineage_keeps_recovery_context_and_adds_history(tmp_path: Path):
    """A DB that ran hardening's old v33 must converge without duplicate DDL."""
    database = Database(tmp_path / "hardening-v33.sqlite3")
    await database.connect()
    try:
        assert database.conn is not None
        await _apply_through(database, 32)
        await database.conn.execute(
            "ALTER TABLE execution_commands ADD COLUMN recovery_context TEXT"
        )
        await database.conn.execute("INSERT INTO schema_migrations(version) VALUES (33)")
        await database.conn.execute(
            "INSERT INTO execution_commands(command_key,account_id,trade_id,kind,payload,"
            "priority,status,attempts,last_error,client_order_id,created_at,updated_at,"
            "operation_id,recovery_context) VALUES "
            "('legacy-hardening','copy1',NULL,'MARKET_OPEN','{}',1,'UNKNOWN',1,NULL,"
            "'ctlegacy','2026-09-01','2026-09-01',NULL,'{\"symbol\":\"BTCUSDT\"}')"
        )
        await database.conn.commit()

        assert await database.migrate() == len(MIGRATIONS) - 34
        tables, _indexes = await _schema_names(database)
        assert "follower_fills" in tables
        assert "user_activity_events" in tables
        row = await database.fetchone(
            "SELECT recovery_context FROM execution_commands WHERE command_key='legacy-hardening'"
        )
        assert row is not None
        assert row["recovery_context"] == '{"symbol":"BTCUSDT"}'
        assert await database.migrate() == 0
    finally:
        await database.close()


async def test_frontend_v58_lineage_adds_recovery_and_close_proof_state(tmp_path: Path):
    """A frontend-v58 DB receives the later trading-core migrations."""
    database = Database(tmp_path / "frontend-v58.sqlite3")
    await database.connect()
    try:
        await _apply_through(database, RECOVERY_CONTEXT_INDEX_VERSION - 1)
        assert not await database._column_exists(  # noqa: SLF001 - migration lineage proof
            "execution_commands", "recovery_context"
        )

        assert await database.migrate() == 4
        assert await database._column_exists(  # noqa: SLF001 - migration lineage proof
            "execution_commands", "recovery_context"
        )
        applied = await database.fetchone("SELECT MAX(version) AS version FROM schema_migrations")
        assert applied is not None
        assert RECOVERY_CONTEXT_INDEX_VERSION == 59
        assert EXTERNAL_CLOSE_INDEX_VERSION == 60
        assert applied["version"] == len(MIGRATIONS) - 1 == 62
        sync_columns = {
            row["name"] for row in await database.fetchall("PRAGMA table_info(sync_basis)")
        }
        assert {
            "episode_trade_id",
            "last_observed_qty",
            "last_engine_close_key",
            "external_close_blocked",
            "external_close_reason",
            "own_protective_filled",
        } <= sync_columns
        assert await database._column_exists(  # noqa: SLF001 - migration lineage proof
            "execution_commands", "executed_qty"
        )
    finally:
        await database.close()


async def test_legacy_flat_protective_episode_is_quarantined_from_v60_and_v61(
    tmp_path: Path,
):
    """The baseline did not persist a FILLED order query result.

    A flat observation with a linked protection on the current episode is
    ambiguous after upgrade; it must not become fresh entry permission.
    The data-only v62 step also repairs databases where v61 already ran.
    """
    for old_version in (60, 61):
        database = Database(tmp_path / f"legacy-v{old_version}.sqlite3")
        await database.connect()
        try:
            await _apply_through(database, old_version)
            cases = (
                (1, "BTCUSDT", "OPEN", "0", 0, True),  # old FILLED could be here
                (2, "ETHUSDT", "OPEN", "0", 0, False),  # no bot-owned protection
                (3, "SOLUSDT", "CLOSED", "0", 0, True),  # old episode ended
                (4, "XRPUSDT", "OPEN", "0", 1, True),  # intentional dust flat
                (5, "ADAUSDT", "OPEN", "0.005", 0, True),  # still occupied
            )
            for trade_id, symbol, status, qty, dust, linked in cases:
                await database.execute_write(
                    "INSERT INTO copy_trades(trade_id,symbol,pos_side,opened_at,status)"
                    " VALUES (?,?,'BOTH','2026-09-12',?)",
                    (trade_id, symbol, status),
                )
                await database.execute_write(
                    "INSERT INTO sync_basis(account_id,symbol,pos_side,master_ref_abs,"
                    "revision,updated_at,zero_target,episode_trade_id,last_observed_qty)"
                    " VALUES ('copyc1',?,'BOTH','1',1,'2026-09-12',?,?,?)",
                    (symbol, dust, trade_id, qty),
                )
                if linked:
                    await database.execute_write(
                        "INSERT INTO order_links(trade_id,account_id,kind,follower_client_order_id)"
                        " VALUES (?,'copyc1','PLACE_STOP',?)",
                        (trade_id, f"ct-stop-{trade_id}"),
                    )

            assert await database.migrate() == 62 - old_version
            rows = {
                row["symbol"]: row
                for row in await database.fetchall("SELECT * FROM sync_basis ORDER BY symbol")
            }
            assert rows["BTCUSDT"]["external_close_blocked"] == 1
            assert rows["BTCUSDT"]["external_close_reason"] == "UPGRADE_FLAT_CAUSE_UNKNOWN"
            for symbol in ("ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"):
                assert rows[symbol]["external_close_blocked"] == 0
            assert await database.migrate() == 0
        finally:
            await database.close()
