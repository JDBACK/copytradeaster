"""P2.6 credential lifecycle regression tests: Builder proof durability.

A Builder onboarding commit atomically persists credentials, Agent
validation, ``identity_source=builder`` and the verified snapshot; direct
onboarding/replacement clears any Builder proof; local disconnect removes
credentials, validation and the snapshot in the same durable transaction
without claiming a remote Aster revoke (``remove_identity`` plumbing is
purely local — it takes no HTTP client and emits no management calls).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eth_account import Account

from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import AgentValidation, SettingsRepo

KEY32 = bytes(range(32))
SIGNER_KEY = "0x" + "33" * 32
USER = "0x" + "aa" * 20
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "builder-lifecycle.sqlite3")
    await database.connect()
    try:
        await database.migrate()
        yield database
    finally:
        await database.close()


def validation() -> AgentValidation:
    return AgentValidation(
        signer_address=Account.from_key(SIGNER_KEY).address,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="192.0.2.1",
        status="VALID",
        checked_at="2026-09-03T00:00:00+00:00",
    )


async def _builder_commit(credentials: SqliteCredentials) -> None:
    await credentials.commit_onboarding(
        "tg-7",
        USER,
        SIGNER_KEY,
        "follower",
        "tg-7",
        owner_telegram_user_id=7,
        owner_chat_id=700,
        validation=validation(),
        identity_source="builder",
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at="2026-09-03T12:00:00+00:00",
    )


async def test_builder_commit_persists_complete_snapshot(db: Database):
    credentials = SqliteCredentials(db, KEY32)
    await _builder_commit(credentials)

    account = await db.fetchone("SELECT * FROM accounts WHERE id='tg-7'")
    assert account is not None
    assert account["identity_source"] == "builder"
    assert account["builder_address"] == BUILDER
    assert account["builder_name"] == "copytradeaster"
    assert account["builder_max_fee_rate"] == "0.0001"
    assert account["builder_agent_expires_at_ms"] == 2_000_000_000_000
    assert account["builder_verified_at"] == "2026-09-03T12:00:00+00:00"
    assert await db.fetchone("SELECT * FROM credentials WHERE account_id='tg-7'") is not None
    assert await db.fetchone("SELECT * FROM agent_validations WHERE account_id='tg-7'") is not None


async def test_direct_commit_clears_builder_proof(db: Database):
    # Re-onboarding a disconnected account as direct carries no old proof.
    credentials = SqliteCredentials(db, KEY32)
    await _builder_commit(credentials)
    assert await credentials.remove_identity("tg-7") is True

    await credentials.commit_onboarding(
        "tg-7",
        USER,
        SIGNER_KEY,
        "follower",
        "tg-7",
        owner_telegram_user_id=7,
        owner_chat_id=700,
        validation=validation(),
    )

    account = await db.fetchone("SELECT * FROM accounts WHERE id='tg-7'")
    assert account is not None
    assert account["identity_source"] == "direct"
    assert account["builder_address"] is None
    assert account["builder_name"] is None
    assert account["builder_max_fee_rate"] is None
    assert account["builder_agent_expires_at_ms"] is None
    assert account["builder_verified_at"] is None


async def test_direct_replacement_clears_old_builder_proof(db: Database):
    credentials = SqliteCredentials(db, KEY32)
    await _builder_commit(credentials)
    new_signer_key = "0x" + "44" * 32
    new_validation = AgentValidation(
        signer_address=Account.from_key(new_signer_key).address,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="192.0.2.1",
        status="VALID",
        checked_at="2026-09-03T00:00:00+00:00",
    )

    await credentials.replace_identity(
        "tg-7",
        USER,
        new_signer_key,
        "follower",
        "tg-7",
        new_validation,
    )

    account = await db.fetchone("SELECT * FROM accounts WHERE id='tg-7'")
    assert account is not None
    assert account["identity_source"] == "direct"
    assert account["builder_address"] is None
    assert account["builder_verified_at"] is None


async def test_disconnect_clears_proof_without_remote_revoke(db: Database):
    credentials = SqliteCredentials(db, KEY32)
    await _builder_commit(credentials)
    await SettingsRepo(db).upsert_policy(
        RiskPolicy("0.035", 7, "0.004", enabled=True, allow_new_entries=True),
        "tg-7",
        risk_base="wallet_equity",
    )

    assert await credentials.remove_identity("tg-7") is True

    account = await db.fetchone("SELECT * FROM accounts WHERE id='tg-7'")
    assert account is not None
    assert int(account["enabled"]) == 0
    assert account["identity_source"] == "direct"
    assert account["builder_address"] is None
    assert account["builder_verified_at"] is None
    assert await db.fetchone("SELECT * FROM credentials WHERE account_id='tg-7'") is None
    assert await db.fetchone("SELECT * FROM agent_validations WHERE account_id='tg-7'") is None
    # Telegram ownership is retained for audit; the reconnect reads no proof.
    owner = await db.fetchone("SELECT * FROM telegram_users WHERE telegram_user_id=7")
    assert owner is not None
    assert owner["account_id"] == "tg-7"
