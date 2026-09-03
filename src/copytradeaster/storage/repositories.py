"""Async repositories over the SQLite schema. One class per table."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiosqlite import Row

from copytradeaster.credentials import crypto
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _as_iso(value: datetime | str | None) -> str:
    if value is None:
        return _now_iso()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


async def _conn_fetchone(conn: Any, sql: str, params: tuple = ()) -> Any | None:
    """Fetch one row and always close the transaction-local cursor."""
    cur = await conn.execute(sql, params)
    try:
        return await cur.fetchone()
    finally:
        await cur.close()


async def _conn_fetchall(conn: Any, sql: str, params: tuple = ()) -> list[Any]:
    """Fetch all rows and always close the transaction-local cursor."""
    cur = await conn.execute(sql, params)
    try:
        return list(await cur.fetchall())
    finally:
        await cur.close()


async def _conn_execute(conn: Any, sql: str, params: tuple = ()) -> tuple[int, int | None]:
    """Execute one statement and finalize its cursor before returning."""
    cur = await conn.execute(sql, params)
    try:
        return cur.rowcount, cur.lastrowid
    finally:
        await cur.close()


class OwnershipConflictError(sqlite3.IntegrityError):
    """Raised when a Telegram owner/account relation would be stolen."""


async def _require_real_follower(conn: Any, account_id: str) -> None:
    row = await _conn_fetchone(
        conn,
        "SELECT role, execution_mode FROM accounts WHERE id=?",
        (account_id,),
    )
    if (
        row is None
        or str(row["role"]) != "follower"
        or str(row["execution_mode"] or "REAL") != "REAL"
    ):
        raise OwnershipConflictError(f"account {account_id!r} must be an existing REAL follower")


@dataclass(frozen=True, slots=True)
class AgentValidation:
    """Safe, persisted snapshot of the last signed Agent permission check."""

    signer_address: str
    can_read: bool
    can_perp_trade: bool
    can_withdraw: bool
    expired_ms: int
    ip_whitelist: str
    status: str
    checked_at: str
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked_at", _as_iso(self.checked_at))
        object.__setattr__(self, "expired_ms", int(self.expired_ms))
        object.__setattr__(self, "can_read", bool(self.can_read))
        object.__setattr__(self, "can_perp_trade", bool(self.can_perp_trade))
        object.__setattr__(self, "can_withdraw", bool(self.can_withdraw))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AgentValidation:
        whitelist = value.get("ip_whitelist", "")
        if not isinstance(whitelist, str):
            whitelist = json.dumps(whitelist, separators=(",", ":"), sort_keys=True)
        return cls(
            signer_address=str(value["signer_address"]),
            can_read=bool(value["can_read"]),
            can_perp_trade=bool(value["can_perp_trade"]),
            can_withdraw=bool(value["can_withdraw"]),
            expired_ms=int(value["expired_ms"]),
            ip_whitelist=whitelist,
            status=str(value["status"]),
            checked_at=_as_iso(value.get("checked_at")),
            last_error_code=(
                None if value.get("last_error_code") is None else str(value["last_error_code"])
            ),
        )


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

    async def enabled_real(self) -> list[Row]:
        """Return boot/runtime accounts with live REAL credentials expected."""
        return await self.db.fetchall(
            "SELECT * FROM accounts "
            "WHERE COALESCE(execution_mode, 'REAL')='REAL' AND enabled=1 ORDER BY id"
        )

    async def get_builder_snapshot(self, id_: str) -> Row | None:
        """Return the durable Builder attestation snapshot for one account.

        The snapshot is last-known evidence, never proof: callers must not
        treat its presence as a verified Builder authorization.
        """
        return await self.db.fetchone(
            "SELECT identity_source, builder_address, builder_name, builder_max_fee_rate, "
            "builder_agent_expires_at_ms, builder_verified_at "
            "FROM accounts WHERE id=?",
            (id_,),
        )  # type: ignore[return-value]

    async def set_builder_snapshot(
        self,
        id_: str,
        *,
        builder_address: str,
        builder_name: str,
        builder_max_fee_rate: str,
        builder_agent_expires_at_ms: int,
        builder_verified_at: str,
    ) -> None:
        """Persist a freshly verified Builder proof for a builder account."""
        await self.db.execute_write(
            "UPDATE accounts SET identity_source='builder', builder_address=?, builder_name=?, "
            "builder_max_fee_rate=?, builder_agent_expires_at_ms=?, builder_verified_at=? "
            "WHERE id=?",
            (
                builder_address,
                builder_name,
                builder_max_fee_rate,
                int(builder_agent_expires_at_ms),
                builder_verified_at,
                id_,
            ),
        )

    async def set_identity_direct(self, id_: str) -> None:
        """Mark an account direct and atomically drop any Builder proof."""
        await self.db.execute_write(
            "UPDATE accounts SET identity_source='direct', builder_address=NULL, "
            "builder_name=NULL, "
            "builder_max_fee_rate=NULL, builder_agent_expires_at_ms=NULL, builder_verified_at=NULL "
            "WHERE id=?",
            (id_,),
        )

    async def clear_builder_snapshot(self, id_: str) -> None:
        """Invalidate a stale Builder proof without changing the source marker."""
        await self.db.execute_write(
            "UPDATE accounts SET builder_address=NULL, builder_name=NULL, "
            "builder_max_fee_rate=NULL, builder_agent_expires_at_ms=NULL, builder_verified_at=NULL "
            "WHERE id=?",
            (id_,),
        )


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

    async def get_optional(self, account_id: str) -> bytes | None:
        row = await self.db.fetchone(
            "SELECT encrypted FROM credentials WHERE account_id=?", (account_id,)
        )
        return None if row is None else bytes(row["encrypted"])

    async def delete(self, account_id: str) -> bool:
        rowcount, _ = await self.db.execute_write(
            "DELETE FROM credentials WHERE account_id=?", (account_id,)
        )
        return rowcount > 0


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

    async def patch_policy(
        self,
        account_id: str,
        *,
        risk_percent: Decimal | str | int | None = None,
        max_leverage: int | None = None,
        max_entry_deviation_pct: Decimal | str | int | None = None,
        enabled: bool | None = None,
        allow_new_entries: bool | None = None,
        risk_base: str | None = None,
    ) -> RiskPolicy | None:
        """Patch selected policy fields without resetting the other fields.

        The read/validate/update sequence is one transaction so a concurrent
        pause or settings update cannot silently overwrite an unrelated field.
        ``None`` means "leave this field unchanged"; policy values themselves
        are never stored as floats.
        """
        async with self.db.transaction() as conn:
            row = await _conn_fetchone(
                conn, "SELECT * FROM copy_settings WHERE account_id=?", (account_id,)
            )
            if row is None:
                return None

            next_risk = row["risk_percent"] if risk_percent is None else risk_percent
            next_leverage = row["max_leverage"] if max_leverage is None else max_leverage
            next_deviation = (
                row["max_entry_deviation_pct"]
                if max_entry_deviation_pct is None
                else max_entry_deviation_pct
            )
            next_enabled = bool(row["enabled"]) if enabled is None else bool(enabled)
            next_allow = (
                bool(row["allow_new_entries"])
                if allow_new_entries is None
                else bool(allow_new_entries)
            )
            next_risk_base = row["risk_base"] if risk_base is None else str(risk_base)
            policy = RiskPolicy(
                risk_percent=next_risk,
                max_leverage=int(next_leverage),
                max_entry_deviation_pct=next_deviation,
                enabled=next_enabled,
                allow_new_entries=next_allow,
            )
            await _conn_execute(
                conn,
                "UPDATE copy_settings SET risk_percent=?, max_leverage=?, "
                "max_entry_deviation_pct=?, risk_base=?, enabled=?, "
                "allow_new_entries=? WHERE account_id=?",
                (
                    str(policy.risk_percent),
                    policy.max_leverage,
                    str(policy.max_entry_deviation_pct),
                    next_risk_base,
                    int(policy.enabled),
                    int(policy.allow_new_entries),
                    account_id,
                ),
            )
            return policy

    async def patch(self, account_id: str, **changes: Any) -> RiskPolicy | None:
        """Compatibility alias for callers that prefer a generic patch name."""
        return await self.patch_policy(account_id, **changes)

    async def set_allow_new_entries(self, account_id: str, allowed: bool) -> RiskPolicy | None:
        return await self.patch_policy(account_id, allow_new_entries=allowed)

    async def set_enabled(self, account_id: str, enabled: bool) -> RiskPolicy | None:
        return await self.patch_policy(account_id, enabled=enabled)

    async def get_flag(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM app_state WHERE key=?", (key,))
        return None if row is None else str(row["value"])

    async def set_flag(self, key: str, value: str) -> None:
        await self.db.execute_write(
            "INSERT INTO app_state(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        if key == "degraded":
            await self.db.execute_write(
                "INSERT INTO app_state(key, value) VALUES ('degraded_active', 'true')"
                " ON CONFLICT(key) DO UPDATE SET value='true'"
            )
            await self.db.execute_write(
                "INSERT INTO app_state(key, value) VALUES ('degraded_reason', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )

    async def start_runtime(self, runtime_id: str, now_ms: int) -> None:
        await self.set_flag("ready", "false")
        await self.set_flag("runtime_id", runtime_id)
        await self.set_flag("started_at", str(now_ms))
        await self.set_flag("heartbeat_at", str(now_ms))
        await self.set_flag("degraded_active", "false")
        await self.set_flag("degraded_reason", "")

    async def heartbeat(self, runtime_id: str, now_ms: int) -> bool:
        current = await self.get_flag("runtime_id")
        if current != runtime_id:
            return False
        await self.set_flag("heartbeat_at", str(now_ms))
        return True

    async def clear_degraded(self) -> None:
        await self.set_flag("degraded_active", "false")
        await self.set_flag("degraded_reason", "")


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

    async def set_direction(self, trade_id: int, direction: int) -> None:
        if direction not in {-1, 1}:
            raise ValueError("copy trade direction must be -1 or 1")
        await self.db.execute_write(
            "UPDATE copy_trades SET direction=? WHERE trade_id=? AND direction=0",
            (direction, trade_id),
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
        operation_id: str | None = None,
    ) -> None:
        now = _now_iso()
        await self.db.execute_write(
            "INSERT INTO execution_commands(command_key, account_id, trade_id, kind, payload,"
            " priority, status, attempts, client_order_id, operation_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)",
            (
                command_key,
                account_id,
                trade_id,
                kind,
                payload_json,
                priority,
                client_order_id,
                operation_id,
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

    async def confirmed(
        self,
        *,
        since: datetime | str,
        limit: int = 1000,
    ) -> list[Row]:
        """Return terminal confirmed commands for notification recovery.

        ``since`` is mandatory so startup recovery cannot replay the entire
        Stage 1 command history. Only safe routing fields are selected; payload
        and last_error are not needed by the notification layer and must never
        reach Telegram.
        """
        if since is None:
            raise ValueError("confirmed recovery since cutoff is required")
        if limit <= 0:
            return []
        return await self.db.fetchall(
            "SELECT command_key, account_id, kind, status, created_at, updated_at "
            "FROM execution_commands WHERE status='CONFIRMED' AND updated_at>=? "
            "ORDER BY updated_at, command_key LIMIT ?",
            (_as_iso(since), int(limit)),
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


class TelegramUserRepo:
    """Telegram identity and the single-account ownership relation."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        telegram_user_id: int,
        chat_id: int,
        *,
        role: str | None = None,
        account_id: str | None = None,
        notifications_enabled: bool | None = None,
        blocked_at: str | None = None,
        access_enabled: bool | None = None,
    ) -> None:
        now = _now_iso()
        async with self.db.transaction() as conn:
            current = await _conn_fetchone(
                conn,
                "SELECT * FROM telegram_users WHERE telegram_user_id=?",
                (int(telegram_user_id),),
            )
            if role is None:
                role = "USER" if current is None else str(current["role"])
            if role not in {"USER", "ADMIN"}:
                raise ValueError(f"unsupported Telegram role: {role!r}")
            current_account = None if current is None else current["account_id"]
            if current_account is not None and current_account != account_id:
                if account_id is None:
                    account_id = str(current_account)
                else:
                    old_account = await _conn_fetchone(
                        conn,
                        "SELECT enabled FROM accounts WHERE id=?",
                        (current_account,),
                    )
                    if old_account is None or int(old_account["enabled"]):
                        raise OwnershipConflictError(
                            f"Telegram user {telegram_user_id} already owns another account"
                        )

            if account_id is not None:
                await _require_real_follower(conn, account_id)
                owner = await _conn_fetchone(
                    conn,
                    "SELECT telegram_user_id, role FROM telegram_users WHERE account_id=?",
                    (account_id,),
                )
                if owner is not None and int(owner["telegram_user_id"]) != int(telegram_user_id):
                    raise OwnershipConflictError(
                        f"account {account_id!r} already has another Telegram owner"
                    )
                # Administrator authority comes from the configured Telegram
                # ID allow-list, not from an exclusive database role.  The
                # project owner may administer the runtime and own one normal
                # follower account from the same private chat.

            if current is None:
                await _conn_execute(
                    conn,
                    "INSERT INTO telegram_users(telegram_user_id, chat_id, account_id, role, "
                    "notifications_enabled, blocked_at, access_enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(telegram_user_id),
                        int(chat_id),
                        account_id,
                        role,
                        int(True if notifications_enabled is None else notifications_enabled),
                        blocked_at,
                        int(True if access_enabled is None else access_enabled),
                        now,
                        now,
                    ),
                )
                return

            next_notifications = (
                bool(current["notifications_enabled"])
                if notifications_enabled is None
                else bool(notifications_enabled)
            )
            next_blocked_at = current["blocked_at"] if blocked_at is None else blocked_at
            next_access = (
                bool(current["access_enabled"]) if access_enabled is None else bool(access_enabled)
            )
            await _conn_execute(
                conn,
                "UPDATE telegram_users SET chat_id=?, account_id=?, role=?, "
                "notifications_enabled=?, blocked_at=?, access_enabled=?, updated_at=? "
                "WHERE telegram_user_id=?",
                (
                    int(chat_id),
                    account_id,
                    role,
                    int(next_notifications),
                    next_blocked_at,
                    int(next_access),
                    now,
                    int(telegram_user_id),
                ),
            )

    async def get(self, telegram_user_id: int) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM telegram_users WHERE telegram_user_id=?",
            (int(telegram_user_id),),
        )

    async def get_all(self, *, limit: int = 11, offset: int = 0) -> list[Row]:
        """Return a bounded safe admin page ordered by immutable Telegram ID."""
        if isinstance(limit, bool) or not 1 <= int(limit) <= 101:
            raise ValueError("Telegram user page limit must be between 1 and 101")
        if isinstance(offset, bool) or int(offset) < 0:
            raise ValueError("Telegram user page offset must be non-negative")
        return await self.db.fetchall(
            "SELECT telegram_user_id, chat_id, account_id, role, "
            "notifications_enabled, blocked_at, access_enabled, created_at, updated_at "
            "FROM telegram_users ORDER BY telegram_user_id LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )

    async def by_account(self, account_id: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM telegram_users WHERE account_id=?", (account_id,)
        )

    async def set_account(self, telegram_user_id: int, account_id: str | None) -> bool:
        now = _now_iso()
        async with self.db.transaction() as conn:
            current = await _conn_fetchone(
                conn,
                "SELECT account_id, role FROM telegram_users WHERE telegram_user_id=?",
                (int(telegram_user_id),),
            )
            if current is None:
                raise KeyError(f"unknown Telegram user {telegram_user_id!r}")
            if account_id is not None:
                await _require_real_follower(conn, account_id)
                owner = await _conn_fetchone(
                    conn,
                    "SELECT telegram_user_id FROM telegram_users WHERE account_id=?",
                    (account_id,),
                )
                if owner is not None and int(owner["telegram_user_id"]) != int(telegram_user_id):
                    raise OwnershipConflictError(
                        f"account {account_id!r} already has another Telegram owner"
                    )
            if current["account_id"] not in (None, account_id):
                old_account = await _conn_fetchone(
                    conn,
                    "SELECT enabled FROM accounts WHERE id=?",
                    (current["account_id"],),
                )
                if old_account is None or int(old_account["enabled"]):
                    raise OwnershipConflictError(
                        f"Telegram user {telegram_user_id} already owns another account"
                    )
            rowcount, _ = await _conn_execute(
                conn,
                "UPDATE telegram_users SET account_id=?, updated_at=? WHERE telegram_user_id=?",
                (account_id, now, int(telegram_user_id)),
            )
            return rowcount == 1

    async def clear_account(self, telegram_user_id: int, account_id: str | None = None) -> bool:
        now = _now_iso()
        async with self.db.transaction() as conn:
            if account_id is None:
                sql = (
                    "UPDATE telegram_users SET account_id=NULL, updated_at=? "
                    "WHERE telegram_user_id=?"
                )
                params = (now, int(telegram_user_id))
            else:
                sql = (
                    "UPDATE telegram_users SET account_id=NULL, updated_at=? "
                    "WHERE telegram_user_id=? AND account_id=?"
                )
                params = (now, int(telegram_user_id), account_id)
            rowcount, _ = await _conn_execute(conn, sql, params)
            return rowcount == 1

    async def set_notifications_enabled(self, telegram_user_id: int, enabled: bool) -> bool:
        blocked_at = None if enabled else _now_iso()
        rowcount, _ = await self.db.execute_write(
            "UPDATE telegram_users SET notifications_enabled=?, updated_at=?, "
            "blocked_at=? WHERE telegram_user_id=?",
            (int(enabled), _now_iso(), blocked_at, int(telegram_user_id)),
        )
        return rowcount == 1

    async def set_access_enabled(self, telegram_user_id: int, enabled: bool) -> bool:
        """Grant or revoke Bot UI access without changing trading or notifications."""
        rowcount, _ = await self.db.execute_write(
            "UPDATE telegram_users SET access_enabled=?, updated_at=? WHERE telegram_user_id=?",
            (int(enabled), _now_iso(), int(telegram_user_id)),
        )
        return rowcount == 1

    async def disable_notifications(self, telegram_user_id: int) -> bool:
        """Disable one recipient after a terminal Telegram 403."""
        now = _now_iso()
        rowcount, _ = await self.db.execute_write(
            "UPDATE telegram_users SET notifications_enabled=0, "
            "blocked_at=COALESCE(blocked_at, ?), updated_at=? "
            "WHERE telegram_user_id=?",
            (now, now, int(telegram_user_id)),
        )
        return rowcount == 1

    async def notification_recipients(
        self,
        *,
        telegram_user_ids: Iterable[int] | None = None,
        role: str | None = None,
    ) -> list[Row]:
        """Return currently deliverable chat destinations."""
        clauses = ["notifications_enabled=1", "blocked_at IS NULL"]
        params: list[Any] = []
        if role is not None:
            clauses.append("role=?")
            params.append(role)
        if telegram_user_ids is not None:
            ids = [int(value) for value in telegram_user_ids]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"telegram_user_id IN ({placeholders})")
            params.extend(ids)
        return await self.db.fetchall(
            "SELECT * FROM telegram_users WHERE " + " AND ".join(clauses) + " "
            "ORDER BY telegram_user_id",
            tuple(params),
        )


