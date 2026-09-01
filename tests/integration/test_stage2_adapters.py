from __future__ import annotations

from pathlib import Path

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.config import Settings
from copytradeaster.storage.database import Database


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
