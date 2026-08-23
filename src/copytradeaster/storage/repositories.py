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

    async def upsert(
        self,
        id_: str,
        role: str,
        label: str,
        user_address: str,
        execution_mode: str = "REAL",
    ) -> None:
        if execution_mode not in {"REAL", "SHADOW"}:
            raise ValueError(f"unsupported execution mode: {execution_mode!r}")
        await self.db.execute_write(
            """
            INSERT INTO accounts(id, role, label, user_address, execution_mode)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              role=excluded.role, label=excluded.label, user_address=excluded.user_address,
              execution_mode=excluded.execution_mode
            """,
            (id_, role, label, user_address, execution_mode),
        )

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
        await self.db.execute_write(
            """
            INSERT INTO credentials(account_id, encrypted, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
              encrypted=excluded.encrypted, updated_at=excluded.updated_at
            """,
            (account_id, blob, _now_iso()),
        )

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
        await self.db.execute_write(
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

    async def get_flag(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM app_state WHERE key=?", (key,))
        return None if row is None else str(row["value"])

    async def set_flag(self, key: str, value: str) -> None:
        await self.db.execute_write(
            "INSERT INTO app_state(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


class MasterEventRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self, event_type: str, event_time: int, raw_json: str, dedup_key: str
    ) -> int | None:
        """Returns the new row id, or None when dedup_key already exists."""
        rowcount, lastrowid = await self.db.execute_write(
            "INSERT OR IGNORE INTO master_events(event_type, event_time, received_at, raw,"
            " dedup_key) VALUES (?, ?, ?, ?, ?)",
            (event_type, event_time, _now_iso(), raw_json, dedup_key),
        )
        if rowcount == 0:
            return None
        return int(lastrowid)

    async def mark_processed(self, event_id: int) -> None:
        await self.db.execute_write(
            "UPDATE master_events SET processed_at=? WHERE id=?", (_now_iso(), event_id)
        )


class CopyTradeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def open_trade(
        self, symbol: str, pos_side: str, master_event_id: int | None, direction: int = 0
    ) -> int:
        _, lastrowid = await self.db.execute_write(
            "INSERT INTO copy_trades(symbol, pos_side, direction, opened_at, master_event_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (symbol, pos_side, int(direction), _now_iso(), master_event_id),
        )
        # A NEW master episode invalidates per-follower intentional-flat
        # markers for this leg: followers may enter normally again.
        await self.db.execute_write(
            "UPDATE sync_basis SET zero_target=0, updated_at=?"
            " WHERE symbol=? AND pos_side=? AND zero_target<>0",
            (_now_iso(), symbol, pos_side),
        )
        return int(lastrowid)

    async def close_trade(self, trade_id: int) -> None:
        await self.db.execute_write(
            "UPDATE copy_trades SET closed_at=?, status='CLOSED' WHERE trade_id=?",
            (_now_iso(), trade_id),
        )

    async def active_trade(self, symbol: str, pos_side: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM copy_trades WHERE symbol=? AND pos_side=? AND status='OPEN'"
            " ORDER BY trade_id DESC LIMIT 1",
            (symbol, pos_side),
        )

    async def latest_trade(self, symbol: str, pos_side: str) -> Row | None:
        """Newest episode incl. CLOSED - attribution for late close actions."""
        return await self.db.fetchone(
            "SELECT * FROM copy_trades WHERE symbol=? AND pos_side=?"
            " ORDER BY trade_id DESC LIMIT 1",
            (symbol, pos_side),
        )

    async def all_open(self) -> list[Row]:
        return await self.db.fetchall(
            "SELECT * FROM copy_trades WHERE status='OPEN' ORDER BY trade_id"
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
        await self.db.execute_write(
            "INSERT OR IGNORE INTO order_links(trade_id, account_id, kind,"
            " master_client_order_id, follower_client_order_id) VALUES (?, ?, ?, ?, ?)",
            (trade_id, account_id, kind, master_coid, follower_coid),
        )

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
        await self.db.execute_write(
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
        await self.db.execute_write(
            f"UPDATE execution_commands SET {', '.join(sets)} WHERE command_key=?", tuple(params)
        )

    async def get(self, command_key: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM execution_commands WHERE command_key=?", (command_key,)
        )

    async def in_flight(self) -> list[Row]:
        """Commands whose exchange outcome is not settled: UNKNOWN/SENDING/SUBMITTED."""
        return await self.db.fetchall(
            "SELECT * FROM execution_commands"
            " WHERE status IN ('UNKNOWN','SENDING','SUBMITTED') ORDER BY created_at"
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
        """Allocate a nonce atomically on the application DB connection."""
        floor = int(time.time()) * 1_000_000
        async with self.db.write_lock:
            cur = await self.db.execute(
                "INSERT INTO nonce_store(signer_address, last_nonce, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(signer_address) DO UPDATE SET"
                " last_nonce=CASE WHEN nonce_store.last_nonce + 1 > excluded.last_nonce"
                " THEN nonce_store.last_nonce + 1 ELSE excluded.last_nonce END,"
                " updated_at=excluded.updated_at"
                " RETURNING last_nonce",
                (signer_address, floor, _now_iso()),
            )
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
            if self.db.conn is None:
                raise RuntimeError("Database.connect() must be called first")
            await self.db.conn.commit()
        if row is None:
            raise RuntimeError("nonce allocation returned no row")
        return int(row[0])


class SyncBasisRepo:
    """Persisted per-(account, symbol, leg) sizing basis for state reconciliation.

    master_ref_abs: the master magnitude the follower position was last brought
    into sync with. Survives restarts, so a master partial close missed while
    offline still produces scaled_reduce(current, ref, new) on the first sweep.
    revision: bumped whenever a NEW desired target is issued for the key;
    feeds command_key idempotency (transition identity, not payload identity).
    issued_target: canonical JSON of the last issued TargetPosition.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def all_for_account(self, account_id: str) -> dict[str, dict]:
        rows = await self.db.fetchall("SELECT * FROM sync_basis WHERE account_id=?", (account_id,))
        return {
            f"{r['symbol']}:{r['pos_side']}": {
                "master_ref_abs": Decimal(str(r["master_ref_abs"])),
                "revision": int(r["revision"]),
                "issued_target": r["issued_target"],
            }
            for r in rows
        }

    async def get(self, account_id: str, symbol: str, pos_side: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM sync_basis WHERE account_id=? AND symbol=? AND pos_side=?",
            (account_id, symbol, pos_side),
        )

    async def upsert_ref(
        self, account_id: str, symbol: str, pos_side: str, ref_abs: Decimal
    ) -> None:
        await self.db.execute_write(
            "INSERT INTO sync_basis(account_id, symbol, pos_side, master_ref_abs, revision,"
            " updated_at) VALUES (?, ?, ?, ?, 0, ?)"
            " ON CONFLICT(account_id, symbol, pos_side) DO UPDATE SET"
            " master_ref_abs=excluded.master_ref_abs, updated_at=excluded.updated_at",
            (account_id, symbol, pos_side, str(ref_abs), _now_iso()),
        )

    async def bump_revision(self, account_id: str, symbol: str, pos_side: str, issued: str) -> int:
        """Register `issued` as the current desired state; returns the new revision."""
        row = await self.get(account_id, symbol, pos_side)
        revision = (int(row["revision"]) + 1) if row is not None else 1
        await self.db.execute_write(
            "INSERT INTO sync_basis(account_id, symbol, pos_side, master_ref_abs, revision,"
            " issued_target, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)"
            " ON CONFLICT(account_id, symbol, pos_side) DO UPDATE SET"
            " revision=excluded.revision, issued_target=excluded.issued_target,"
            " updated_at=excluded.updated_at",
            (account_id, symbol, pos_side, revision, issued, _now_iso()),
        )
        return revision

    async def clear(self, account_id: str, symbol: str, pos_side: str) -> None:
        await self.db.execute_write(
            "DELETE FROM sync_basis WHERE account_id=? AND symbol=? AND pos_side=?",
            (account_id, symbol, pos_side),
        )

    async def mark_zero_target(
        self, account_id: str, symbol: str, pos_side: str, *, zero: bool = True
    ) -> None:
        """Persist that the CURRENT desired target for an active master episode
        is zero (e.g. a proportional reduction rounded down to dust).

        The row itself must survive while the master episode stays open:
        deleting it (the old behavior) made the very next recompute see a
        non-zero master vs a flat follower and re-open the position the engine
        had just closed on purpose.
        """
        await self.db.execute_write(
            "INSERT INTO sync_basis(account_id, symbol, pos_side, master_ref_abs,"
            " revision, issued_target, zero_target, updated_at)"
            " VALUES (?, ?, ?, 0, 0, NULL, ?, ?)"
            " ON CONFLICT(account_id, symbol, pos_side) DO UPDATE SET"
            " issued_target=NULL, zero_target=excluded.zero_target,"
            " updated_at=excluded.updated_at",
            (account_id, symbol, pos_side, int(zero), _now_iso()),
        )

    async def is_zero_target(self, account_id: str, symbol: str, pos_side: str) -> bool:
        row = await self.get(account_id, symbol, pos_side)
        return row is not None and bool(row["zero_target"])

    async def clear_zero_target(self, account_id: str, symbol: str, pos_side: str) -> None:
        """Drop the intentional-flat marker without touching revision/ref state."""
        await self.db.execute_write(
            "UPDATE sync_basis SET zero_target=0, updated_at=?"
            " WHERE account_id=? AND symbol=? AND pos_side=?",
            (_now_iso(), account_id, symbol, pos_side),
        )
