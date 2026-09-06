"""Immutable account-owned user events for the activity feed.

Events are confirmed facts with safe structured numbers, never a live view of
the execution journal: replays resolve to the same public ref through the
per-account dedup key, and feed cursors use the internal monotonic seq so a
late backfill never shifts already opened pages. Notification delivery state
lives in the outbox; events carry only eligibility.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import aiosqlite

from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryScope

EventKind = Literal[
    "POSITION_OPENED",
    "POSITION_INCREASED",
    "POSITION_REDUCED",
    "POSITION_CLOSED",
    "PROTECTION_SET",
    "PROTECTION_CHANGED",
    "PROTECTION_UNCONFIRMED",
    "RISK_CHANGED",
    "COPY_PAUSED",
    "COPY_RESUMED",
    "CONNECTIVITY_LOST",
    "CONNECTIVITY_RESTORED",
    "ENTRY_SKIPPED",
]

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
        "PROTECTION_SET",
        "PROTECTION_CHANGED",
        "PROTECTION_UNCONFIRMED",
        "RISK_CHANGED",
        "COPY_PAUSED",
        "COPY_RESUMED",
        "CONNECTIVITY_LOST",
        "CONNECTIVITY_RESTORED",
        "ENTRY_SKIPPED",
    }
)

FactValue = str | int | bool | None
MAX_PAGE = 200


@dataclass(frozen=True, slots=True)
class UserEvent:
    event_ref: str
    kind: EventKind
    trade_ref: str | None
    occurred_at: int
    recorded_at: int
    facts: Mapping[str, FactValue]
    dedup_key: str
    notify_eligible: bool


@dataclass(frozen=True, slots=True)
class StoredEvent:
    scope: HistoryScope
    event: UserEvent
    seq: int


@dataclass(frozen=True, slots=True)
class EventDraft:
    """Complete write params for one immutable event, built by derivation."""

    kind: EventKind
    trade_ref: str | None
    occurred_at: int
    recorded_at: int
    facts: dict[str, FactValue]
    dedup_key: str
    notify_eligible: bool


class EventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        scope: HistoryScope,
        *,
        kind: str,
        occurred_at: int,
        recorded_at: int,
        facts: Mapping[str, FactValue],
        dedup_key: str,
        trade_ref: str | None = None,
        notify_eligible: bool = False,
    ) -> StoredEvent:
        """Persist one event idempotently; a replay returns the stored row.

        The dedup key is the identity: a repeated record with the same key
        returns the first write unchanged, never a merged update.
        """
        draft = EventDraft(
            kind=kind,
            trade_ref=trade_ref,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            facts=dict(facts),
            dedup_key=dedup_key,
            notify_eligible=notify_eligible,
        )
        ref = uuid.uuid4().hex
        async with self._db.transaction() as conn:
            await self.insert_many(conn, scope, [draft], event_refs=[ref])
            row = await self._db.fetchone(
                "SELECT seq,event_ref,trade_ref,kind,occurred_at,recorded_at,"
                "facts,dedup_key,notify_eligible FROM user_activity_events"
                " WHERE account_id=? AND environment=? AND dedup_key=?",
                (scope.account_id, scope.environment, dedup_key),
            )
        if row is None:  # INSERT-or-existing under one transaction; never absent.
            raise RuntimeError("event write lost its own row")
        return _decode(scope, row)

    async def insert_many(
        self,
        conn: aiosqlite.Connection,
        scope: HistoryScope,
        drafts: Sequence[EventDraft],
        *,
        event_refs: Sequence[str] | None = None,
    ) -> None:
        """Insert drafts on the caller's connection and transaction.

        Every draft is validated before any row is written; a repeated
        dedup key keeps the first write. The caller owns atomicity: episode
        publication passes its own transaction so episodes and their events
        commit or roll back together.
        """
        refs = list(event_refs) if event_refs is not None else [uuid.uuid4().hex for _ in drafts]
        if len(refs) != len(drafts) or len({d.dedup_key for d in drafts}) != len(drafts):
            raise ValueError("event batch requires unique refs and dedup keys")
        bound = [_validated_draft(draft) for draft in drafts]
        for ref, params in zip(refs, bound, strict=True):
            cursor = await conn.execute(
                "INSERT INTO user_activity_events(event_ref,account_id,environment,"
                "trade_ref,kind,occurred_at,recorded_at,facts,dedup_key,notify_eligible)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_id,environment,dedup_key) DO NOTHING",
                (ref, scope.account_id, scope.environment, *params),
            )
            await cursor.close()

    async def get(self, scope: HistoryScope, event_ref: str) -> StoredEvent | None:
        row = await self._db.fetchone(
            "SELECT seq,event_ref,trade_ref,kind,occurred_at,recorded_at,"
            "facts,dedup_key,notify_eligible FROM user_activity_events"
            " WHERE account_id=? AND environment=? AND event_ref=?",
            (scope.account_id, scope.environment, event_ref),
        )
        return _decode(scope, row) if row is not None else None

    async def list_owned(
        self,
        scope: HistoryScope,
        *,
        kinds: Sequence[str] | None = None,
        after_seq: int = 0,
        limit: int = 50,
    ) -> list[StoredEvent]:
        """Stable feed page: strictly increasing seq, never reordered."""
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("feed cursor must be a nonnegative integer")
        _check_limit(limit)
        params: list[object] = [scope.account_id, scope.environment]
        where = "account_id=? AND environment=? AND seq>?"
        params.append(after_seq)
        if kinds is not None:
            kinds = tuple(kinds)
            if not kinds or any(kind not in EVENT_KINDS for kind in kinds):
                raise ValueError("unknown event kind filter")
            where += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        rows = await self._db.fetchall(
            "SELECT seq,event_ref,trade_ref,kind,occurred_at,recorded_at,"
            f"facts,dedup_key,notify_eligible FROM user_activity_events WHERE {where}"
            f" ORDER BY seq ASC LIMIT {limit}",
            tuple(params),
        )
        return [_decode(scope, row) for row in rows]

    async def list_by_trade(
        self,
        scope: HistoryScope,
        trade_ref: str,
        *,
        after_seq: int = 0,
        limit: int = 50,
    ) -> list[StoredEvent]:
        if not trade_ref:
            raise ValueError("trade ref required")
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("feed cursor must be a nonnegative integer")
        _check_limit(limit)
        rows = await self._db.fetchall(
            "SELECT seq,event_ref,trade_ref,kind,occurred_at,recorded_at,"
            "facts,dedup_key,notify_eligible FROM user_activity_events"
            " WHERE account_id=? AND environment=? AND trade_ref=? AND seq>?"
            f" ORDER BY seq ASC LIMIT {limit}",
            (scope.account_id, scope.environment, trade_ref, after_seq),
        )
        return [_decode(scope, row) for row in rows]

    async def max_seq(self, scope: HistoryScope) -> int:
        """Newest feed position; zero when the account has no events yet."""
        row = await self._db.fetchone(
            "SELECT MAX(seq) AS max_seq FROM user_activity_events"
            " WHERE account_id=? AND environment=?",
            (scope.account_id, scope.environment),
        )
        return 0 if row is None or row["max_seq"] is None else int(row["max_seq"])


def _check_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("feed page must be bounded")


def _canonical_facts(facts: Mapping[str, FactValue]) -> str:
    if not isinstance(facts, Mapping) or not facts:
        raise ValueError("event facts must be a nonempty object")
    for key, value in facts.items():
        if not isinstance(key, str) or not key:
            raise ValueError("event fact keys must be nonempty strings")
        if isinstance(value, float) or not isinstance(value, (str, int, bool, type(None))):
            raise ValueError("event fact values must be strings, integers, booleans or null")
    return json.dumps(dict(facts), sort_keys=True, separators=(",", ":"))


def _validated_draft(draft: EventDraft) -> tuple:
    """Validate one draft and bind its row params (ref excluded)."""
    if draft.kind not in EVENT_KINDS:
        raise ValueError("unknown event kind")
    if (
        type(draft.occurred_at) is not int
        or type(draft.recorded_at) is not int
        or draft.occurred_at < 0
        or draft.recorded_at < 0
    ):
        raise ValueError("event timestamps must be nonnegative integers")
    if not draft.dedup_key:
        raise ValueError("event dedup key required")
    if draft.trade_ref is not None and not draft.trade_ref:
        raise ValueError("trade ref must be opaque or absent")
    return (
        draft.trade_ref,
        draft.kind,
        draft.occurred_at,
        draft.recorded_at,
        _canonical_facts(draft.facts),
        draft.dedup_key,
        1 if draft.notify_eligible else 0,
    )


def _decode(scope: HistoryScope, row: object) -> StoredEvent:
    data = json.loads(row["facts"])
    if not isinstance(data, dict):
        raise ValueError("stored event facts are not an object")
    return StoredEvent(
        scope,
        UserEvent(
            event_ref=str(row["event_ref"]),
            kind=str(row["kind"]),
            trade_ref=str(row["trade_ref"]) if row["trade_ref"] is not None else None,
            occurred_at=int(row["occurred_at"]),
            recorded_at=int(row["recorded_at"]),
            facts=data,
            dedup_key=str(row["dedup_key"]),
            notify_eligible=bool(row["notify_eligible"]),
        ),
        int(row["seq"]),
    )
