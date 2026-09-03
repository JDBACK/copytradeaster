import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from eth_account import Account

from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import (
    CASEFOLD_ADDRESS_INDEX_VERSION,
    MIGRATIONS,
    Database,
    MigrationConflictError,
)
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    NotificationOutboxRepo,
    OwnershipConflictError,
    SettingsRepo,
    TelegramAccessRequestRepo,
    TelegramUserRepo,
)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "wp1.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


def _validation(signer_address: str) -> AgentValidation:
    return AgentValidation(
        signer_address=signer_address,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="192.0.2.1",
        status="VALID",
        checked_at="2026-08-23T00:00:00+00:00",
    )


async def test_stage1_database_gets_only_append_only_wp1_migrations(tmp_path: Path):
    """A DB at the pre-WP1 schema upgrades without rebuilding Stage1 tables."""
    database = Database(tmp_path / "stage1.sqlite3")
    await database.connect()
    try:
        stage1_migration_count = next(
            version
            for version, statement in enumerate(MIGRATIONS)
            if "CREATE TABLE telegram_users" in statement
        )
        assert stage1_migration_count > 0
        assert database.conn is not None
        await database.conn.execute(MIGRATIONS[0])
        await database.conn.commit()
        for version in range(1, stage1_migration_count):
            await database.conn.execute("BEGIN")
            await database.conn.execute(MIGRATIONS[version])
            await database.conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
            await database.conn.commit()

        applied = await database.migrate()
        assert applied == len(MIGRATIONS) - stage1_migration_count
        tables = await database.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {row["name"] for row in tables}
        assert {
            "telegram_users",
            "agent_validations",
            "notification_outbox",
            "telegram_access_requests",
        } <= names
        indexes = await database.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        )
        index_names = {row["name"] for row in indexes}
        assert "idx_accounts_user_address_lower" in index_names
        assert "idx_notification_outbox_status_available" in index_names
        assert "idx_telegram_access_requests_pending_user" in index_names
        assert await database.migrate() == 0
    finally:
        await database.close()


async def test_stage1_case_variant_addresses_fail_with_actionable_migration_error(
    tmp_path: Path,
):
    database = Database(tmp_path / "legacy-duplicates.sqlite3")
    await database.connect()
    try:
        stage1_migration_count = CASEFOLD_ADDRESS_INDEX_VERSION
        assert database.conn is not None
        await database.conn.execute(MIGRATIONS[0])
        for version in range(1, stage1_migration_count):
            await database.conn.execute(MIGRATIONS[version])
            await database.conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        await database.conn.execute(
            "INSERT INTO accounts(id, role, label, user_address) VALUES "
            "('copy-a', 'follower', 'copy-a', '0xAbCd'), "
            "('copy-b', 'follower', 'copy-b', '0xabcd')"
        )
        await database.conn.commit()

        with pytest.raises(MigrationConflictError, match="copy-a,copy-b"):
            await database.migrate()
    finally:
        await database.close()


async def test_explicit_transaction_closes_begin_cursor(db, monkeypatch):
    assert db.conn is not None
    original_execute = db.conn.execute
    closed: list[str] = []

    async def execute(sql, params=()):
        cursor = await original_execute(sql, params)
        if str(sql).startswith("BEGIN"):
            original_close = cursor.close

            async def close():
                closed.append(str(sql))
                await original_close()

            cursor.close = close
        return cursor

    monkeypatch.setattr(db.conn, "execute", execute)
    async with db.transaction():
        pass
    assert closed == ["BEGIN IMMEDIATE"]


async def test_lower_address_unique_constraint_is_case_insensitive(db):
    accounts = AccountRepo(db)
    await accounts.upsert("a1", "follower", "a1", "0xAbCd")
    with pytest.raises(sqlite3.IntegrityError):
        await accounts.upsert("a2", "follower", "a2", "0xabcd")


async def test_telegram_owner_accepts_only_existing_real_followers(db):
    accounts = AccountRepo(db)
    await accounts.upsert("follower", "follower", "follower", "0xfeed")
    await accounts.upsert("follower2", "follower", "follower2", "0xfade")
    await accounts.upsert("follower3", "follower", "follower3", "0xf00d")
    await accounts.upsert("master", "master", "master", "0xbeef")
    await accounts.upsert(
        "shadow",
        "follower",
        "shadow",
        "0xcafe",
        execution_mode="SHADOW",
    )
    users = TelegramUserRepo(db)

    await users.upsert(1, 10, account_id="follower")
    with pytest.raises(OwnershipConflictError):
        await users.upsert(2, 20, account_id="master")
    with pytest.raises(OwnershipConflictError):
        await users.upsert(3, 30, account_id="shadow")
    with pytest.raises(OwnershipConflictError):
        await users.upsert(4, 40, account_id="missing")

    await users.upsert(5, 50)
    with pytest.raises(OwnershipConflictError):
        await users.set_account(5, "master")
    assert await users.set_account(5, "follower2")

    await users.upsert(6, 60, role="ADMIN")
    assert await users.set_account(6, "follower3")
    admin_owner = await users.get(6)
    assert admin_owner["role"] == "ADMIN"
    assert admin_owner["account_id"] == "follower3"
    assert admin_owner["access_enabled"] == 1
    assert await users.set_access_enabled(6, False)
    assert (await users.get(6))["access_enabled"] == 0


