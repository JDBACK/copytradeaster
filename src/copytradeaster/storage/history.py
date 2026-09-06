"""Atomic follower facts and import coverage, isolated by account/environment.

Canonical history is retained without TTL. Repeated identical facts are no-ops;
a conflicting exchange identity aborts the batch instead of silently changing
financial history. Projection and user events will share this transaction path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import aiosqlite

from copytradeaster.aster.models import IncomeDTO, OrderDTO, UserTradeDTO
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database


class HistoryConflictError(ValueError):
    """The same exchange identity arrived with different canonical facts."""


@dataclass(frozen=True, slots=True)
class HistoryScope:
    account_id: str
    environment: str

    def __post_init__(self) -> None:
        if not self.account_id or self.environment not in {"testnet", "mainnet", "shadow"}:
            raise ValueError("invalid history scope")


class HistoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def commit_fills(
        self,
        scope: HistoryScope,
        symbol: str,
        window: Window,
        result: WindowRead[UserTradeDTO],
        *,
        checked_at: int,
    ) -> None:
        if not symbol or any(row.symbol != symbol for row in result.rows):
            raise ValueError("fill symbol mismatch")
        self._validate_result(window, result)
        async with self._db.transaction() as conn:
            batch = []
            for row in result.rows:
                values = (
                    scope.account_id,
                    scope.environment,
                    symbol,
                    str(row.trade_id),
                    str(row.order_id),
                    row.side.value,
                    row.position_side.value,
                    _decimal_text(row.qty),
                    _decimal_text(row.price),
                    _decimal_text(row.quote_qty),
                    _decimal_text(row.realized_pnl),
                    _decimal_text(row.commission),
                    row.commission_asset,
                    row.time,
                )
                batch.append(values)
            await _insert_facts(
                conn,
                "follower_fills",
                ("account_id", "environment", "symbol", "exchange_trade_id"),
                batch,
            )
            await self._commit_windows(conn, scope, "fills", symbol, window, result, checked_at)

    async def commit_income(
        self,
        scope: HistoryScope,
        window: Window,
        result: WindowRead[IncomeDTO],
        *,
        checked_at: int,
    ) -> None:
        self._validate_result(window, result)
        async with self._db.transaction() as conn:
            batch = []
            for row in result.rows:
                values = (
                    scope.account_id,
                    scope.environment,
                    row.income_type,
                    row.tran_id,
                    row.symbol,
                    row.asset,
                    _decimal_text(row.income),
                    row.time,
                    row.trade_id,
                )
                batch.append(values)
            await _insert_facts(
                conn,
                "follower_cash_flows",
                ("account_id", "environment", "income_type", "tran_id"),
                batch,
            )
            await self._commit_windows(conn, scope, "income", "", window, result, checked_at)

    @staticmethod
    def _validate_result(window: Window, result: WindowRead) -> None:
        parts = sorted(result.complete + result.pending + result.saturated, key=lambda w: w.start)
        next_start = window.start
        for part in parts:
            if part.start != next_start or part.end > window.end:
                raise ValueError("coverage must partition requested window")
            next_start = part.end + 1
        if next_start != window.end + 1:
            raise ValueError("coverage must partition requested window")
        known = result.complete + result.saturated
        if any(not any(w.start <= row.time <= w.end for w in known) for row in result.rows):
            raise ValueError("facts outside verified windows")

    @staticmethod
    async def _commit_windows(
        conn: aiosqlite.Connection,
        scope: HistoryScope,
        stream: str,
        symbol: str,
        window: Window,
        result: WindowRead,
        checked_at: int,
    ) -> None:
        # Remove exactly the work item being split; overlapping refresh evidence
        # is kept independently and cannot erase an older verified interval.
        cursor = await conn.execute(
            "DELETE FROM history_sync_state WHERE account_id=? AND environment=? "
            "AND stream=? AND symbol=? AND start_ms=? AND end_ms=?",
            (scope.account_id, scope.environment, stream, symbol, window.start, window.end),
        )
        await cursor.close()
        for state, parts in (
            ("complete", result.complete),
            ("pending", result.pending),
            ("saturated", result.saturated),
        ):
            for part in parts:
                cursor = await conn.execute(
                    "INSERT INTO history_sync_state VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id,environment,stream,symbol,start_ms,end_ms) "
                    "DO UPDATE SET state=excluded.state,checked_at=excluded.checked_at",
                    (
                        scope.account_id,
                        scope.environment,
                        stream,
                        symbol,
                        part.start,
                        part.end,
                        state,
                        checked_at if state != "pending" else None,
                    ),
                )
                await cursor.close()

        await _compact_coverage(conn, scope, stream, symbol)

    async def windows(self, scope: HistoryScope, stream: str, symbol: str) -> list:
        return await self._db.fetchall(
            "SELECT start_ms,end_ms,state,checked_at FROM history_sync_state "
            "WHERE account_id=? AND environment=? AND stream=? AND symbol=? ORDER BY start_ms",
            (scope.account_id, scope.environment, stream, symbol),
        )

    async def fills(self, scope: HistoryScope, symbol: str, window: Window) -> list[UserTradeDTO]:
        rows = await self._db.fetchall(
            "SELECT * FROM follower_fills WHERE account_id=? AND environment=? AND symbol=? "
            "AND executed_at BETWEEN ? AND ? ORDER BY executed_at, length(exchange_trade_id), "
            "exchange_trade_id",
            (scope.account_id, scope.environment, symbol, window.start, window.end),
        )
        return [
            UserTradeDTO(
                id=int(row["exchange_trade_id"]),
                orderId=int(row["order_id"]),
                symbol=row["symbol"],
                side=row["side"],
                positionSide=row["position_side"],
                qty=row["qty"],
                price=row["price"],
                quoteQty=row["quote_qty"],
                realizedPnl=row["realized_pnl"],
                commission=row["commission"],
                commissionAsset=row["commission_asset"],
                time=row["executed_at"],
            )
            for row in rows
        ]

    async def repair_candidate(self, scope: HistoryScope, now: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT c.command_key,c.client_order_id,c.kind,"
            "json_extract(c.payload,'$.symbol') symbol "
            "FROM execution_commands c LEFT JOIN history_order_checks h ON "
            "h.account_id=c.account_id AND h.environment=? AND h.command_key=c.command_key "
            "LEFT JOIN follower_order_observations o ON o.account_id=c.account_id "
            "AND o.environment=? AND o.client_order_id=c.client_order_id "
            "WHERE c.account_id=? AND c.status IN ('CONFIRMED','SUBMITTED') "
            "AND c.client_order_id IS NOT NULL AND json_valid(c.payload) "
            "AND json_extract(c.payload,'$.symbol') IS NOT NULL "
            "AND (o.order_id IS NULL OR "
            "o.status NOT IN ('FILLED','CANCELED','EXPIRED','REJECTED')) "
            "AND (h.checked_at IS NULL OR h.checked_at<?) "
            "ORDER BY COALESCE(h.checked_at,0),c.created_at,c.command_key LIMIT 1",
            (scope.environment, scope.environment, scope.account_id, now - 300000),
        )
        return dict(row) if row is not None else None

    async def record_order_check(
        self,
        scope: HistoryScope,
        command_key: str,
        checked_at: int,
        *,
        found: bool,
    ) -> None:
        await self._db.execute_write(
            "INSERT INTO history_order_checks VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id,environment,command_key) DO UPDATE SET "
            "checked_at=excluded.checked_at,outcome=excluded.outcome",
            (
                scope.account_id,
                scope.environment,
                command_key,
                checked_at,
                "observed" if found else "not_found",
            ),
        )

    async def observe_order(
        self,
        scope: HistoryScope,
        order: OrderDTO,
        *,
        kind: str,
        command_key: str | None,
        observed_at: int,
    ) -> None:
        # Cumulative order values are evidence only, never additive fills.
        await self._db.execute_write(
            "INSERT INTO follower_order_observations VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,environment,symbol,order_id) DO UPDATE SET "
            "client_order_id=excluded.client_order_id, "
            "command_key=COALESCE(excluded.command_key,follower_order_observations.command_key), "
            "kind=excluded.kind,status=excluded.status,executed_qty=excluded.executed_qty, "
            "avg_price=excluded.avg_price,observed_at=excluded.observed_at "
            "WHERE excluded.observed_at >= follower_order_observations.observed_at",
            (
                scope.account_id,
                scope.environment,
                order.symbol,
                str(order.order_id),
                order.client_order_id,
                command_key,
                kind,
                order.status,
                _decimal_text(order.executed_qty),
                _decimal_text(order.avg_price),
                observed_at,
            ),
        )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


async def _insert_facts(
    conn: aiosqlite.Connection,
    table: str,
    key_columns: tuple[str, ...],
    rows: list[tuple],
) -> None:
    # Table/columns are internal constants, never transport input. Batch both
    # lookups and inserts to avoid one aiosqlite thread hop per individual fill.
    for offset in range(0, len(rows), 200):
        chunk = rows[offset : offset + 200]
        keyed = {}
        for row in chunk:
            key = row[:4]
            if key in keyed and keyed[key] != row:
                raise HistoryConflictError("conflicting history identity")
            keyed[key] = row
        placeholders = ",".join("(?,?,?,?)" for _ in keyed)
        cursor = await conn.execute(
            f"SELECT * FROM {table} WHERE ({','.join(key_columns)}) IN ({placeholders})",
            tuple(value for key in keyed for value in key),
        )
        try:
            existing = await cursor.fetchall()
        finally:
            await cursor.close()
        for old in existing:
            key = tuple(old)[:4]
            if tuple(old) != keyed[key]:
                raise HistoryConflictError("conflicting history identity")
            del keyed[key]
        if keyed:
            cursor = await conn.executemany(
                f"INSERT INTO {table} VALUES ({','.join('?' for _ in chunk[0])})",
                list(keyed.values()),
            )
            await cursor.close()


async def _compact_coverage(
    conn: aiosqlite.Connection,
    scope: HistoryScope,
    stream: str,
    symbol: str,
) -> None:
    params = (scope.account_id, scope.environment, stream, symbol)
    cursor = await conn.execute(
        "SELECT start_ms,end_ms,checked_at FROM history_sync_state "
        "WHERE account_id=? AND environment=? AND stream=? AND symbol=? "
        "AND state='complete' ORDER BY start_ms",
        params,
    )
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    merged: list[list[int]] = []
    for row in rows:
        start, end, checked = tuple(row)
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = min(merged[-1][2], checked)
        else:
            merged.append([start, end, checked])
    if len(merged) == len(rows):
        return
    cursor = await conn.execute(
        "DELETE FROM history_sync_state WHERE account_id=? AND environment=? "
        "AND stream=? AND symbol=? AND state='complete'",
        params,
    )
    await cursor.close()
    cursor = await conn.executemany(
        "INSERT INTO history_sync_state VALUES (?,?,?,?,?,?,'complete',?)",
        [(*params, start, end, checked) for start, end, checked in merged],
    )
    await cursor.close()
