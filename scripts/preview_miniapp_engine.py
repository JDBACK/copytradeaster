"""P2.0 visual preview: production-like Home for the connected account.

Same as scripts/preview_miniapp.py, but the dev user is bound to the existing
connected follower account in the preview DB copy (production untouched).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger
from copytradeaster.miniapp.server import MiniAppServer
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import TelegramUserRepo  # noqa: F401

log = get_logger("preview")

PREVIEW_USER_ID = 900_000_001
ACCOUNT_ID = "tg-8264892202"
PREVIEW_DB = Path("data/miniapp-preview.sqlite3")
PROD_DB = Path("data/copytrade.sqlite3")


def _settings(port: int) -> Settings:
    return Settings(
        db_path=PREVIEW_DB,
        log_path=Path("data/logs/miniapp-preview.log"),
        miniapp_enabled=True,
        miniapp_bind_host="127.0.0.1",
        miniapp_port=port,
        miniapp_static_dir=Path("miniapp/dist"),
        miniapp_dev_unsigned=True,
        miniapp_dev_user_id=PREVIEW_USER_ID,
        telegram_bot_token=None,
        telegram_notifications_enabled=False,
    )


async def main(port: int) -> None:
    if PROD_DB.exists():
        PREVIEW_DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PROD_DB, PREVIEW_DB)
        for suffix in ("-wal", "-shm"):
            src = PROD_DB.with_name(PROD_DB.name + suffix)
            if src.exists():
                shutil.copy(src, PREVIEW_DB.with_name(PREVIEW_DB.name + suffix))
        log.info("database copied")

    settings = _settings(port)
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()

    app = App(settings, db=db)
    composition = build_stage2_composition(app, admin_ids=[])

    # Rebind ownership to the preview dev user in the preview DB copy only:
    # clear referencing outbox rows, drop the copied owner row, then insert
    # the preview user as the owner.
    owner = await db.fetchone(
        "SELECT telegram_user_id FROM telegram_users WHERE account_id=?", (ACCOUNT_ID,)
    )
    if owner is not None:
        await db.execute(
            "DELETE FROM notification_outbox WHERE telegram_user_id=?",
            (owner["telegram_user_id"],),
        )
    await db.execute("DELETE FROM telegram_users WHERE account_id=?", (ACCOUNT_ID,))
    await TelegramUserRepo(db).upsert(
        PREVIEW_USER_ID, PREVIEW_USER_ID, role="USER", account_id=ACCOUNT_ID
    )

    server = MiniAppServer(settings, composition.application)
    # Boot the full trading engine against the preview DB copy so the
    # follower runtime is genuinely registered (production-like session).
    await app.start()
    await server.start()
    log.info("preview ready on http://127.0.0.1:%s/miniapp/", port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    try:
        asyncio.run(main(port))
    except KeyboardInterrupt:
        pass
