import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from eth_account import Account

from copytradeaster.credentials import crypto
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.storage.database import Database

KEY32 = bytes(range(32))
WRONG_KEY = bytes(range(32, 64))
SIGNER_KEY = "0x" + "33" * 32
MAIN_KEY = "0x" + "44" * 32
USER = "0x" + "aa" * 20


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "cred.sqlite3")
    await d.connect()
    await d.migrate()
    yield d
    await d.close()


async def test_import_then_private_key_roundtrip(db):
    svc = SqliteCredentials(db, KEY32)
    await svc.import_identity(
        "copyc1", USER, SIGNER_KEY, "follower", "copyc1", main_private_key_hex=MAIN_KEY
    )
    assert svc.private_key("copyc1") == SIGNER_KEY
    assert svc.main_private_key("copyc1") == MAIN_KEY
    assert svc.has_main_key("copyc1")
    ident = svc.identity("copyc1")
    assert ident.user == USER
    assert ident.signer == Account.from_key(SIGNER_KEY).address
    assert ident.chain_id == 714
    assert svc.account_ids(role="follower") == ["copyc1"]


async def test_import_without_main_key(db):
    svc = SqliteCredentials(db, KEY32)
    await svc.import_identity("copyc2", USER, SIGNER_KEY, "follower", "copyc2")
    assert svc.main_private_key("copyc2") is None
    assert not svc.has_main_key("copyc2")


async def test_load_from_db_after_reimport(db):
    svc = SqliteCredentials(db, KEY32)
    await svc.import_identity("main", USER, SIGNER_KEY, "master", "main")
    # fresh instance over same DB warms cache via load()
    svc2 = SqliteCredentials(db, KEY32)
    assert svc2.account_ids() == []
    await svc2.load()
    assert svc2.private_key("main") == SIGNER_KEY
    assert svc2.identity("main").chain_id == 714


async def test_wrong_master_key_raises_invalid_tag(db):
    svc = SqliteCredentials(db, KEY32)
    await svc.import_identity("copyc1", USER, SIGNER_KEY, "follower", "copyc1")
    svc_bad = SqliteCredentials(db, WRONG_KEY)
    with pytest.raises(InvalidTag):
        await svc_bad.load()


async def test_blob_is_encrypted_json(db):
    await AccountRepo_upsert(db)
    svc = SqliteCredentials(db, KEY32)
    await svc.import_identity("copyc1", USER, SIGNER_KEY, "follower", "copyc1")
    row = await db.fetchone(
        "SELECT encrypted, algo FROM credentials WHERE account_id=?", ("copyc1",)
    )
    assert row["algo"] == "AESGCM-v1"
    raw = bytes(row["encrypted"])
    with pytest.raises(UnicodeDecodeError):
        json.loads(raw)  # not plaintext
    data = json.loads(crypto.decrypt(KEY32, raw))
    assert data["signer_key"] == SIGNER_KEY


async def test_identity_unknown_account(db):
    svc = SqliteCredentials(db, KEY32)
    with pytest.raises(KeyError):
        svc.identity("ghost")


async def test_trading_chain_configurable(db):
    svc = SqliteCredentials(db, KEY32, trading_chain_id=714)
    await svc.import_identity("copyc1", USER, SIGNER_KEY, "follower", "copyc1")
    assert svc.identity("copyc1").chain_id == 714
    svc_mainnet = SqliteCredentials(db, KEY32, trading_chain_id=1666)
    await svc_mainnet.load()
    assert svc_mainnet.identity("copyc1").chain_id == 1666


async def test_AccountRepo_upsert_helper(db):
    await AccountRepo_upsert(db)
    row = await db.fetchone("SELECT * FROM accounts WHERE id='x'")
    assert row is not None


async def AccountRepo_upsert(db):
    from copytradeaster.storage.repositories import AccountRepo

    await AccountRepo(db).upsert("x", "follower", "x", "0xu")
