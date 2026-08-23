from pathlib import Path

from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import AccountRepo, SettingsRepo


async def test_shadow_account_does_not_require_credentials(tmp_path: Path):
    db = Database(tmp_path / "shadow.sqlite3")
    await db.connect()
    await db.migrate()
    try:
        await AccountRepo(db).upsert(
            "shadow-001",
            "follower",
            "shadow-001",
            "shadow://shadow-001",
            execution_mode="SHADOW",
        )
        await SettingsRepo(db).upsert_policy(RiskPolicy("0.01", 20, "0.01"), "shadow-001")
        credentials = SqliteCredentials(db, bytes(range(32)))
        await credentials.load()
        assert credentials.account_ids() == []
        row = await db.fetchone("SELECT execution_mode FROM accounts WHERE id='shadow-001'")
        assert row["execution_mode"] == "SHADOW"
    finally:
        await db.close()
