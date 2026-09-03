"""P2.6 storage regression tests: durable Builder identity lifecycle.

Covers the append-only migration/backfill (existing rows become
``identity_source=direct`` with NULL Builder fields), the snapshot
set/clear primitives, and the direct/disconnect invalidation path that
guarantees a reconnect can never resurrect an old proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from copytradeaster.storage.database import MIGRATIONS, Database
from copytradeaster.storage.repositories import AccountRepo

USER = "0x1111111111111111111111111111111111111111"
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "builder_identity.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


async def test_fresh_account_is_direct_without_builder_proof(db: Database):
    await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER)

    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")

    assert snapshot is not None
    assert snapshot["identity_source"] == "direct"
    assert snapshot["builder_address"] is None
    assert snapshot["builder_name"] is None
    assert snapshot["builder_max_fee_rate"] is None
    assert snapshot["builder_agent_expires_at_ms"] is None
    assert snapshot["builder_verified_at"] is None


async def test_migration_backfills_existing_rows_as_direct(tmp_path: Path):
    database = Database(tmp_path / "legacy.sqlite3")
    await database.connect()
    try:
        assert database.conn is not None
        # Apply every migration except the P2.6 identity statements, recording
        # versions exactly like migrate() so the final migrate() only applies
        # the new block — simulating a pre-P2.6 database file on upgrade.
        p26_start = next(
            index for index, statement in enumerate(MIGRATIONS) if "identity_source" in statement
        )
        for version in range(p26_start):
            await database.conn.execute("BEGIN")
            try:
                await database.conn.execute(MIGRATIONS[version])
                await database.conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
                await database.conn.commit()
            except Exception:
                await database.conn.rollback()
                raise
        await AccountRepo(database).upsert("tg-legacy", "follower", "tg-legacy", USER)

        applied = await database.migrate()
        assert applied == len(MIGRATIONS) - p26_start
        snapshot = await AccountRepo(database).get_builder_snapshot("tg-legacy")
    finally:
        await database.close()

    assert snapshot is not None
    assert snapshot["identity_source"] == "direct"
    assert snapshot["builder_address"] is None
    assert snapshot["builder_name"] is None
    assert snapshot["builder_max_fee_rate"] is None
    assert snapshot["builder_agent_expires_at_ms"] is None
    assert snapshot["builder_verified_at"] is None


async def test_set_builder_snapshot_marks_builder_with_complete_proof(db: Database):
    await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER)

    await AccountRepo(db).set_builder_snapshot(
        "tg-7",
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at="2026-09-03T12:00:00+00:00",
    )
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")

    assert snapshot is not None
    assert snapshot["identity_source"] == "builder"
    assert snapshot["builder_address"] == BUILDER
    assert snapshot["builder_name"] == "copytradeaster"
    assert snapshot["builder_max_fee_rate"] == "0.0001"
    assert snapshot["builder_agent_expires_at_ms"] == 2_000_000_000_000
    assert snapshot["builder_verified_at"] == "2026-09-03T12:00:00+00:00"


async def test_set_identity_direct_clears_old_builder_proof(db: Database):
    await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER)
    await AccountRepo(db).set_builder_snapshot(
        "tg-7",
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at="2026-09-03T12:00:00+00:00",
    )

    await AccountRepo(db).set_identity_direct("tg-7")
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")

    assert snapshot is not None
    assert snapshot["identity_source"] == "direct"
    assert snapshot["builder_address"] is None
    assert snapshot["builder_name"] is None
    assert snapshot["builder_max_fee_rate"] is None
    assert snapshot["builder_agent_expires_at_ms"] is None
    assert snapshot["builder_verified_at"] is None


async def test_clear_builder_snapshot_invalidates_proof_without_source_flip(db: Database):
    await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER)
    await AccountRepo(db).set_builder_snapshot(
        "tg-7",
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at="2026-09-03T12:00:00+00:00",
    )

    await AccountRepo(db).clear_builder_snapshot("tg-7")
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")

    assert snapshot is not None
    # The proof is gone; a refresh must re-verify before serving verified.
    assert snapshot["builder_address"] is None
    assert snapshot["builder_verified_at"] is None


async def test_reconnect_after_clear_gets_no_old_proof(db: Database):
    await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER)
    await AccountRepo(db).set_builder_snapshot(
        "tg-7",
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at="2026-09-03T12:00:00+00:00",
    )
    await AccountRepo(db).clear_builder_snapshot("tg-7")

    # Simulate a reconnect read: the durable row carries no usable proof.
    snapshot = await AccountRepo(db).get_builder_snapshot("tg-7")

    assert snapshot is not None
    assert snapshot["builder_address"] is None
    assert snapshot["builder_max_fee_rate"] is None
    assert snapshot["builder_verified_at"] is None