async def test_policy_patch_preserves_unspecified_fields(db):
    await AccountRepo(db).upsert("a1", "follower", "a1", "0xabc")
    settings = SettingsRepo(db)
    original = RiskPolicy("0.035", 7, "0.004", enabled=True, allow_new_entries=True)
    await settings.upsert_policy(original, "a1", risk_base="wallet_equity")

    patched = await settings.patch_policy("a1", allow_new_entries=False)

    assert patched == RiskPolicy("0.035", 7, "0.004", enabled=True, allow_new_entries=False)
    row = await db.fetchone("SELECT * FROM copy_settings WHERE account_id='a1'")
    assert row["risk_base"] == "wallet_equity"
    assert row["max_leverage"] == 7
    assert row["max_entry_deviation_pct"] == "0.004"


async def test_access_request_approval_is_atomic_idempotent_and_preserves_owner(db):
    accounts = AccountRepo(db)
    await accounts.upsert("owned", "follower", "owned", "0x1234")
    users = TelegramUserRepo(db)
    await users.upsert(
        51,
        510,
        account_id="owned",
        notifications_enabled=False,
        access_enabled=False,
    )
    requests = TelegramAccessRequestRepo(db)

    pending = await requests.create_pending(
        51,
        510,
        username="second_account",
        display_name="Second Account",
    )
    created_row, created = await requests.create_pending_result(
        51,
        510,
        username="ignored",
        display_name="Ignored",
    )
    duplicate = await requests.create_pending(
        51,
        510,
        username="ignored",
        display_name="Ignored",
    )
    assert duplicate["id"] == pending["id"]
    assert created_row["id"] == pending["id"]
    assert not created
    assert await requests.pending_count() == 1

    approved, changed = await requests.decide(pending["id"], 100, approve=True)
    repeated, repeated_changed = await requests.decide(pending["id"], 100, approve=False)
    owner = await users.get(51)

    assert changed and not repeated_changed
    assert approved["status"] == repeated["status"] == "APPROVED"
    assert owner["account_id"] == "owned"
    assert owner["role"] == "USER"
    assert owner["notifications_enabled"] == 0
    assert owner["access_enabled"] == 1
    assert await requests.pending_count() == 0


async def test_access_request_rejection_does_not_create_user_and_race_decides_once(db):
    requests = TelegramAccessRequestRepo(db)
    first = await requests.create_pending(61, 610, username=None, display_name=None)
    rejected, changed = await requests.decide(first["id"], 100, approve=False)
    assert changed and rejected["status"] == "REJECTED"
    assert await TelegramUserRepo(db).get(61) is None

    second = await requests.create_pending(61, 610, username=None, display_name=None)
    results = await asyncio.gather(
        requests.decide(second["id"], 100, approve=True),
        requests.decide(second["id"], 100, approve=False),
    )
    assert sum(changed for _, changed in results) == 1
    assert len({str(row["status"]) for row, _ in results}) == 1


async def test_telegram_owner_relation_is_unique_under_concurrent_commits(db):
    from copytradeaster.credentials.service import SqliteCredentials

    signer = "0x" + "33" * 32
    svc = SqliteCredentials(db, bytes(range(32)))

    async def commit(account_id: str, user: str):
        return await svc.commit_onboarding(
            account_id,
            user,
            signer,
            "follower",
            account_id,
            501,
            9001,
            _validation(Account.from_key(signer).address),
        )

    results = await asyncio.gather(
        commit("a1", "0x" + "aa" * 20),
        commit("a2", "0x" + "bb" * 20),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, OwnershipConflictError) for result in results) == 1
    assert len(await AccountRepo(db).get_all(role="follower")) == 1
    owner = await TelegramUserRepo(db).get(501)
    assert owner is not None
    assert owner["account_id"] in {"a1", "a2"}


async def test_outbox_enqueue_claim_lease_recovery_and_bounded_retry(db):
    await AccountRepo(db).upsert("a1", "follower", "a1", "0xabc")
    await TelegramUserRepo(db).upsert(telegram_user_id=1, chat_id=10)
    outbox = NotificationOutboxRepo(db)
    base = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    row_id = await outbox.enqueue(
        "event-1",
        1,
        10,
        "STATUS",
        "INFO",
        "hello",
        account_id="a1",
        available_at=base,
    )
    assert (
        await outbox.enqueue(
            "event-1", 1, 10, "STATUS", "INFO", "duplicate", account_id="a1", available_at=base
        )
        == row_id
    )

    claimed = await outbox.claim_one("worker-1", now=base, lease_seconds=30)
    assert claimed is not None
    assert claimed["status"] == "SENDING"
    assert claimed["attempts"] == 1
    assert await outbox.claim_one("worker-2", now=base) is None

    after_lease = base + timedelta(seconds=31)
    assert await outbox.recover_expired(after_lease) == 1
    claimed_again = await outbox.claim_one("worker-2", now=after_lease, lease_seconds=30)
    assert claimed_again is not None
    assert claimed_again["attempts"] == 2

    assert await outbox.retry(
        row_id,
        "bad gateway",
        available_at=after_lease,
        max_attempts=2,
        worker_id="worker-2",
    )
    final = await outbox.get(row_id)
    assert final["status"] == "FAILED"
    assert final["last_error"] == "bad gateway"
