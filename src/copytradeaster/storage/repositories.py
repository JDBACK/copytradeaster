"""Async repositories over the SQLite schema. One class per table."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiosqlite import Row

from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AccountRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, id_: str, role: str, label: str, user_address: str) -> None:
        await self.db.execute(
            """
            INSERT INTO accounts(id, role, label, user_address) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              role=excluded.role, label=excluded.label, user_address=excluded.user_address
            """,
            (id_, role, label, user_address),
        )
        await self.db.conn.commit()

    async def get_all(self, role: str | None = None) -> list[Row]:
        if role is None:
            return await self.db.fetchall("SELECT * FROM accounts ORDER BY id")
        return await self.db.fetchall("SELECT * FROM accounts WHERE role=? ORDER BY id", (role,))

    async def get(self, id_: str) -> Row | None:
        return await self.db.fetchone("SELECT * FROM accounts WHERE id=?", (id_,))


class CredentialRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def put_encrypted(self, account_id: str, blob: bytes) -> None:
        await self.db.execute(
            """
            INSERT INTO credentials(account_id, encrypted, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
              encrypted=excluded.encrypted, updated_at=excluded.updated_at
            """,
            (account_id, blob, _now_iso()),
        )
        await self.db.conn.commit()

    async def get(self, account_id: str) -> bytes:
        row = await self.db.fetchone(
            "SELECT encrypted FROM credentials WHERE account_id=?", (account_id,)
        )
        if row is None:
            raise KeyError(f"no credentials for account {account_id!r}")
        return bytes(row["encrypted"])


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_policy(self, account_id: str) -> RiskPolicy | None:
        row = await self.db.fetchone(
            "SELECT * FROM copy_settings WHERE account_id=?", (account_id,)
        )
        if row is None:
            return None
        return RiskPolicy(
            risk_percent=Decimal(row["risk_percent"]),
            max_leverage=int(row["max_leverage"]),
            max_entry_deviation_pct=Decimal(row["max_entry_deviation_pct"]),
            enabled=bool(row["enabled"]),
            allow_new_entries=bool(row["allow_new_entries"]),
        )

    async def upsert_policy(
        self, policy: RiskPolicy, account_id: str, risk_base: str = "available_balance"
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO copy_settings(account_id, risk_percent, max_leverage,
                                      max_entry_deviation_pct, risk_base, enabled,
                                      allow_new_entries)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
              risk_percent=excluded.risk_percent, max_leverage=excluded.max_leverage,
              max_entry_deviation_pct=excluded.max_entry_deviation_pct,
              risk_base=excluded.risk_base, enabled=excluded.enabled,
              allow_new_entries=excluded.allow_new_entries
            """,
            (
                account_id,
                str(policy.risk_percent),
                policy.max_leverage,
                str(policy.max_entry_deviation_pct),
                risk_base,
                int(policy.enabled),
                int(policy.allow_new_entries),
            ),
        )
        await self.db.conn.commit()

    async def get_flag(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM app_state WHERE key=?", (key,))
        return None if row is None else str(row["value"])

    async def set_flag(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO app_state(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.conn.commit()


class MasterEventRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self, event_type: str, event_time: int, raw_json: str, dedup_key: str
    ) -> int | None:
        """Returns the new row id, or None when dedup_key already exists."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO master_events(event_type, event_time, received_at, raw,"
            " dedup_key) VALUES (?, ?, ?, ?, ?)",
            (event_type, event_time, _now_iso(), raw_json, dedup_key),
        )
        await self.db.conn.commit()
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)

    async def mark_processed(self, event_id: int) -> None:
        await self.db.execute(
            "UPDATE master_events SET processed_at=? WHERE id=?", (_now_iso(), event_id)
        )
        await self.db.conn.commit()


class CopyTradeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def open_trade(self, symbol: str, pos_side: str, master_event_id: int | None) -> int:
        cur = await self.db.execute(
            "INSERT INTO copy_trades(symbol, pos_side, opened_at, master_event_id)"
            " VALUES (?, ?, ?, ?)",
            (symbol, pos_side, _now_iso(), master_event_id),
        )
        await self.db.conn.commit()
        return int(cur.lastrowid)

    async def close_trade(self, trade_id: int) -> None:
        await self.db.execute(
            "UPDATE copy_trades SET closed_at=?, status='CLOSED' WHERE trade_id=?",
            (_now_iso(), trade_id),
        )
        await self.db.conn.commit()

    async def active_trade(self, symbol: str, pos_side: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM copy_trades WHERE symbol=? AND pos_side=? AND status='OPEN'"
            " ORDER BY trade_id DESC LIMIT 1",
            (symbol, pos_side),
        )


class OrderLinkRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        trade_id: int | None,
        account_id: str,
        kind: str,
        follower_coid: str,
        master_coid: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO order_links(trade_id, account_id, kind,"
            " master_client_order_id, follower_client_order_id) VALUES (?, ?, ?, ?, ?)",
            (trade_id, account_id, kind, master_coid, follower_coid),
        )
        await self.db.conn.commit()

    async def by_follower_coid(self, account_id: str, coid: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM order_links WHERE account_id=? AND follower_client_order_id=?",
            (account_id, coid),
        )

    async def by_trade(self, trade_id: int) -> list[Row]:
        return await self.db.fetchall(
            "SELECT * FROM order_links WHERE trade_id=? ORDER BY id", (trade_id,)
        )


class CommandRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert(
        self,
        command_key: str,
        account_id: str,
        trade_id: int | None,
        kind: str,
        payload_json: str,
        priority: int,
        client_order_id: str | None,
    ) -> None:
        now = _now_iso()
        await self.db.execute(
            "INSERT INTO execution_commands(command_key, account_id, trade_id, kind, payload,"
            " priority, status, attempts, client_order_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)",
            (
                command_key,
                account_id,
                trade_id,
                kind,
                payload_json,
                priority,
                client_order_id,
                now,
                now,
            ),
        )
        await self.db.conn.commit()

    async def set_status(
        self,
        command_key: str,
        status: str,
        last_error: str | None = None,
        bump_attempts: bool = False,
        client_order_id: str | None = None,
    ) -> None:
        sets = ["status=?", "updated_at=?", "last_error=?"]
        params: list[Any] = [status, _now_iso(), last_error]
        if bump_attempts:
            sets.append("attempts=attempts+1")
        if client_order_id is not None:
            sets.append("client_order_id=?")
            params.append(client_order_id)
        params.append(command_key)
        await self.db.execute(
            f"UPDATE execution_commands SET {', '.join(sets)} WHERE command_key=?", tuple(params)
        )
        await self.db.conn.commit()

    async def get(self, command_key: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM execution_commands WHERE command_key=?", (command_key,)
        )

    async def unresolved_unknowns(self) -> list[Row]:
        return await self.db.fetchall(
            "SELECT * FROM execution_commands WHERE status='UNKNOWN' ORDER BY created_at"
        )

    async def recent(self, limit: int) -> list[Row]:
        return await self.db.fetchall(
            "SELECT * FROM execution_commands ORDER BY created_at DESC, command_key LIMIT ?",
            (limit,),
        )


class NonceStore:
    """Persistent strictly-monotonic nonce per signer; commit happens before return.

    The caller MUST only send the HTTP request after next_nonce() returned
    (persist-before-send contract).
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def next_nonce(self, signer_address: str) -> int:
        cur = await self.db.execute(
            "SELECT last_nonce FROM nonce_store WHERE signer_address=?", (signer_address,)
        )
        row = await cur.fetchone()
        last = int(row["last_nonce"]) if row is not None else 0
        candidate = max(last + 1, int(time.time()) * 1_000_000)
        await self.db.execute(
            "INSERT INTO nonce_store(signer_address, last_nonce, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(signer_address) DO UPDATE SET"
            " last_nonce=excluded.last_nonce, updated_at=excluded.updated_at",
            (signer_address, candidate, _now_iso()),
        )
        await self.db.conn.commit()
        return candidate
