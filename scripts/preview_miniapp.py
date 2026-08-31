"""Local Mini App preview without Telegram polling (Stage 3 part 1).

Runs the real aiohttp transport + UserApplicationService against a COPY of
the production testnet database, in unsigned dev-session mode, so the owner
can walk the builder wizard in a plain browser (Chrome + MetaMask) without
touching the live bot process or its database.

Usage:
    python scripts/preview_miniapp.py [port]

Env knobs (defaults fit the preview):
    CT_MINIAPP_DEV_USER_ID  - dev user bound to the preview session (default 900000001)
    CT_DB_PATH              - database copy (default data/miniapp-preview.sqlite3)
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from pathlib import Path

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger
from copytradeaster.miniapp.server import MiniAppServer
from copytradeaster.storage.database import Database

log = get_logger("preview")

PREVIEW_USER_ID = 900_000_001
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
    # Fresh copy of the production testnet snapshot; production stays intact.
    if PROD_DB.exists():
        PREVIEW_DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PROD_DB, PREVIEW_DB)
        sidecars = ("-wal", "-shm")
        for suffix in sidecars:
            src = PROD_DB.with_name(PROD_DB.name + suffix)
            if src.exists():
                shutil.copy(src, PREVIEW_DB.with_name(PREVIEW_DB.name + suffix))
        log.info("database copied from %s to %s", PROD_DB, PREVIEW_DB)

    settings = _settings(port)
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()

    app = App(settings, db=db)
    composition = build_stage2_composition(app, admin_ids=[])

    # Pre-allowlist the preview user exactly like the captcha access flow:
    # approved session, no connected account -> the wizard is shown.
    from copytradeaster.storage.repositories import TelegramUserRepo

    await TelegramUserRepo(db).upsert(PREVIEW_USER_ID, PREVIEW_USER_ID, role="USER")

    server = MiniAppServer(settings, composition.application)
    await server.start()
    url = f"http://127.0.0.1:{port}/miniapp/"
    print()
    print("=== ASTER COPY mini app preview ===")
    print(f"URL:        {url}")
    print(f"Dev user:   {PREVIEW_USER_ID} (unsigned dev session, testnet only)")
    print("Browser:    open in Chrome with MetaMask; the wallet signs a REAL")
    print("            testnet approveAgent (agent expires in 30 days).")
    print("Stop:       Ctrl+C")
    print("===================================")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await app.stop()
        await db.close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(port))