class TelegramAccessRequestRepo:
    """Durable, auditable Telegram menu-access requests."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, request_id: int) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM telegram_access_requests WHERE id=?",
            (int(request_id),),
        )

    async def latest_for_user(self, telegram_user_id: int) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM telegram_access_requests WHERE telegram_user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (int(telegram_user_id),),
        )

    async def create_pending(
        self,
        telegram_user_id: int,
        chat_id: int,
        *,
        username: str | None,
        display_name: str | None,
    ) -> Row:
        """Create one pending request, or return the already-pending request.

        ``create_pending_result`` also exposes whether this call performed the
        insert.  Keep this small compatibility wrapper for callers that only
        need the durable row.
        """
        row, _ = await self.create_pending_result(
            telegram_user_id,
            chat_id,
            username=username,
            display_name=display_name,
        )
        return row

    async def create_pending_result(
        self,
        telegram_user_id: int,
        chat_id: int,
        *,
        username: str | None,
        display_name: str | None,
    ) -> tuple[Row, bool]:
        """Insert one pending request and report whether this call won.

        The decision is made inside the same ``BEGIN IMMEDIATE`` transaction
        as the uniqueness check.  This lets the Telegram boundary suppress a
        duplicate direct admin card without relying on process-local FSM
        isolation.
        """
        now = _now_iso()
        async with self.db.transaction() as conn:
            current = await _conn_fetchone(
                conn,
                "SELECT * FROM telegram_access_requests "
                "WHERE telegram_user_id=? AND status='PENDING' ORDER BY id DESC LIMIT 1",
                (int(telegram_user_id),),
            )
            if current is not None:
                return current, False
            _, request_id = await _conn_execute(
                conn,
                "INSERT INTO telegram_access_requests(telegram_user_id, chat_id, username, "
                "display_name, status, requested_at) VALUES (?, ?, ?, ?, 'PENDING', ?)",
                (
                    int(telegram_user_id),
                    int(chat_id),
                    username,
                    display_name,
                    now,
                ),
            )
            if request_id is None:
                raise RuntimeError("access request insert did not return an ID")
            created = await _conn_fetchone(
                conn,
                "SELECT * FROM telegram_access_requests WHERE id=?",
                (int(request_id),),
            )
            if created is None:
                raise RuntimeError("access request disappeared after insert")
            return created, True

    async def pending(self, *, limit: int = 11, offset: int = 0) -> list[Row]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 101:
            raise ValueError("access request page limit must be between 1 and 101")
        if isinstance(offset, bool) or int(offset) < 0:
            raise ValueError("access request page offset must be non-negative")
        return await self.db.fetchall(
            "SELECT * FROM telegram_access_requests WHERE status='PENDING' "
            "ORDER BY id LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )

    async def pending_count(self) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM telegram_access_requests WHERE status='PENDING'"
        )
        return 0 if row is None else int(row["count"])

    async def decide(
        self, request_id: int, admin_user_id: int, *, approve: bool
    ) -> tuple[Row, bool]:
        """Resolve a request once; approval and menu-access grant are atomic."""
        now = _now_iso()
        async with self.db.transaction() as conn:
            request = await _conn_fetchone(
                conn,
                "SELECT * FROM telegram_access_requests WHERE id=?",
                (int(request_id),),
            )
            if request is None:
                raise KeyError(f"unknown Telegram access request {request_id!r}")
            if str(request["status"]) != "PENDING":
                return request, False

            if approve:
                target = int(request["telegram_user_id"])
                chat_id = int(request["chat_id"])
                current_user = await _conn_fetchone(
                    conn,
                    "SELECT * FROM telegram_users WHERE telegram_user_id=?",
                    (target,),
                )
                chat_owner = await _conn_fetchone(
                    conn,
                    "SELECT telegram_user_id FROM telegram_users WHERE chat_id=?",
                    (chat_id,),
                )
                if chat_owner is not None and int(chat_owner["telegram_user_id"]) != target:
                    raise OwnershipConflictError("private Telegram chat already has another owner")
                if current_user is None:
                    await _conn_execute(
                        conn,
                        "INSERT INTO telegram_users(telegram_user_id, chat_id, account_id, role, "
                        "notifications_enabled, blocked_at, access_enabled, created_at, "
                        "updated_at) "
                        "VALUES (?, ?, NULL, 'USER', 1, NULL, 1, ?, ?)",
                        (target, chat_id, now, now),
                    )
                else:
                    if int(current_user["chat_id"]) != chat_id:
                        raise OwnershipConflictError(
                            "Telegram access request does not match the stored private chat"
                        )
                    await _conn_execute(
                        conn,
                        "UPDATE telegram_users SET access_enabled=1, updated_at=? "
                        "WHERE telegram_user_id=?",
                        (now, target),
                    )

            status = "APPROVED" if approve else "REJECTED"
            await _conn_execute(
                conn,
                "UPDATE telegram_access_requests SET status=?, decided_at=?, decided_by=? "
                "WHERE id=? AND status='PENDING'",
                (status, now, int(admin_user_id), int(request_id)),
            )
            decided = await _conn_fetchone(
                conn,
                "SELECT * FROM telegram_access_requests WHERE id=?",
                (int(request_id),),
            )
            if decided is None:
                raise RuntimeError("access request disappeared after decision")
            return decided, True


class AgentValidationRepo:
    """Repository for the latest safe Agent permission snapshot."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        account_id: str,
        validation: AgentValidation | Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        if validation is None:
            validation = fields
        elif fields:
            raise TypeError("pass validation either as a record or as keyword fields")
        record = (
            validation
            if isinstance(validation, AgentValidation)
            else AgentValidation.from_mapping(validation)
        )
        await self.db.execute_write(
            "INSERT INTO agent_validations(account_id, signer_address, can_read, "
            "can_perp_trade, can_withdraw, expired_ms, ip_whitelist, status, checked_at, "
            "last_error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET signer_address=excluded.signer_address, "
            "can_read=excluded.can_read, can_perp_trade=excluded.can_perp_trade, "
            "can_withdraw=excluded.can_withdraw, expired_ms=excluded.expired_ms, "
            "ip_whitelist=excluded.ip_whitelist, status=excluded.status, "
            "checked_at=excluded.checked_at, last_error_code=excluded.last_error_code",
            (
                account_id,
                record.signer_address,
                int(record.can_read),
                int(record.can_perp_trade),
                int(record.can_withdraw),
                record.expired_ms,
                record.ip_whitelist,
                record.status,
                record.checked_at,
                record.last_error_code,
            ),
        )

    async def get(self, account_id: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT * FROM agent_validations WHERE account_id=?", (account_id,)
        )

    async def delete(self, account_id: str) -> bool:
        rowcount, _ = await self.db.execute_write(
            "DELETE FROM agent_validations WHERE account_id=?", (account_id,)
        )
        return rowcount == 1


