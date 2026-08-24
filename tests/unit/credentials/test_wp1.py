import asyncio
import json
from pathlib import Path

import pytest
from eth_account import Account

from copytradeaster.credentials import crypto
from copytradeaster.credentials import service as service_module
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AgentValidation,
    OwnershipConflictError,
    SettingsRepo,
)

KEY32 = bytes(range(32))
SIGNER_KEY = "0x" + "33" * 32
NEW_SIGNER_KEY = "0x" + "44" * 32
USER = "0x" + "aa" * 20


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "wp1-credentials.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


def validation(signer_key: str = SIGNER_KEY) -> AgentValidation:
    return AgentValidation(
        signer_address=Account.from_key(signer_key).address,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="192.0.2.1",
        status="VALID",
        checked_at="2026-08-23T00:00:00+00:00",
    )


async def test_atomic_onboarding_writes_paused_policy_owner_and_validation(db):
    credentials = SqliteCredentials(db, KEY32)
    await credentials.commit_onboarding(
        "copyc1",
        USER,
        SIGNER_KEY,
        "follower",
        "copyc1",
        owner_telegram_user_id=100,
        owner_chat_id=200,
        validation=validation(),
    )

    account = await db.fetchone("SELECT * FROM accounts WHERE id='copyc1'")
    policy = await db.fetchone("SELECT * FROM copy_settings WHERE account_id='copyc1'")
    owner = await db.fetchone("SELECT * FROM telegram_users WHERE telegram_user_id=100")
    check = await db.fetchone("SELECT * FROM agent_validations WHERE account_id='copyc1'")
    assert account["enabled"] == 1
    assert policy["allow_new_entries"] == 0
    assert policy["enabled"] == 1
    assert owner["account_id"] == "copyc1"
    assert check["signer_address"].casefold() == Account.from_key(SIGNER_KEY).address.casefold()
    assert credentials.private_key("copyc1") == SIGNER_KEY


async def test_concurrent_direct_commits_same_enabled_account_have_one_winner(db):
    credentials = SqliteCredentials(db, KEY32)
    second_db = Database(db.path)
    await second_db.connect()
    await second_db.migrate()
    second_credentials = SqliteCredentials(second_db, KEY32)
    second_user = "0x" + "bb" * 20

    async def commit(target: SqliteCredentials, user: str, signer_key: str, chat_id: int):
        return await target.commit_onboarding(
            "tg-200",
            user,
            signer_key,
            "follower",
            "tg-200",
            owner_telegram_user_id=200,
            owner_chat_id=chat_id,
            validation=validation(signer_key),
        )

    try:
        results = await asyncio.gather(
            commit(credentials, USER, SIGNER_KEY, 300),
            commit(second_credentials, second_user, NEW_SIGNER_KEY, 301),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, OwnershipConflictError) for result in results) == 1

        account = await db.fetchone("SELECT enabled, user_address FROM accounts WHERE id='tg-200'")
        owner = await db.fetchone("SELECT chat_id FROM telegram_users WHERE telegram_user_id=200")
        blob = await db.fetchone("SELECT encrypted FROM credentials WHERE account_id='tg-200'")
        payload = json.loads(crypto.decrypt(KEY32, bytes(blob["encrypted"])))
        assert account["enabled"] == 1
        assert account["user_address"] in {USER, second_user}
        assert owner["chat_id"] in {300, 301}
        assert payload["signer_key"] in {SIGNER_KEY, NEW_SIGNER_KEY}
    finally:
        await second_db.close()


async def test_injected_failure_rolls_back_all_atomic_rows_and_cache(db, monkeypatch):
    credentials = SqliteCredentials(db, KEY32)
    real_execute = service_module._conn_execute

    async def fail_on_credential(conn, sql, params=()):
        if sql.startswith("INSERT INTO credentials"):
            raise RuntimeError("injected credential write failure")
        return await real_execute(conn, sql, params)

    monkeypatch.setattr(service_module, "_conn_execute", fail_on_credential)
    with pytest.raises(RuntimeError, match="injected credential write failure"):
        await credentials.commit_onboarding(
            "copyc1",
            USER,
            SIGNER_KEY,
            "follower",
            "copyc1",
            owner_telegram_user_id=100,
            owner_chat_id=200,
            validation=validation(),
        )

    assert await db.fetchone("SELECT * FROM accounts WHERE id='copyc1'") is None
    assert await db.fetchone("SELECT * FROM credentials WHERE account_id='copyc1'") is None
    assert await db.fetchone("SELECT * FROM copy_settings WHERE account_id='copyc1'") is None
    assert await db.fetchone("SELECT * FROM telegram_users WHERE telegram_user_id=100") is None
    assert await db.fetchone("SELECT * FROM agent_validations WHERE account_id='copyc1'") is None
    assert credentials.account_ids() == []


