"""C6 regression: durable user-event delivery cursor + trade-link gate."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from copytradeaster.application.engine import (
    NOTIFICATION_RECOVERY_FLOOR_KEY,
    _build_worker,
    recover_notification_outbox,
)
from copytradeaster.config import Settings
from copytradeaster.notifications.delivery import (
    USER_EVENT_DELIVERY_ROLLOUT_FLOOR_KEY,
    load_scanned_seq,
    recover_user_events_durable,
)
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.notifications.worker import TelegramOutboxWorker
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository, StoredEvent, UserEvent
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    NotificationOutboxRepo,
    SettingsRepo,
    TelegramUserRepo,
)

_CLOSED_FACTS = {
    "symbol": "BTCUSDT",
    "side": "LONG",
    "entry_price": "62400",
    "exit_price": "64100",
    "realized_gross": "18.42",
    "duration_ms": 8_280_000,
}


async def _open_db(path: Path) -> Database:
    db = Database(path)
    await db.connect()
    await db.migrate()
    return db


async def _setup_owner(db: Database, *, account_id: str, user_id: int, chat_id: int) -> None:
    await AccountRepo(db).upsert(account_id, "follower", account_id, f"0x{user_id:040d}")
    await TelegramUserRepo(db).upsert(user_id, chat_id, account_id=account_id)


async def _seed_closed(
    events: EventRepository,
    scope: HistoryScope,
    name: str,
    *,
    recorded_ms: int,
    eligible: bool = True,
) -> None:
    await events.record(
        scope,
        kind="POSITION_CLOSED",
        occurred_at=recorded_ms,
        recorded_at=recorded_ms,
        facts=dict(_CLOSED_FACTS),
        dedup_key=f"evt:c6:{scope.account_id}:{name}",
        trade_ref=f"trade-{scope.account_id}-{name}",
        notify_eligible=eligible,
    )


async def _outbox_count(db: Database) -> int:
    row = await db.fetchone("SELECT COUNT(*) AS n FROM notification_outbox")
    assert row is not None
    return int(row["n"])


def _router(db: Database) -> NotificationRouter:
    return NotificationRouter(db, console=lambda _event: asyncio.sleep(0))


async def test_durable_sweep_survives_mid_sweep_crash_without_skips_or_doubles(
    tmp_path: Path,
) -> None:
    db = await _open_db(tmp_path / "c6-crash.sqlite3")
    try:
        owners = (("a1", 1, 101), ("a2", 2, 102), ("a3", 3, 103))
        for account_id, user_id, chat_id in owners:
            await _setup_owner(db, account_id=account_id, user_id=user_id, chat_id=chat_id)
        events = EventRepository(db)
        base_ms = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
        eligible = 0
        ineligible = 0
        for account_id, _, _ in owners:
            scope = HistoryScope(account_id, "testnet")
            count = 201 if account_id == "a3" else 400
            for i in range(count):
                await _seed_closed(events, scope, f"e{i}", recorded_ms=base_ms + i)
                eligible += 1
                if i % 40 == 0:
                    await _seed_closed(
                        events, scope, f"silent{i}", recorded_ms=base_ms + i, eligible=False
                    )
                    ineligible += 1
        assert eligible == 1001
        router = _router(db)
        scopes = [HistoryScope(account_id, "testnet") for account_id, _, _ in owners]

        # A bounded tick commits its batches, then stops like a crash would.
        first = await recover_user_events_durable(db, router, events, scopes, budget=500)
        assert first.budget_exhausted is True
        # seq is a global feed counter, so per-scope cursors cannot sum to
        # the budget; the rows-considered invariant is exact instead.
        assert first.recovered + first.skipped_ineligible + first.skipped_no_recipient == 500
        assert first.blocked_unrenderable == 0
        maxima = [await events.max_seq(scope) for scope in scopes]
        cursors = [await load_scanned_seq(db, scope) for scope in scopes]
        assert cursors[0] == maxima[0]  # a1 fully swept
        assert 0 < cursors[1] < maxima[1]  # a2 holds the mid-sweep crash position
        assert cursors[2] == 0  # a3 untouched
        assert await _outbox_count(db) == first.recovered
        assert first.recovered < eligible

        # Replay resumes the saved position and finishes every eligible row.
        second = await recover_user_events_durable(db, router, events, scopes)
        assert second.budget_exhausted is False
        assert first.recovered + second.recovered == eligible
        assert first.skipped_ineligible + second.skipped_ineligible == ineligible
        assert await _outbox_count(db) == eligible
        for scope in scopes:
            assert await load_scanned_seq(db, scope) == await events.max_seq(scope)
        dupes = await db.fetchall(
            "SELECT event_key, COUNT(*) AS n FROM notification_outbox"
            " GROUP BY event_key HAVING n > 1"
        )
        assert dupes == []

        # A settled sweep replays to zero rows: no doubles, no resends.
        third = await recover_user_events_durable(db, router, events, scopes)
        assert third.recovered == 0
        assert await _outbox_count(db) == eligible
    finally:
        await db.close()


async def test_first_enable_records_rollout_floor_and_keeps_old_rows_silent(
    tmp_path: Path,
) -> None:
    db = await _open_db(tmp_path / "c6-floor.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=101)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        t0 = datetime(2026, 9, 2, tzinfo=UTC)
        old_ms = int(t0.timestamp() * 1000) - 60_000
        for i in range(3):
            await _seed_closed(events, scope, f"old-{i}", recorded_ms=old_ms + i)
        router = _router(db)

        # First enable records both floors and notifies nothing: no blast.
        await recover_notification_outbox(db, router, now=t0, enable_user_event_delivery=True)
        assert await _outbox_count(db) == 0
        assert await SettingsRepo(db).get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY) == t0.isoformat()
        floor_key = USER_EVENT_DELIVERY_ROLLOUT_FLOOR_KEY
        assert await SettingsRepo(db).get_flag(floor_key) == t0.isoformat()

        # A command confirmed after the legacy floor still recovers as today.
        await CommandRepo(db).insert("new-command", "a1", None, "MARKET_OPEN", "{}", 1, None)
        cmd_time = datetime(2026, 9, 2, 0, 0, 30, tzinfo=UTC).isoformat()
        await db.execute_write(
            "UPDATE execution_commands SET status='CONFIRMED', created_at=?, updated_at=?"
            " WHERE command_key='new-command'",
            (cmd_time, cmd_time),
        )
        new_ms = int(t0.timestamp() * 1000) + 1_000
        for i in range(2):
            await _seed_closed(events, scope, f"new-{i}", recorded_ms=new_ms + i)

        t1 = datetime(2026, 9, 3, tzinfo=UTC)
        await recover_notification_outbox(db, router, now=t1, enable_user_event_delivery=True)
        rows = await db.fetchall("SELECT event_key, kind FROM notification_outbox ORDER BY id")
        fresh = await events.list_owned(scope, after_seq=3, limit=2)
        assert [row["event_key"] for row in rows] == [
            "command:new-command",
            f"user-event:{fresh[0].event.event_ref}",
            f"user-event:{fresh[1].event.event_ref}",
        ]
        # The rollout floor never moves; the legacy floor advances on success.
        assert await SettingsRepo(db).get_flag(floor_key) == t0.isoformat()
    finally:
        await db.close()


async def test_notification_recovery_uses_selected_network_environment(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-mainnet-scope.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=101)
        router = _router(db)
        t0 = datetime(2026, 9, 2, tzinfo=UTC)
        await recover_notification_outbox(
            db,
            router,
            now=t0,
            enable_user_event_delivery=True,
            environment="mainnet",
        )

        events = EventRepository(db)
        mainnet_scope = HistoryScope("a1", "mainnet")
        testnet_scope = HistoryScope("a1", "testnet")
        recorded_ms = int(t0.timestamp() * 1000) + 1_000
        await _seed_closed(events, mainnet_scope, "mainnet", recorded_ms=recorded_ms)
        await _seed_closed(events, testnet_scope, "testnet", recorded_ms=recorded_ms)

        await recover_notification_outbox(
            db,
            router,
            now=datetime(2026, 9, 3, tzinfo=UTC),
            enable_user_event_delivery=True,
            environment="mainnet",
        )

        rows = await db.fetchall("SELECT event_key FROM notification_outbox ORDER BY id")
        mainnet_events = await events.list_owned(mainnet_scope, limit=1)
        assert [row["event_key"] for row in rows] == [
            f"user-event:{mainnet_events[0].event.event_ref}"
        ]
        assert await load_scanned_seq(db, mainnet_scope) == await events.max_seq(mainnet_scope)
        assert await load_scanned_seq(db, testnet_scope) == 0
    finally:
        await db.close()


class _CaptureBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(
        self, *, chat_id: int, text: str, parse_mode: object = None, reply_markup: object = None
    ) -> dict:
        self.calls.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


def _production_settings(db: Database, *, trade_links: bool = True) -> Settings:
    return Settings(
        db_path=db.path,
        encryption_key="ab" * 32,
        miniapp_public_url="https://example.com",
        miniapp_enabled=trade_links,
    )


async def test_production_worker_without_trade_links_stays_text_only(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-gate.sqlite3")
    try:
        # Private chat (chat_id == user_id) so the links capability is the
        # only gate under test.
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        await NotificationOutboxRepo(db).enqueue(
            "evt-structured",
            1,
            1,
            "user.POSITION_CLOSED",
            "INFO",
            "Закрыта BTCUSDT",
            account_id="a1",
            trade_ref="ref-1",
            action="open_trade",
        )
        bot = _CaptureBot()
        worker = _build_worker(bot, db, _production_settings(db, trade_links=False))
        assert await worker.process_once() is True
        assert len(bot.calls) == 1
        assert bot.calls[0]["reply_markup"] is None
        row = await db.fetchone(
            "SELECT status, trade_ref, action FROM notification_outbox WHERE event_key=?",
            ("evt-structured",),
        )
        assert row is not None
        assert row["status"] == "SENT"
        assert row["trade_ref"] == "ref-1" and row["action"] == "open_trade"
    finally:
        await db.close()


async def test_trade_links_capability_enables_button(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-gate-on.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        await NotificationOutboxRepo(db).enqueue(
            "evt-structured",
            1,
            1,
            "user.POSITION_CLOSED",
            "INFO",
            "Закрыта BTCUSDT",
            account_id="a1",
            trade_ref="ref-1",
            action="open_trade",
        )
        bot = _CaptureBot()
        worker = TelegramOutboxWorker(
            bot=bot,
            db=db,
            per_chat_interval=0,
            webapp_base_url="https://example.com",
            trade_links_enabled=True,
        )
        assert await worker.process_once() is True
        assert len(bot.calls) == 1
        markup = bot.calls[0]["reply_markup"]
        assert markup is not None
        (button,) = markup.inline_keyboard[0]
        assert button.text == "Открыть сделку"
        assert button.web_app is not None and "ref-1" in button.web_app.url
    finally:
        await db.close()


class _StubEvents:
    """Feed double: one renderable row followed by an unrenderable one."""

    def __init__(self, rows: list[StoredEvent]) -> None:
        self._rows = rows

    async def max_seq(self, scope: HistoryScope) -> int:
        del scope
        return max(stored.seq for stored in self._rows)

    async def list_owned(
        self, scope: HistoryScope, *, after_seq: int = 0, limit: int = 50
    ) -> list[StoredEvent]:
        del scope
        return [stored for stored in self._rows if stored.seq > after_seq][:limit]


def _stored(seq: int, *, kind: str, eligible: bool = True) -> StoredEvent:
    base_ms = int(datetime(2026, 9, 4, tzinfo=UTC).timestamp() * 1000)
    return StoredEvent(
        scope=HistoryScope("a1", "testnet"),
        event=UserEvent(
            event_ref=f"c6-broken-{seq}",
            kind=kind,
            trade_ref="ref-1" if kind == "POSITION_CLOSED" else None,
            occurred_at=base_ms,
            recorded_at=base_ms,
            facts=dict(_CLOSED_FACTS) if kind == "POSITION_CLOSED" else {"symbol": "BTCUSDT"},
            dedup_key=f"c6-broken-{seq}",
            notify_eligible=eligible,
        ),
        seq=seq,
    )


async def test_unrenderable_row_blocks_without_silent_skip(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-block.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=101)
        router = _router(db)
        scope = HistoryScope("a1", "testnet")
        stub = _StubEvents([_stored(1, kind="POSITION_CLOSED"), _stored(2, kind="FUTURE_KIND")])

        first = await recover_user_events_durable(db, router, stub, [scope])  # type: ignore[arg-type]
        assert (first.recovered, first.blocked_unrenderable) == (1, 1)
        assert await load_scanned_seq(db, scope) == 1
        assert await _outbox_count(db) == 1

        # The next sweep retries from the broken row: still blocked, still one row.
        second = await recover_user_events_durable(db, router, stub, [scope])  # type: ignore[arg-type]
        assert (second.recovered, second.blocked_unrenderable) == (0, 1)
        assert await load_scanned_seq(db, scope) == 1
        assert await _outbox_count(db) == 1
    finally:
        await db.close()


async def test_console_only_sweep_advances_without_enqueueing(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-console.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=101)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 5, tzinfo=UTC).timestamp() * 1000)
        for i in range(2):
            await _seed_closed(events, scope, f"muted-{i}", recorded_ms=base_ms + i)

        muted = await recover_user_events_durable(db, None, events, [scope])
        assert (muted.recovered, muted.skipped_no_recipient) == (0, 2)
        assert await _outbox_count(db) == 0
        assert await load_scanned_seq(db, scope) == await events.max_seq(scope)

        # Enabling later notifies only rows recorded after the muted window.
        for i in range(2):
            await _seed_closed(events, scope, f"live-{i}", recorded_ms=base_ms + 100 + i)
        live = await recover_user_events_durable(db, _router(db), events, [scope])
        assert live.recovered == 2
        assert await _outbox_count(db) == 2
    finally:
        await db.close()


async def test_durable_sweep_skips_foreign_and_empty_scopes(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-scopes.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=101)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 6, tzinfo=UTC).timestamp() * 1000)
        await _seed_closed(events, scope, "kept", recorded_ms=base_ms)
        router = _router(db)

        empty = await recover_user_events_durable(db, router, events, [])
        assert empty.recovered == 0
        foreign = await recover_user_events_durable(
            db,
            router,
            events,
            ["not-a-scope"],  # type: ignore[list-item]
        )
        assert (foreign.recovered, foreign.skipped_no_recipient) == (0, 0)
        assert await _outbox_count(db) == 0
        assert await load_scanned_seq(db, scope) == 0
    finally:
        await db.close()


async def test_durable_sweep_rejects_bad_bounds(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-bounds.sqlite3")
    try:
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        with pytest.raises(ValueError):
            await recover_user_events_durable(db, _router(db), events, [scope], rollout_floor_ms=-1)
        with pytest.raises(ValueError):
            await recover_user_events_durable(db, _router(db), events, [scope], budget=-1)
        with pytest.raises(ValueError):
            await load_scanned_seq(db, "not-a-scope")  # type: ignore[arg-type]
    finally:
        await db.close()


async def test_commit_crash_before_and_after_atomic_commit_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbox insert and cursor advance commit atomically (C6)."""
    import copytradeaster.notifications.delivery as delivery

    db = await _open_db(tmp_path / "c6-commit-crash.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 8, tzinfo=UTC).timestamp() * 1000)
        for i in range(3):
            await _seed_closed(events, scope, f"crash-{i}", recorded_ms=base_ms + i)

        original = delivery._commit_batch

        async def failing_commit(db_, scope_, due_rows, last_seq):
            del db_, scope_, due_rows, last_seq
            raise RuntimeError("crash before commit")

        monkeypatch.setattr(delivery, "_commit_batch", failing_commit)
        with pytest.raises(RuntimeError, match="crash before commit"):
            await recover_user_events_durable(db, _router(db), events, [scope])
        # Nothing half-committed: no rows, cursor still before the batch.
        assert await _outbox_count(db) == 0
        assert await load_scanned_seq(db, scope) == 0

        # The replay after the crash commits once and only once.
        monkeypatch.setattr(delivery, "_commit_batch", original)
        report = await recover_user_events_durable(db, _router(db), events, [scope])
        assert (report.recovered, await _outbox_count(db)) == (3, 3)
        assert await load_scanned_seq(db, scope) == await events.max_seq(scope)

        # A crash after commit replays the saved cursor: no doubles.
        replay = await recover_user_events_durable(db, _router(db), events, [scope])
        assert replay.recovered == 0
        assert await _outbox_count(db) == 3
    finally:
        await db.close()


async def test_toggle_fence_skips_events_recorded_while_disabled(tmp_path: Path) -> None:
    """Disable -> record -> enable must not deliver the muted gap event."""
    from copytradeaster.notifications.delivery import ensure_user_event_rollout_floor

    db = await _open_db(tmp_path / "c6-toggle-fence.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 9, tzinfo=UTC).timestamp() * 1000)
        users = TelegramUserRepo(db)
        router = _router(db)
        await ensure_user_event_rollout_floor(db, now=datetime(2026, 9, 9, tzinfo=UTC))

        assert await users.set_notifications_enabled(1, False) is True
        await _seed_closed(events, scope, "muted", recorded_ms=base_ms)
        assert await users.set_notifications_enabled(1, True) is True

        report = await recover_user_events_durable(db, router, events, [scope])
        assert (report.recovered, await _outbox_count(db)) == (0, 0)

        # A fact recorded after re-enable still arrives.
        await _seed_closed(events, scope, "live", recorded_ms=base_ms + 1_000)
        live = await recover_user_events_durable(db, router, events, [scope])
        assert (live.recovered, await _outbox_count(db)) == (1, 1)
    finally:
        await db.close()


async def test_toggle_between_prepare_and_commit_skips_muted_rows(tmp_path: Path) -> None:
    """A sweep admitted before a disable must not insert muted rows."""
    from copytradeaster.notifications.delivery import ensure_user_event_rollout_floor

    db = await _open_db(tmp_path / "c6-toggle-mid.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 9, tzinfo=UTC).timestamp() * 1000)
        users = TelegramUserRepo(db)
        router = _router(db)
        await ensure_user_event_rollout_floor(db, now=datetime(2026, 9, 9, tzinfo=UTC))
        await _seed_closed(events, scope, "race", recorded_ms=base_ms)

        original = router.prepare_user_event_notification

        async def prepare_then_toggle(stored):
            planned = await original(stored)
            await users.set_notifications_enabled(1, False)
            return planned

        router.prepare_user_event_notification = prepare_then_toggle  # type: ignore[method-assign]
        report = await recover_user_events_durable(db, router, events, [scope])
        # Planned before the toggle; the commit rechecks the recipient, so
        # nothing lands and the cursor still advances past the muted row.
        assert (report.recovered, await _outbox_count(db)) == (1, 0)
        assert await load_scanned_seq(db, scope) == await events.max_seq(scope)

        # Disable -> enable between prepare and commit: the fence recorded at
        # the toggle covers the row, so the late commit inserts nothing.
        await users.set_notifications_enabled(1, True)
        await _seed_closed(events, scope, "race-2", recorded_ms=base_ms + 1_000)
        router.prepare_user_event_notification = original  # type: ignore[method-assign]

        async def prepare_then_cycle(stored):
            planned = await original(stored)
            await users.set_notifications_enabled(1, False)
            await users.set_notifications_enabled(1, True)
            return planned

        router.prepare_user_event_notification = prepare_then_cycle  # type: ignore[method-assign]
        second = await recover_user_events_durable(db, router, events, [scope])
        assert (second.recovered, await _outbox_count(db)) == (1, 0)
    finally:
        await db.close()


async def test_noop_enable_keeps_pending_events_deliverable(tmp_path: Path) -> None:
    """A no-op enable=True must not move the fence and eat new events."""
    from copytradeaster.notifications.delivery import ensure_user_event_rollout_floor

    db = await _open_db(tmp_path / "c6-noop-enable.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 9, tzinfo=UTC).timestamp() * 1000)
        users = TelegramUserRepo(db)
        await ensure_user_event_rollout_floor(db, now=datetime(2026, 9, 9, tzinfo=UTC))
        await _seed_closed(events, scope, "waiting", recorded_ms=base_ms)

        # Already enabled: the toggle is a no-op and must not cut history.
        assert await users.set_notifications_enabled(1, True) is True
        assert await load_scanned_seq(db, scope) == 0

        report = await recover_user_events_durable(db, _router(db), events, [scope])
        assert (report.recovered, await _outbox_count(db)) == (1, 1)
    finally:
        await db.close()


async def test_disable_marks_pending_outbox_recipient_disabled(tmp_path: Path) -> None:
    db = await _open_db(tmp_path / "c6-disable-outbox.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        await NotificationOutboxRepo(db).enqueue(
            "evt-pending",
            1,
            1,
            "user.POSITION_CLOSED",
            "INFO",
            "Закрыта BTCUSDT",
            account_id="a1",
            trade_ref="ref-1",
            action="open_trade",
        )
        assert await TelegramUserRepo(db).set_notifications_enabled(1, False) is True
        row = await db.fetchone(
            "SELECT status, last_error FROM notification_outbox WHERE event_key='evt-pending'"
        )
        assert row is not None
        assert (row["status"], row["last_error"]) == ("FAILED", "recipient_disabled")
    finally:
        await db.close()


async def test_commit_batch_keeps_cursor_monotonic(tmp_path: Path) -> None:
    """The cursor only moves forward through MAX, never backwards."""
    import copytradeaster.notifications.delivery as delivery
    from copytradeaster.notifications.delivery import ensure_user_event_rollout_floor

    db = await _open_db(tmp_path / "c6-monotonic.sqlite3")
    try:
        await _setup_owner(db, account_id="a1", user_id=1, chat_id=1)
        events = EventRepository(db)
        scope = HistoryScope("a1", "testnet")
        base_ms = int(datetime(2026, 9, 9, tzinfo=UTC).timestamp() * 1000)
        await ensure_user_event_rollout_floor(db, now=datetime(2026, 9, 9, tzinfo=UTC))
        await _seed_closed(events, scope, "mono", recorded_ms=base_ms)
        await recover_user_events_durable(db, _router(db), events, [scope])
        high = await load_scanned_seq(db, scope)
        assert high > 0

        await delivery._commit_batch(db, scope, [], 1)
        assert await load_scanned_seq(db, scope) == high
    finally:
        await db.close()
