"""Durable per-account user-event delivery cursor (C6, plan §23.7).

The legacy user-event recovery rescans from a timestamp floor on every boot:
a crash between enqueue and floor advance either resends or, worse, a large
backlog blocks startup. This module replaces the timestamp floor with a
durable per-scope scan cursor in ``user_event_delivery_state``:

* each sweep fixes a target high-water (current ``max_seq``) per scope and
  reads batches of at most :data:`DELIVERY_BATCH_LIMIT` rows with
  ``scanned_seq < seq <= target``; backfill-silent and pre-rollout rows also
  advance the scan, so they never block or re-notify;
* formatting and recipient resolution happen BEFORE the short commit; one DB
  transaction then inserts every due outbox row and advances ``scanned_seq``
  to the last considered row;
* a crash therefore replays idempotently through the existing
  ``user-event:{event_ref}`` keys (``ON CONFLICT DO NOTHING``): never a skip,
  never a duplicate row;
* a bounded ``budget`` stops the sweep early with the saved position intact,
  so the next tick resumes where this one stopped and startup never waits
  out a huge backlog.

Unrenderable eligible rows are a diagnosable, blocking condition: the sweep
logs them, counts them in
:attr:`UserEventDeliveryReport.blocked_unrenderable`, and leaves the cursor
before the broken row, so the next sweep retries from that row explicitly
instead of advancing past it silently. Broken rows are finite; the retry is
deliberate, not an infinite loop. A missing recipient (or globally disabled
notifications) is the opposite: an explicit deliberate skip that advances
the scan, so enabling notifications later never retroactively sends it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from copytradeaster.logging_utils import get_logger
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import NotificationOutboxRepo, SettingsRepo

log = get_logger(__name__)

#: Rows per atomic batch: mirrors the event feed page bound so one commit
#: stays short even while a sweep walks a huge backlog.
DELIVERY_BATCH_LIMIT = 200

#: app_state key for the rollout cutoff. Recorded once on first enable; rows
#: recorded before it stay silent so accumulated backfill never blasts. The
#: key never moves afterwards (unlike the legacy recovery floor, which still
#: advances on success for command recovery).
USER_EVENT_DELIVERY_ROLLOUT_FLOOR_KEY = "user_event_delivery_rollout_floor"

_OUTBOX_INSERT_SQL = (
    "INSERT INTO notification_outbox(event_key, telegram_user_id, chat_id, account_id, "
    "kind, severity, text, status, attempts, available_at, lease_until, claimed_by, "
    "last_error, created_at, updated_at, sent_at, trade_ref, action) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(event_key, telegram_user_id) DO NOTHING"
)


@dataclass(slots=True)
class UserEventDeliveryReport:
    """Diagnosable outcome of one durable delivery sweep.

    ``recovered`` counts events planned this sweep (one per stored event with
    at least one recipient). Inserts are idempotent, so a replay may re-plan
    an already-stored row without duplicating it.
    """

    recovered: int = 0
    skipped_ineligible: int = 0
    skipped_no_recipient: int = 0
    blocked_unrenderable: int = 0
    budget_exhausted: bool = False


async def load_scanned_seq(db: Database, scope: HistoryScope) -> int:
    """Return the durable scan cursor; zero when the scope never swept."""
    if not isinstance(scope, HistoryScope):
        raise ValueError("delivery scope must be a HistoryScope")
    row = await db.fetchone(
        "SELECT scanned_seq FROM user_event_delivery_state WHERE account_id=? AND environment=?",
        (scope.account_id, scope.environment),
    )
    if row is None:
        return 0
    return int(row["scanned_seq"])


async def ensure_user_event_rollout_floor(db: Database, *, now: datetime) -> int:
    """Return the rollout cutoff in ms, recording it on first enable.

    A corrupt stored floor fails closed instead of notifying the backlog.
    """
    settings = SettingsRepo(db)
    current = await settings.get_flag(USER_EVENT_DELIVERY_ROLLOUT_FLOOR_KEY)
    if current is None:
        floor = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
        await settings.set_flag(USER_EVENT_DELIVERY_ROLLOUT_FLOOR_KEY, floor.isoformat())
        return int(floor.timestamp() * 1000)
    try:
        parsed = datetime.fromisoformat(current.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid user event delivery rollout floor") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


async def recover_user_events_durable(
    db: Database,
    router: NotificationRouter | None,
    events: EventRepository,
    scopes: Iterable[HistoryScope],
    *,
    rollout_floor_ms: int = 0,
    budget: int | None = None,
) -> UserEventDeliveryReport:
    """Sweep owned events to the outbox behind the durable per-scope cursor.

    ``router`` may be ``None`` in console-only mode: the sweep then advances
    every cursor without enqueueing, so enabling notifications later never
    retroactively sends muted rows. Any ``ValueError`` raised while planning
    one eligible row (unknown kind, action/ref mismatch) blocks its scope at
    that row instead of skipping past it.
    """
    if type(rollout_floor_ms) is not int or rollout_floor_ms < 0:
        raise ValueError("delivery rollout floor must be a nonnegative integer")
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("delivery budget must be a nonnegative integer or None")
    report = UserEventDeliveryReport()
    remaining = budget
    stop_all = False
    for scope in scopes:
        if stop_all:
            break
        if not isinstance(scope, HistoryScope):
            continue
        target = await events.max_seq(scope)
        scanned = await load_scanned_seq(db, scope)
        while scanned < target:
            if remaining is not None and remaining <= 0:
                report.budget_exhausted = True
                stop_all = True
                break
            if remaining is None:
                fetch = DELIVERY_BATCH_LIMIT
            else:
                fetch = min(DELIVERY_BATCH_LIMIT, remaining)
            rows = await events.list_owned(scope, after_seq=scanned, limit=fetch)
            batch = [stored for stored in rows if stored.seq <= target]
            if not batch:
                break
            due: list[dict[str, Any]] = []
            last_considered = scanned
            scope_blocked = False
            for stored in batch:
                if remaining is not None:
                    if remaining <= 0:
                        break
                    remaining -= 1
                record = stored.event
                if not record.notify_eligible or record.recorded_at < rollout_floor_ms:
                    report.skipped_ineligible += 1
                    last_considered = stored.seq
                    continue
                if router is None:
                    report.skipped_no_recipient += 1
                    last_considered = stored.seq
                    continue
                try:
                    planned = await router.prepare_user_event_notification(stored)
                except ValueError:
                    log.warning(
                        "blocking unrenderable user event (kind=%s account_id=%s seq=%s)",
                        record.kind,
                        scope.account_id,
                        stored.seq,
                    )
                    report.blocked_unrenderable += 1
                    scope_blocked = True
                    break
                if planned is None:
                    report.skipped_no_recipient += 1
                    last_considered = stored.seq
                    continue
                event, recipients = planned
                due.extend(
                    {**row, "user_event_seq": stored.seq}
                    for row in NotificationRouter.plan_outbox_rows(event, recipients)
                )
                report.recovered += 1
                last_considered = stored.seq
            if last_considered > scanned:
                await _commit_batch(db, scope, due, last_considered)
                scanned = last_considered
            if scope_blocked:
                # The cursor stays before the broken row: the next sweep
                # retries from that row explicitly instead of skipping it.
                break
    return report


async def _commit_batch(
    db: Database,
    scope: HistoryScope,
    due_rows: list[dict[str, Any]],
    last_seq: int,
) -> None:
    """Insert one batch and advance the cursor in a single transaction.

    A crash before commit replays the batch idempotently; a crash after
    commit resumes past it. Neither case skips or duplicates.
    """
    now_ms = int(time.time() * 1000)
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "SELECT scanned_seq FROM user_event_delivery_state "
            "WHERE account_id=? AND environment=?",
            (scope.account_id, scope.environment),
        )
        state = await cursor.fetchone()
        await cursor.close()
        committed_seq = int(state["scanned_seq"]) if state is not None else 0
        for row in due_rows:
            if int(row["user_event_seq"]) <= committed_seq:
                continue  # A toggle or another committed sweep already consumed it.
            cursor = await conn.execute(
                "SELECT 1 FROM telegram_users WHERE telegram_user_id=? AND account_id=? "
                "AND notifications_enabled=1 AND blocked_at IS NULL",
                (row["telegram_user_id"], scope.account_id),
            )
            eligible = await cursor.fetchone()
            await cursor.close()
            if eligible is None:
                continue
            # Pure value mapping shared with the live outbox path, so the row
            # shape stays identical by construction (see repositories).
            values = NotificationOutboxRepo._event_values(row)
            cursor = await conn.execute(_OUTBOX_INSERT_SQL, values)
            await cursor.close()
        cursor = await conn.execute(
            "INSERT INTO user_event_delivery_state(account_id, environment, scanned_seq,"
            " updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(account_id, environment) DO UPDATE SET"
            " scanned_seq=MAX(user_event_delivery_state.scanned_seq,excluded.scanned_seq),"
            " updated_at=excluded.updated_at",
            (scope.account_id, scope.environment, int(last_seq), now_ms),
        )
        await cursor.close()