async def test_replacement_failure_keeps_old_blob_and_cache(db, monkeypatch):
    credentials = SqliteCredentials(db, KEY32)
    await credentials.import_identity("copyc1", USER, SIGNER_KEY, "follower", "copyc1")
    old_blob = await db.fetchone("SELECT encrypted FROM credentials WHERE account_id='copyc1'")
    real_execute = service_module._conn_execute

    async def fail_on_credential(conn, sql, params=()):
        if sql.startswith("INSERT INTO credentials"):
            raise RuntimeError("injected replacement failure")
        return await real_execute(conn, sql, params)

    monkeypatch.setattr(service_module, "_conn_execute", fail_on_credential)
    with pytest.raises(RuntimeError, match="injected replacement failure"):
        await credentials.replace_identity(
            "copyc1",
            USER,
            NEW_SIGNER_KEY,
            "follower",
            "copyc1",
            validation(NEW_SIGNER_KEY),
        )

    new_blob = await db.fetchone("SELECT encrypted FROM credentials WHERE account_id='copyc1'")
    assert bytes(new_blob["encrypted"]) == bytes(old_blob["encrypted"])
    assert credentials.private_key("copyc1") == SIGNER_KEY


async def test_account_credentials_repr_does_not_expose_private_keys(db):
    credentials = SqliteCredentials(db, KEY32)
    await credentials.import_identity(
        "copyc1",
        USER,
        SIGNER_KEY,
        "follower",
        "copyc1",
        main_private_key_hex=NEW_SIGNER_KEY,
    )

    rendered = repr(credentials._cache["copyc1"])
    assert SIGNER_KEY not in rendered
    assert NEW_SIGNER_KEY not in rendered


async def test_disabled_real_account_with_deleted_blob_is_ignored_at_boot(db):
    credentials = SqliteCredentials(db, KEY32)
    await credentials.import_identity("copyc1", USER, SIGNER_KEY, "follower", "copyc1")
    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id='copyc1'")
    await db.execute_write("DELETE FROM credentials WHERE account_id='copyc1'")

    fresh = SqliteCredentials(db, KEY32)
    await fresh.load()

    assert fresh.account_ids() == []
    row = await db.fetchone("SELECT enabled FROM accounts WHERE id='copyc1'")
    assert row["enabled"] == 0


async def test_disconnect_is_atomic_and_retains_telegram_owner(db):
    credentials = SqliteCredentials(db, KEY32)
    await credentials.commit_onboarding(
        "tg-100",
        USER,
        SIGNER_KEY,
        "follower",
        "tg-100",
        owner_telegram_user_id=100,
        owner_chat_id=200,
        validation=validation(),
    )
    await SettingsRepo(db).upsert_policy(
        RiskPolicy("0.035", 7, "0.004", enabled=True, allow_new_entries=True),
        "tg-100",
        risk_base="wallet_equity",
    )

    assert await credentials.remove_identity("tg-100")
    assert credentials.account_ids() == []
    account = await db.fetchone("SELECT enabled FROM accounts WHERE id='tg-100'")
    policy = await db.fetchone("SELECT * FROM copy_settings WHERE account_id='tg-100'")
    owner = await db.fetchone("SELECT * FROM telegram_users WHERE telegram_user_id=100")
    assert account["enabled"] == 0
    assert policy["enabled"] == 0
    assert policy["allow_new_entries"] == 0
    assert policy["risk_percent"] == "0.035"
    assert policy["max_leverage"] == 7
    assert policy["max_entry_deviation_pct"] == "0.004"
    assert policy["risk_base"] == "wallet_equity"
    assert await db.fetchone("SELECT * FROM credentials WHERE account_id='tg-100'") is None
    assert await db.fetchone("SELECT * FROM agent_validations WHERE account_id='tg-100'") is None
    assert owner["account_id"] == "tg-100"

    # A disabled stable account may be re-onboarded with a new user address;
    # policy risk fields survive, participation is re-enabled, and entries
    # remain paused until the explicit resume action.
    await credentials.commit_onboarding(
        "tg-100",
        "0x" + "bb" * 20,
        NEW_SIGNER_KEY,
        "follower",
        "tg-100",
        owner_telegram_user_id=100,
        owner_chat_id=201,
        validation=validation(NEW_SIGNER_KEY),
    )
    reassigned = await db.fetchone(
        "SELECT account_id, chat_id FROM telegram_users WHERE telegram_user_id=100"
    )
    account = await db.fetchone("SELECT enabled, user_address FROM accounts WHERE id='tg-100'")
    policy = await db.fetchone("SELECT * FROM copy_settings WHERE account_id='tg-100'")
    assert account["enabled"] == 1
    assert account["user_address"] == "0x" + "bb" * 20
    assert policy["enabled"] == 1
    assert policy["allow_new_entries"] == 0
    assert policy["risk_percent"] == "0.035"
    assert policy["max_leverage"] == 7
    assert policy["max_entry_deviation_pct"] == "0.004"
    assert policy["risk_base"] == "wallet_equity"
    assert reassigned["account_id"] == "tg-100"
    assert reassigned["chat_id"] == 201
