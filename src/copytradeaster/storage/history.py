"""Atomic follower facts and import coverage, isolated by account/environment.

Canonical history is retained without TTL. Repeated identical facts are no-ops;
a conflicting exchange identity aborts the batch instead of silently changing
financial history. Projection and user events will share this transaction path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import aiosqlite

from copytradeaster.aster.models import IncomeDTO, OrderDTO, UserTradeDTO
from copytradeaster.history.models import ImportDelta
from copytradeaster.history.reader import Span, Window, WindowRead
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
    ) -> ImportDelta:
        """Store one import batch and report what actually changed.

        The facts revision bumps inside the same transaction whenever new
        facts land or coverage newly completes; a repeat import of identical
        evidence returns ``inserted_count == 0`` and ``coverage_changed ==
        False`` so replay can stand down.
        """
        if not symbol or any(row.symbol != symbol for row in result.rows):
            raise ValueError("fill symbol mismatch")
        self._validate_result(window, result)
        async with self._db.transaction() as conn:
            before = await _complete_intervals(conn, scope, "fills", symbol)
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
            inserted = await _insert_facts(
                conn,
                "follower_fills",
                ("account_id", "environment", "symbol", "exchange_trade_id"),
                batch,
            )
            await self._commit_windows(conn, scope, "fills", symbol, window, result, checked_at)
            after = await _complete_intervals(conn, scope, "fills", symbol)
            changed_from = min((row.time for row in result.rows), default=None)
            coverage_changed = before != after
            coverage_from = _new_coverage_start(before, after)
            revision = await _bump_scope_revision(
                conn, scope, symbol, checked_at, changed=inserted > 0 or coverage_changed
            )
            states = await conn.execute(
                "SELECT MAX(end_ms) FROM history_sync_state WHERE account_id=? "
                "AND environment=? AND stream='fills' AND symbol=? AND state='complete'",
                (scope.account_id, scope.environment, symbol),
            )
            try:
                frontier = await states.fetchone()
            finally:
                await states.close()
            return ImportDelta(
                inserted_count=inserted,
                changed_from_ms=changed_from if inserted > 0 else None,
                coverage_changed=coverage_changed,
                coverage_changed_from_ms=coverage_from,
                verified_through_ms=frontier[0] if frontier and frontier[0] is not None else None,
                facts_revision=revision,
            )

    async def facts_revision(self, scope: HistoryScope, symbol: str) -> int:
        """Durable import revision for one scope/symbol; 0 means never imported."""
        row = await self._db.fetchone(
            "SELECT facts_revision FROM history_scope_state "
            "WHERE account_id=? AND environment=? AND symbol=?",
            (scope.account_id, scope.environment, symbol),
        )
        return int(row["facts_revision"]) if row is not None else 0

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

    async def capture_state(self, scope: HistoryScope, symbol: str) -> dict | None:
        """Newest fully published window end per symbol; None means never."""
        row = await self._db.fetchone(
            "SELECT cap_start_ms,cap_end_ms,last_error,updated_at FROM history_capture_state "
            "WHERE account_id=? AND environment=? AND symbol=?",
            (scope.account_id, scope.environment, symbol),
        )
        return dict(row) if row is not None else None

    async def record_capture(
        self,
        scope: HistoryScope,
        symbol: str,
        *,
        start_ms: int | None,
        end_ms: int | None,
        now_ms: int,
        error: str | None = None,
    ) -> None:
        """Replace the publication frontier; dumb put, policy lives in capture."""
        if (
            (start_ms is not None and (type(start_ms) is not int or start_ms < 0))
            or (end_ms is not None and (type(end_ms) is not int or end_ms < 0))
            or (start_ms is not None and end_ms is not None and end_ms < start_ms)
            or type(now_ms) is not int
            or now_ms < 0
            or (error is not None and (not isinstance(error, str) or len(error) > 128))
        ):
            raise ValueError("invalid capture checkpoint")
        await self._db.execute_write(
            "INSERT INTO history_capture_state VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,environment,symbol) DO UPDATE SET "
            "cap_start_ms=excluded.cap_start_ms,cap_end_ms=excluded.cap_end_ms,"
            "last_error=excluded.last_error,updated_at=excluded.updated_at",
            (scope.account_id, scope.environment, symbol, start_ms, end_ms, error, now_ms),
        )

    async def fills(
        self, scope: HistoryScope, symbol: str, window: Window | Span
    ) -> list[UserTradeDTO]:
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

    async def coverage_from(self, scope: HistoryScope) -> int | None:
        """Earliest verified fill coverage; None means nothing proven yet."""
        row = await self._db.fetchone(
            "SELECT MIN(start_ms) AS start_ms FROM history_sync_state"
            " WHERE account_id=? AND environment=? AND stream='fills' AND state='complete'",
            (scope.account_id, scope.environment),
        )
        return None if row is None or row["start_ms"] is None else int(row["start_ms"])

    async def proven_order_sources(self, scope: HistoryScope, symbol: str) -> dict[int, str]:
        """Exchange order ids proven to be copy-driven, from durable links.

        A fill order counts as ``copy`` only when three durable rows agree:
        an order observation, a CONFIRMED entry/exit command under the same
        command key, and an order link with a non-null master trade id under
        the same follower client order id. Anything else stays ``unknown``;
        absence of a link never proves an external trade.
        """
        if not symbol:
            raise ValueError("order source symbol required")
        rows = await self._db.fetchall(
            "SELECT o.order_id FROM follower_order_observations o "
            "JOIN execution_commands c ON c.account_id=o.account_id "
            "AND c.command_key=o.command_key "
            "JOIN order_links l ON l.account_id=o.account_id "
            "AND l.follower_client_order_id=o.client_order_id "
            "WHERE o.account_id=? AND o.environment=? AND o.symbol=? "
            "AND c.status='CONFIRMED' AND c.kind IN ('MARKET_OPEN','MARKET_CLOSE') "
            "AND o.kind=c.kind AND l.kind=c.kind AND l.trade_id IS NOT NULL",
            (scope.account_id, scope.environment, symbol),
        )
        sources: dict[int, str] = {}
        for row in rows:
            try:
                order_id = int(row["order_id"])
            except (TypeError, ValueError):
                continue
            sources[order_id] = "copy"
        return sources

    async def protection_observations(
        self, scope: HistoryScope, symbol: str, start_ms: int, end_ms: int
    ) -> list[dict]:
        """Confirmed protection lifecycle rows inside one window, oldest first."""
        if not symbol:
            raise ValueError("protection symbol required")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms < start_ms
        ):
            raise ValueError("protection window must be a valid range")
        rows = await self._db.fetchall(
            "SELECT order_id,client_order_id,command_key,kind,status,observed_at "
            "FROM follower_order_observations "
            "WHERE account_id=? AND environment=? AND symbol=? "
            "AND kind IN ('PLACE_STOP','PLACE_TP','CANCEL_ORDER') "
            "AND observed_at BETWEEN ? AND ? ORDER BY observed_at, order_id",
            (scope.account_id, scope.environment, symbol, start_ms, end_ms),
        )
        return [dict(row) for row in rows]

    async def latest_protection_places(
        self, scope: HistoryScope, symbol: str, before_ms: int
    ) -> list[dict]:
        """Newest stop/take-profit placement per leg and slot before a time.

        The partition key is ``(kind, leg)`` with the leg derived from the
        placement *command* payload (``positionSide``, one-way orders
        honestly read as ``BOTH``), never from the mutable observation row
        alone: two legs of the same slot converge independently.
        """
        if not symbol or type(before_ms) is not int or before_ms < 0:
            raise ValueError("protection baseline requires a symbol and time")
        leg = (
            "CASE WHEN json_valid(c.payload) THEN COALESCE(NULLIF(UPPER(TRIM("
            "json_extract(c.payload,'$.positionSide'))),'')"
            ",'BOTH') ELSE 'BOTH' END"
        )
        rows = await self._db.fetchall(
            "SELECT order_id,client_order_id,command_key,kind,status,observed_at,leg FROM ("
            f"SELECT o.order_id,o.client_order_id,o.command_key,o.kind,o.status,o.observed_at,"
            f"{leg} AS leg, ROW_NUMBER() OVER (PARTITION BY o.kind,{leg} "
            "ORDER BY o.observed_at DESC, o.order_id DESC) AS rn "
            "FROM follower_order_observations o "
            "LEFT JOIN execution_commands c ON c.command_key=o.command_key "
            "WHERE o.account_id=? AND o.environment=? AND o.symbol=? "
            "AND o.kind IN ('PLACE_STOP','PLACE_TP') AND o.observed_at < ?) WHERE rn=1",
            (scope.account_id, scope.environment, symbol, before_ms),
        )
        return [dict(row) for row in rows]

    async def protection_canceled_before(
        self, scope: HistoryScope, symbol: str, replaced_coid: str, before_ms: int
    ) -> bool:
        """Whether a confirmed cancel removed one protection order earlier.

        Exchange observations are the first proof; CONFIRMED journal cancel
        commands are the second (an exchange-unknown ``-2011`` confirm writes
        no observation, yet proves the order no longer rests). A proven
        cancel with an unreadable journal time excludes the slot rather than
        risking a stale baseline claim.
        """
        if not symbol or not replaced_coid or type(before_ms) is not int or before_ms < 0:
            raise ValueError("cancel check requires a symbol, order id and time")
        row = await self._db.fetchone(
            "SELECT 1 FROM follower_order_observations o JOIN execution_commands c "
            "ON c.account_id=o.account_id AND c.command_key=o.command_key "
            "WHERE o.account_id=? AND o.environment=? AND o.symbol=? "
            "AND o.kind='CANCEL_ORDER' AND o.observed_at < ? "
            "AND c.status='CONFIRMED' "
            "AND json_valid(c.payload) AND json_extract(c.payload,'$.origClientOrderId')=? "
            "LIMIT 1",
            (scope.account_id, scope.environment, symbol, before_ms, replaced_coid),
        )
        if row is not None:
            return True
        cmds = await self._db.fetchall(
            "SELECT created_at FROM execution_commands "
            "WHERE account_id=? AND kind='CANCEL_ORDER' AND status='CONFIRMED' "
            "AND json_valid(payload) AND json_extract(payload,'$.origClientOrderId')=? "
            "AND json_extract(payload,'$.symbol')=?",
            (scope.account_id, replaced_coid, symbol),
        )
        for cmd in cmds:
            created_ms = _iso_to_ms(cmd["created_at"])
            if created_ms is None or created_ms < before_ms:
                return True
        return False

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

    async def fills_by_trade_ids(
        self, scope: HistoryScope, keys: Sequence[tuple[str, str]]
    ) -> list[UserTradeDTO]:
        """Owned fills for exact ``(symbol, exchange_trade_id)`` keys.

        C5 money read (plan §23.6): no time-range or side-only selection, so
        overlapping HEDGE legs never leak and two fills sharing a numeric id
        on different symbols stay distinct. Empty keys return no rows.
        """
        wanted = tuple(keys)
        if not wanted:
            return []
        if any(
            not isinstance(symbol, str) or not symbol or not isinstance(trade, str) or not trade
            for symbol, trade in wanted
        ):
            raise ValueError("fill keys must be nonempty symbol and trade id strings")
        if len(set(wanted)) != len(wanted):
            raise ValueError("fill keys must be unique")
        pairs = ",".join("(?,?)" for _ in wanted)
        params: tuple[object, ...] = (scope.account_id, scope.environment)
        params += tuple(value for key in wanted for value in key)
        rows = await self._db.fetchall(
            "SELECT * FROM follower_fills WHERE account_id=? AND environment=? "
            f"AND (symbol,exchange_trade_id) IN ({pairs}) "
            "ORDER BY executed_at, length(exchange_trade_id), exchange_trade_id",
            params,
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

    async def cashflows_for_symbols(
        self, scope: HistoryScope, symbols: Sequence[str]
    ) -> list[IncomeDTO]:
        """Owned cashflows for the requested symbols, oldest first.

        C5 money read (plan §23.6): the canonical signed source for
        commissions, funding and builder flows. Symbol-empty global rows are
        included so no proven flow is silently dropped; callers attribute
        them explicitly and never treat a non-empty response as complete.
        """
        wanted = tuple(symbols)
        if not wanted:
            return []
        if any(not isinstance(symbol, str) or not symbol for symbol in wanted):
            raise ValueError("cashflow symbols must be nonempty strings")
        if len(set(wanted)) != len(wanted):
            raise ValueError("cashflow symbols must be unique")
        marks = ",".join("?" for _ in wanted)
        rows = await self._db.fetchall(
            "SELECT * FROM follower_cash_flows WHERE account_id=? AND environment=? "
            f"AND (symbol IN ({marks}) OR symbol='') "
            "ORDER BY occurred_at, income_type, tran_id",
            (scope.account_id, scope.environment, *wanted),
        )
        return [
            IncomeDTO(
                symbol=row["symbol"],
                incomeType=row["income_type"],
                income=row["amount"],
                asset=row["asset"],
                time=row["occurred_at"],
                tranId=row["tran_id"],
                tradeId=row["exchange_trade_id"],
            )
            for row in rows
        ]

    async def sibling_refs(
        self, scope: HistoryScope, keys: Sequence[tuple[str, str]]
    ) -> tuple[str, ...]:
        """Every episode ref sharing one of the given allocation keys.

        C5 money read (plan §23.6): a shared flip fill loads ALL sibling
        allocations even when the sibling episode sits outside the current
        page, so the signed COMMISSION total splits exactly once.
        """
        wanted = tuple(keys)
        if not wanted:
            return ()
        if any(
            not isinstance(symbol, str) or not symbol or not isinstance(trade, str) or not trade
            for symbol, trade in wanted
        ):
            raise ValueError("allocation keys must be nonempty symbol and trade id strings")
        if len(set(wanted)) != len(wanted):
            raise ValueError("allocation keys must be unique")
        pairs = ",".join("(?,?)" for _ in wanted)
        params: tuple[object, ...] = (scope.account_id, scope.environment)
        params += tuple(value for key in wanted for value in key)
        rows = await self._db.fetchall(
            "SELECT DISTINCT trade_ref FROM follower_episode_allocations "
            "WHERE account_id=? AND environment=? "
            f"AND (symbol,exchange_trade_id) IN ({pairs}) ORDER BY trade_ref",
            params,
        )
        return tuple(str(row["trade_ref"]) for row in rows)

    async def builder_policy(self, scope: HistoryScope) -> str:
        """Durable fee policy for money proof; the cap never becomes an expense.

        C5 money read (plan §23.6): ``direct`` accounts prove
        ``included_in_commission`` (no separate builder flow expected);
        ``builder`` accounts and unknown rows stay ``unknown`` so their net
        keeps a concrete gap instead of an invented zero. Only proven USDT
        flows ever count as costs.
        """
        row = await self._db.fetchone(
            "SELECT identity_source FROM accounts WHERE id=?",
            (scope.account_id,),
        )
        if row is None:
            return "unknown"
        try:
            source = str(row["identity_source"] or "direct").strip().lower()
        except (TypeError, ValueError, AttributeError):
            return "unknown"
        if source == "builder":
            return "unknown"
        if source == "direct":
            return "included_in_commission"
        return "unknown"


def _iso_to_ms(value: object) -> int | None:
    """Journal ISO time as epoch ms; None when unreadable (never invented)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


