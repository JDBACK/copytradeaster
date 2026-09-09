"""P2.0 visual preview: production-like Home for the connected account.

Runs the real aiohttp transport + UserApplicationService and boots the engine
in-process against a consistent SQLite backup copy of the database (production
untouched) in unsigned dev-session mode.

NOTE: This local browser preview does NOT replace Telegram WebView acceptance
(safe-area, Telegram BackButton, real native WebApp haptics/theme).

Usage:
    python scripts/preview_miniapp_engine.py --account <ACCOUNT_ID> --confirm-engine-preview
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sqlite3
import sys
from pathlib import Path

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger
from copytradeaster.miniapp.server import MiniAppServer
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import TelegramUserRepo

log = get_logger("preview")

PREVIEW_USER_ID = 900_000_001
PREVIEW_DB = Path("data/miniapp-preview.sqlite3")
PROD_DB = Path("data/copytrade.sqlite3")


def _backup_sqlite(src_path: Path, dst_path: Path) -> None:
    """Create a consistent point-in-time SQLite copy using SQLite backup API."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()
    # Remove any stale sidecar files from previous preview runs
    for suffix in ("-wal", "-shm"):
        sidecar = dst_path.with_name(dst_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    src_conn = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _settings(port: int) -> Settings:
    settings = Settings(
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
    if not settings.is_testnet:
        sys.exit(
            "ERROR: preview_miniapp_engine can only be run against Aster Testnet.\n"
            f"rest_base_url={settings.rest_base_url!r}, ws_user_base={settings.ws_user_base!r}"
        )
    return settings


async def main(account_id: str, port: int) -> None:
    if PROD_DB.exists():
        _backup_sqlite(PROD_DB, PREVIEW_DB)
        log.info("consistent database backup created at %s", PREVIEW_DB)

    settings = _settings(port)
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()

    app = App(settings, db=db)
    composition = build_stage2_composition(app, admin_ids=[])

    # Rebind ownership to the preview dev user in the preview DB copy only:
    # clear referencing outbox rows, drop existing owner row, then upsert dev user.
    owner = await db.fetchone(
        "SELECT telegram_user_id FROM telegram_users WHERE account_id=?", (account_id,)
    )
    if owner is not None:
        await db.execute(
            "DELETE FROM notification_outbox WHERE telegram_user_id=?",
            (owner["telegram_user_id"],),
        )
    await db.execute("DELETE FROM telegram_users WHERE account_id=?", (account_id,))
    # A previous preview run may have left the dev user bound to another account
    # in the source snapshot. The preview copy is disposable: rebind cleanly.
    await db.execute("DELETE FROM notification_outbox WHERE telegram_user_id=?", (PREVIEW_USER_ID,))
    await db.execute("DELETE FROM telegram_users WHERE telegram_user_id=?", (PREVIEW_USER_ID,))
    await TelegramUserRepo(db).upsert(
        PREVIEW_USER_ID, PREVIEW_USER_ID, role="USER", account_id=account_id
    )

    server = MiniAppServer(settings, composition.application)
    # Boot the full trading engine against the preview DB copy so the
    # follower runtime is genuinely registered (production-like session).
    await app.start()
    await server.start()

    url = f"http://127.0.0.1:{port}/miniapp/"
    print()
    print("=== ASTER COPY P2.0 Engine Preview ===")
    print(f"URL:             {url}")
    print(f"Dev user:        {PREVIEW_USER_ID} (testnet unsigned session)")
    print(f"Target account:  {account_id}")
    print("Notice:          Local browser preview only (not Telegram acceptance).")
    print("Stop:            Ctrl+C")
    print("======================================")

    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await app.stop()
        await db.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P2.0 engine preview for connected testnet account."
    )
    parser.add_argument(
        "--account",
        required=True,
        help="Follower account ID to bind the preview dev user to (e.g. tg-8264892202)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8085,
        help="Local port for preview server (default: 8085)",
    )
    parser.add_argument(
        "--confirm-engine-preview",
        action="store_true",
        required=True,
        help="Explicit confirmation required to boot full testnet engine in preview mode",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.account, args.port))