class NotificationOutboxRepo:
    """Atomic enqueue and worker lease/retry primitives for Telegram events."""

    _SELECT_COLUMNS = "SELECT * FROM notification_outbox"

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _event_values(event: Mapping[str, Any]) -> tuple[Any, ...]:
        required = ("event_key", "telegram_user_id", "chat_id", "kind", "severity", "text")
        missing = [key for key in required if key not in event]
        if missing:
            raise ValueError(f"missing outbox fields: {', '.join(missing)}")
        severity = str(event["severity"])
        if severity not in {"INFO", "WARN", "CRITICAL"}:
            raise ValueError(f"unsupported outbox severity: {severity!r}")
        return (
            str(event["event_key"]),
            int(event["telegram_user_id"]),
            int(event["chat_id"]),
            None if event.get("account_id") is None else str(event["account_id"]),
            str(event["kind"]),
            severity,
            str(event["text"]),
            str(event.get("status", "PENDING")),
            int(event.get("attempts", 0)),
            _as_iso(event.get("available_at")),
            None if event.get("lease_until") is None else _as_iso(event["lease_until"]),
            event.get("claimed_by"),
            event.get("last_error"),
            _as_iso(event.get("created_at")),
            _as_iso(event.get("updated_at")),
            None if event.get("sent_at") is None else _as_iso(event["sent_at"]),
        )

    async def enqueue(
        self,
        event_key: str,
        telegram_user_id: int,
        chat_id: int,
        kind: str,
        severity: str,
        text: str,
        account_id: str | None = None,
        available_at: datetime | str | None = None,
    ) -> int:
        event = {
            "event_key": event_key,
            "telegram_user_id": telegram_user_id,
            "chat_id": chat_id,
            "account_id": account_id,
            "kind": kind,
            "severity": severity,
            "text": text,
            "available_at": available_at,
        }
        values = self._event_values(event)
        async with self.db.transaction() as conn:
            await _conn_execute(
                conn,
                "INSERT INTO notification_outbox(event_key, telegram_user_id, chat_id, account_id, "
                "kind, severity, text, status, attempts, available_at, lease_until, claimed_by, "
                "last_error, created_at, updated_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_key, telegram_user_id) DO NOTHING",
                values,
            )
            row = await _conn_fetchone(
                conn,
                "SELECT id FROM notification_outbox WHERE event_key=? AND telegram_user_id=?",
                (event_key, int(telegram_user_id)),
            )
            if row is None:
                raise RuntimeError("outbox enqueue did not return a row")
            return int(row["id"])

    async def enqueue_many(self, events: Iterable[Mapping[str, Any]]) -> list[int]:
        event_list = list(events)
        if not event_list:
            return []
        async with self.db.transaction() as conn:
            ids: list[int] = []
            for event in event_list:
                values = self._event_values(event)
                await _conn_execute(
                    conn,
                    "INSERT INTO notification_outbox(event_key, telegram_user_id, chat_id, "
                    "account_id, "
                    "kind, severity, text, status, attempts, available_at, lease_until, "
                    "claimed_by, "
                    "last_error, created_at, updated_at, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(event_key, telegram_user_id) DO NOTHING",
                    values,
                )
                row = await _conn_fetchone(
                    conn,
                    "SELECT id FROM notification_outbox WHERE event_key=? AND telegram_user_id=?",
                    (values[0], values[1]),
                )
                if row is None:
                    raise RuntimeError("outbox enqueue did not return a row")
                ids.append(int(row["id"]))
            return ids

    async def get(self, row_id: int) -> Row | None:
        return await self.db.fetchone(f"{self._SELECT_COLUMNS} WHERE id=?", (int(row_id),))

    async def has_event(self, event_key: str) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM notification_outbox WHERE event_key=? LIMIT 1",
            (event_key,),
        )
        return row is not None

    async def counts(self) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS count FROM notification_outbox GROUP BY status"
        )
        counts = {status: 0 for status in ("PENDING", "SENDING", "SENT", "FAILED")}
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        counts["TOTAL"] = sum(counts.values())
        return counts

    async def counts_for_user(self, telegram_user_id: int) -> dict[str, int | str | None]:
        """Return status counts and last delivery time without message content."""
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS count, MAX(sent_at) AS last_sent_at "
            "FROM notification_outbox WHERE telegram_user_id=? GROUP BY status",
            (int(telegram_user_id),),
        )
        summary: dict[str, int | str | None] = {
            status: 0 for status in ("PENDING", "SENDING", "SENT", "FAILED")
        }
        summary["last_sent_at"] = None
        for row in rows:
            status = str(row["status"])
            summary[status] = int(row["count"])
            if status == "SENT":
                summary["last_sent_at"] = row["last_sent_at"]
        summary["TOTAL"] = sum(
            int(summary[status]) for status in ("PENDING", "SENDING", "SENT", "FAILED")
        )
        return summary

    async def recover_expired(self, now: datetime | str | None = None) -> int:
        now_iso = _as_iso(now)
        async with self.db.transaction() as conn:
            rowcount, _ = await _conn_execute(
                conn,
                "UPDATE notification_outbox SET status='PENDING', lease_until=NULL, "
                "claimed_by=NULL, available_at=?, updated_at=? "
                "WHERE status='SENDING' AND lease_until IS NOT NULL AND lease_until<=?",
                (now_iso, now_iso, now_iso),
            )
            return rowcount

    async def claim(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        now: datetime | str | None = None,
        lease_seconds: int = 30,
    ) -> list[Row]:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if limit <= 0:
            return []
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if now is None:
            now_dt = datetime.now(UTC)
        else:
            now_dt = datetime.fromisoformat(str(now))
            now_dt = now_dt.replace(tzinfo=UTC) if now_dt.tzinfo is None else now_dt.astimezone(UTC)
        now_iso = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        async with self.db.transaction() as conn:
            await _conn_execute(
                conn,
                "UPDATE notification_outbox SET status='PENDING', lease_until=NULL, "
                "claimed_by=NULL, available_at=?, updated_at=? "
                "WHERE status='SENDING' AND lease_until IS NOT NULL AND lease_until<=?",
                (now_iso, now_iso, now_iso),
            )
            rows = await _conn_fetchall(
                conn,
                "SELECT id FROM notification_outbox WHERE status='PENDING' "
                "AND available_at<=? ORDER BY id LIMIT ?",
                (now_iso, int(limit)),
            )
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            for row_id in ids:
                await _conn_execute(
                    conn,
                    "UPDATE notification_outbox SET status='SENDING', attempts=attempts+1, "
                    "lease_until=?, claimed_by=?, updated_at=? "
                    "WHERE id=? AND status='PENDING' AND available_at<=?",
                    (lease_until, worker_id, now_iso, row_id, now_iso),
                )
            placeholders = ",".join("?" for _ in ids)
            return await _conn_fetchall(
                conn,
                f"{self._SELECT_COLUMNS} WHERE id IN ({placeholders}) ORDER BY id",
                tuple(ids),
            )

    async def claim_one(
        self,
        worker_id: str,
        *,
        now: datetime | str | None = None,
        lease_seconds: int = 30,
    ) -> Row | None:
        rows = await self.claim(
            worker_id,
            limit=1,
            now=now,
            lease_seconds=lease_seconds,
        )
        return rows[0] if rows else None

    async def mark_sent(self, row_id: int, worker_id: str | None = None) -> bool:
        now = _now_iso()
        sql = (
            "UPDATE notification_outbox SET status='SENT', sent_at=?, lease_until=NULL, "
            "claimed_by=NULL, updated_at=? WHERE id=? AND status='SENDING'"
        )
        params: tuple[Any, ...] = (now, now, int(row_id))
        if worker_id is not None:
            sql += " AND claimed_by=?"
            params += (worker_id,)
        rowcount, _ = await self.db.execute_write(sql, params)
        return rowcount == 1

    async def retry(
        self,
        row_id: int,
        error: str,
        *,
        available_at: datetime | str | None = None,
        max_attempts: int = 3,
        worker_id: str | None = None,
    ) -> bool:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now = _now_iso()
        next_at = _as_iso(available_at)
        async with self.db.transaction() as conn:
            row = await _conn_fetchone(
                conn,
                "SELECT attempts, status, claimed_by FROM notification_outbox WHERE id=?",
                (int(row_id),),
            )
            if row is None or row["status"] != "SENDING":
                return False
            if worker_id is not None and row["claimed_by"] != worker_id:
                return False
            terminal = int(row["attempts"]) >= max_attempts
            if terminal:
                sql = (
                    "UPDATE notification_outbox SET status='FAILED', last_error=?, "
                    "lease_until=NULL, claimed_by=NULL, updated_at=? "
                    "WHERE id=? AND status='SENDING'"
                )
                params: tuple[Any, ...] = (str(error), now, int(row_id))
            else:
                sql = (
                    "UPDATE notification_outbox SET status='PENDING', available_at=?, "
                    "last_error=?, lease_until=NULL, claimed_by=NULL, updated_at=? "
                    "WHERE id=? AND status='SENDING'"
                )
                params = (next_at, str(error), now, int(row_id))
            rowcount, _ = await _conn_execute(conn, sql, params)
            return rowcount == 1

    async def fail(self, row_id: int, error: str, worker_id: str | None = None) -> bool:
        now = _now_iso()
        sql = (
            "UPDATE notification_outbox SET status='FAILED', last_error=?, lease_until=NULL, "
            "claimed_by=NULL, updated_at=? WHERE id=? AND status='SENDING'"
        )
        params: tuple[Any, ...] = (str(error), now, int(row_id))
        if worker_id is not None:
            sql += " AND claimed_by=?"
            params += (worker_id,)
        rowcount, _ = await self.db.execute_write(sql, params)
        return rowcount == 1

    async def pending(self, limit: int = 100) -> list[Row]:
        return await self.db.fetchall(
            f"{self._SELECT_COLUMNS} WHERE status='PENDING' ORDER BY available_at, id LIMIT ?",
            (int(limit),),
        )