async def _complete_intervals(
    conn: aiosqlite.Connection, scope: HistoryScope, stream: str, symbol: str
) -> tuple[tuple[int, int], ...]:
    cursor = await conn.execute(
        "SELECT start_ms,end_ms FROM history_sync_state WHERE account_id=? "
        "AND environment=? AND stream=? AND symbol=? AND state='complete' ORDER BY start_ms",
        (scope.account_id, scope.environment, stream, symbol),
    )
    try:
        rows = await cursor.fetchall()
    finally:
        await cursor.close()
    return tuple((int(row[0]), int(row[1])) for row in rows)


def _new_coverage_start(
    before: tuple[tuple[int, int], ...], after: tuple[tuple[int, int], ...]
) -> int | None:
    """Earliest start the ``after`` set covers that ``before`` did not."""
    starts = [
        start
        for start, _ in after
        if not any(prev_start <= start <= prev_end for prev_start, prev_end in before)
    ]
    return min(starts) if starts else None


async def _bump_scope_revision(
    conn: aiosqlite.Connection,
    scope: HistoryScope,
    symbol: str,
    checked_at: int,
    *,
    changed: bool,
) -> int:
    cursor = await conn.execute(
        "SELECT facts_revision FROM history_scope_state "
        "WHERE account_id=? AND environment=? AND symbol=?",
        (scope.account_id, scope.environment, symbol),
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    revision = int(row[0]) if row is not None else 0
    if changed:
        revision += 1
        cursor = await conn.execute(
            "INSERT INTO history_scope_state VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id,environment,symbol) DO UPDATE SET "
            "facts_revision=excluded.facts_revision,updated_at=excluded.updated_at",
            (scope.account_id, scope.environment, symbol, revision, checked_at),
        )
        await cursor.close()
    return revision


async def _insert_facts(
    conn: aiosqlite.Connection,
    table: str,
    key_columns: tuple[str, ...],
    rows: list[tuple],
) -> int:
    """Insert new facts only; returns the stored row count."""
    # Table/columns are internal constants, never transport input. Batch both
    # lookups and inserts to avoid one aiosqlite thread hop per individual fill.
    inserted = 0
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
            inserted += len(keyed)
    return inserted


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
