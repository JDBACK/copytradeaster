"""SQLite (aiosqlite, WAL) access: connection + ordered migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

SQLITE_BUSY_TIMEOUT_MS = 30_000

MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations(
      version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))
    """,
    """
    CREATE TABLE accounts(
      id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('master','follower')),
      label TEXT NOT NULL UNIQUE, user_address TEXT NOT NULL UNIQUE,
      enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')))
    """,
    """
    CREATE TABLE credentials(
      account_id TEXT PRIMARY KEY REFERENCES accounts(id),
      encrypted BLOB NOT NULL, algo TEXT NOT NULL DEFAULT 'AESGCM-v1', updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE copy_settings(
      account_id TEXT PRIMARY KEY REFERENCES accounts(id),
      risk_percent TEXT NOT NULL, max_leverage INTEGER NOT NULL,
      max_entry_deviation_pct TEXT NOT NULL, risk_base TEXT NOT NULL DEFAULT 'available_balance',
      enabled INTEGER NOT NULL DEFAULT 1, allow_new_entries INTEGER NOT NULL DEFAULT 1)
    """,
    """
    CREATE TABLE master_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, event_time INTEGER NOT NULL,
      received_at TEXT NOT NULL, raw TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE,
      processed_at TEXT)
    """,
    """
    CREATE TABLE copy_trades(
      trade_id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, pos_side TEXT NOT NULL,
      opened_at TEXT NOT NULL, closed_at TEXT, master_event_id INTEGER,
      status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED','SKIPPED')))
    """,
    """
    CREATE TABLE order_links(
      id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER REFERENCES copy_trades(trade_id),
      account_id TEXT NOT NULL, kind TEXT NOT NULL,
      master_client_order_id TEXT, follower_client_order_id TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(account_id, follower_client_order_id))
    """,
    """
    CREATE TABLE execution_commands(
      command_key TEXT PRIMARY KEY, account_id TEXT NOT NULL, trade_id INTEGER,
      kind TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL
        CHECK(status IN ('PENDING','SENDING','SUBMITTED','CONFIRMED','UNKNOWN','FAILED')),
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, client_order_id TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
    """,
    "CREATE INDEX IF NOT EXISTS idx_commands_account_status"
    " ON execution_commands(account_id, status)",
    """
    CREATE TABLE nonce_store(
      signer_address TEXT PRIMARY KEY, last_nonce INTEGER NOT NULL, updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """,
    """
    CREATE TABLE sync_basis(
      account_id TEXT NOT NULL, symbol TEXT NOT NULL, pos_side TEXT NOT NULL,
      master_ref_abs TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
      issued_target TEXT, updated_at TEXT NOT NULL,
      PRIMARY KEY(account_id, symbol, pos_side))
    """,
    """
    ALTER TABLE accounts ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'REAL'
      CHECK(execution_mode IN ('REAL','SHADOW'))
    """,
    """
    ALTER TABLE sync_basis ADD COLUMN zero_target INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE copy_trades ADD COLUMN direction INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE execution_commands ADD COLUMN operation_id TEXT
    """,
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Repositories deliberately keep each write short and commit it in
        # the same coroutine.  Explicit autocommit prevents an implicit
        # transaction from remaining open between the awaited ``execute``
        # and ``commit`` calls when many follower reconciliations fan out at
        # once.  Ordinary writes and nonce allocation are serialized through
        # ``write_lock`` below.
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        # concurrent writers: repository writes and nonce allocation share a
        # single connection-level lock.
        await self.conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")

    async def migrate(self) -> int:
        """Applies unapplied MIGRATIONS in order; returns number applied."""
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called before migrate()")
        # bootstrap the migration bookkeeping table itself
        await self.conn.execute(MIGRATIONS[0])
        await self.conn.commit()
        cur = await self.conn.execute("SELECT COALESCE(MAX(version), -1) FROM schema_migrations")
        try:
            (last_version,) = await cur.fetchone()
        finally:
            await cur.close()
        applied = 0
        for version in range(last_version + 1, len(MIGRATIONS)):
            await self.conn.execute("BEGIN")
            try:
                await self.conn.execute(MIGRATIONS[version])
                await self.conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
                await self.conn.commit()
                applied += 1
            except Exception:
                await self.conn.rollback()
                raise
        return applied

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called first")
        return await self.conn.execute(sql, params)

    async def execute_write(self, sql: str, params: tuple = ()) -> tuple[int, int | None]:
        """Execute one short write and finalize its cursor before commit.

        aiosqlite cursors are backed by real SQLite statements. Leaving a
        cursor alive across the commit boundary can retain a lock on some
        SQLite builds. Repositories use this helper for all ordinary writes so
        the statement lifetime is explicit and bounded.
        """
        async with self.write_lock:
            cur = await self.execute(sql, params)
            try:
                rowcount = cur.rowcount
                lastrowid = cur.lastrowid
            finally:
                await cur.close()
            if self.conn is None:
                raise RuntimeError("Database.connect() must be called first")
            await self.conn.commit()
        return rowcount, lastrowid

    async def fetchone(self, sql: str, params: tuple = ()) -> object | None:
        cur = await self.execute(sql, params)
        try:
            return await cur.fetchone()
        finally:
            await cur.close()

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        cur = await self.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()