@dataclass(slots=True)
class BuilderOnboardingAttempt:
    pending_id: str
    telegram_user_id: int
    account_id: str
    user_address: str
    signer_address: str
    signer_private_key: str = field(repr=False)
    params: dict = field(default_factory=dict)
    state: str = "PREPARED"
    approval_deadline_ms: int = 0
    agent_expires_at_ms: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_error_code: str | None = None


class BuilderOnboardingAttemptsRepo:
    """Encrypted persistent storage for in-flight Builder onboarding attempts."""

    def __init__(self, db: Database, encryption_key: bytes) -> None:
        self.db = db
        self._key = encryption_key

    async def save(self, attempt: BuilderOnboardingAttempt) -> None:
        payload = json.dumps(
            {
                "signer_private_key": attempt.signer_private_key,
                "params": attempt.params,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        encrypted = crypto.encrypt(self._key, payload)
        now = _now_iso()
        created_at = _as_iso(attempt.created_at) if attempt.created_at else now
        updated_at = now
        await self.db.execute_write(
            """
            INSERT INTO builder_onboarding_attempts(
              pending_id, telegram_user_id, account_id, user_address, signer_address,
              encrypted_payload, algo, state, approval_deadline_ms, agent_expires_at_ms,
              created_at, updated_at, last_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, 'AESGCM-v1', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
              pending_id=excluded.pending_id,
              account_id=excluded.account_id,
              user_address=excluded.user_address,
              signer_address=excluded.signer_address,
              encrypted_payload=excluded.encrypted_payload,
              algo=excluded.algo,
              state=excluded.state,
              approval_deadline_ms=excluded.approval_deadline_ms,
              agent_expires_at_ms=excluded.agent_expires_at_ms,
              updated_at=excluded.updated_at,
              last_error_code=excluded.last_error_code
            """,
            (
                attempt.pending_id,
                int(attempt.telegram_user_id),
                attempt.account_id,
                attempt.user_address,
                attempt.signer_address,
                encrypted,
                attempt.state,
                int(attempt.approval_deadline_ms),
                int(attempt.agent_expires_at_ms),
                created_at,
                updated_at,
                attempt.last_error_code,
            ),
        )

    def _row_to_attempt(self, row: Any) -> BuilderOnboardingAttempt:
        decrypted_bytes = crypto.decrypt(self._key, bytes(row["encrypted_payload"]))
        payload = json.loads(decrypted_bytes.decode("utf-8"))
        return BuilderOnboardingAttempt(
            pending_id=str(row["pending_id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            account_id=str(row["account_id"]),
            user_address=str(row["user_address"]),
            signer_address=str(row["signer_address"]),
            signer_private_key=str(payload["signer_private_key"]),
            params=dict(payload["params"]),
            state=str(row["state"]),
            approval_deadline_ms=int(row["approval_deadline_ms"]),
            agent_expires_at_ms=int(row["agent_expires_at_ms"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_error_code=(
                str(row["last_error_code"]) if row["last_error_code"] is not None else None
            ),
        )

    async def get_by_user_id(self, telegram_user_id: int) -> BuilderOnboardingAttempt | None:
        row = await self.db.fetchone(
            "SELECT * FROM builder_onboarding_attempts WHERE telegram_user_id=?",
            (int(telegram_user_id),),
        )
        if row is None:
            return None
        return self._row_to_attempt(row)

    async def get_by_pending_id(self, pending_id: str) -> BuilderOnboardingAttempt | None:
        row = await self.db.fetchone(
            "SELECT * FROM builder_onboarding_attempts WHERE pending_id=?",
            (str(pending_id),),
        )
        if row is None:
            return None
        return self._row_to_attempt(row)

    async def update_state(
        self,
        pending_id: str,
        state: str,
        *,
        last_error_code: str | None = None,
    ) -> bool:
        now = _now_iso()
        query = (
            "UPDATE builder_onboarding_attempts "
            "SET state=?, updated_at=?, last_error_code=? WHERE pending_id=?"
        )
        rowcount, _ = await self.db.execute_write(
            query,
            (state, now, last_error_code, str(pending_id)),
        )
        return rowcount > 0

    async def delete(self, telegram_user_id: int) -> bool:
        rowcount, _ = await self.db.execute_write(
            "DELETE FROM builder_onboarding_attempts WHERE telegram_user_id=?",
            (int(telegram_user_id),),
        )
        return rowcount > 0

    async def delete_by_pending_id(self, pending_id: str) -> bool:
        rowcount, _ = await self.db.execute_write(
            "DELETE FROM builder_onboarding_attempts WHERE pending_id=?",
            (str(pending_id),),
        )
        return rowcount > 0

    async def cleanup_expired_prepared(self, now_ms: int) -> int:
        query = (
            "DELETE FROM builder_onboarding_attempts "
            "WHERE state='PREPARED' AND approval_deadline_ms < ?"
        )
        rowcount, _ = await self.db.execute_write(query, (int(now_ms),))
        return rowcount
