import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from copytradeaster.notifications.models import NotificationEvent, NotificationSeverity
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.notifications.worker import TelegramOutboxWorker
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    NotificationOutboxRepo,
    TelegramUserRepo,
)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "notifications.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


class FakeBot:
    def __init__(self, action=None):
        self.action = action
        self.calls: list[tuple[int, str, object]] = []
        self.active = 0
        self.max_active = 0

    async def send_message(self, *, chat_id: int, text: str, parse_mode=None):
        self.calls.append((chat_id, text, parse_mode))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.action is not None:
                return await self.action(chat_id, text, parse_mode)
            return {"chat_id": chat_id}
        finally:
            self.active -= 1


def telegram_error(error_type, message="fake", retry_after=3):
    method = SendMessage(chat_id=1, text="notification")
    if error_type is TelegramRetryAfter:
        return error_type(method, message, retry_after)
    return error_type(method, message)


def event(
    key="event-1",
    *,
    account_id="a1",
    severity=NotificationSeverity.INFO,
    text="safe notification",
):
    return NotificationEvent(
        event_key=key,
        account_id=account_id,
        kind="status",
        severity=severity,
        text=text,
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
    )


async def setup_account_owner(db, *, account_id="a1", user_id=1, chat_id=101):
    await AccountRepo(db).upsert(account_id, "follower", account_id, f"0x{account_id:0<4}")
    await TelegramUserRepo(db).upsert(user_id, chat_id, account_id=account_id)


async def test_account_and_global_routing_with_critical_admins(db):
    await setup_account_owner(db)
    users = TelegramUserRepo(db)
    await users.upsert(10, 110, role="ADMIN")
    await users.upsert(11, 111, role="ADMIN")
    router = NotificationRouter(db, admin_ids=[10, 11], console=lambda _event: asyncio.sleep(0))

    await router.emit(event("normal"))
    await router.emit(event("critical", severity=NotificationSeverity.CRITICAL))
    await router.emit(event("global", account_id=None))

    rows = await db.fetchall(
        "SELECT * FROM notification_outbox ORDER BY event_key, telegram_user_id"
    )
    routed = {(row["event_key"], row["telegram_user_id"]) for row in rows}
    assert routed == {
        ("normal", 1),
        ("critical", 1),
        ("critical", 10),
        ("critical", 11),
        ("global", 10),
        ("global", 11),
    }


async def test_owner_who_is_also_configured_admin_gets_one_critical_row(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, admin_ids=[1], console=lambda _event: asyncio.sleep(0))

    await router.emit(event("owner-admin", severity=NotificationSeverity.CRITICAL))

    rows = await db.fetchall(
        "SELECT telegram_user_id FROM notification_outbox WHERE event_key='owner-admin'"
    )
    assert [row["telegram_user_id"] for row in rows] == [1]


async def test_disabled_or_blocked_recipients_are_not_enqueued(db):
    await setup_account_owner(db)
    users = TelegramUserRepo(db)
    await users.upsert(2, 102, role="ADMIN")
    await users.set_notifications_enabled(1, False)
    await users.disable_notifications(2)

    router = NotificationRouter(
        db,
        admin_ids=[2],
        console=lambda _event: asyncio.sleep(0),
    )
    await router.emit(event("disabled-owner"))
    await router.emit(event("disabled-global", account_id=None))

    count = await db.fetchone("SELECT COUNT(*) AS count FROM notification_outbox")
    assert count["count"] == 0


async def test_duplicate_event_is_one_row_per_recipient(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))

    await router.emit(event("duplicate"))
    await router.emit(event("duplicate", text="second delivery attempt"))

    count = await db.fetchone(
        "SELECT COUNT(*) AS count FROM notification_outbox WHERE event_key='duplicate'"
    )
    row = await db.fetchone("SELECT text FROM notification_outbox WHERE event_key='duplicate'")
    assert count["count"] == 1
    assert row["text"] == "safe notification"


async def test_retry_after_reschedules_without_terminal_failure(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event())

    async def retry_after(_chat_id, _text, _parse_mode):
        raise telegram_error(TelegramRetryAfter, retry_after=4)

    worker = TelegramOutboxWorker(
        bot=FakeBot(retry_after),
        db=db,
        per_chat_interval=0,
        retry_jitter=0,
    )
    assert await worker.process_once()
    row = await db.fetchone("SELECT * FROM notification_outbox WHERE event_key='event-1'")
    assert row["status"] == "PENDING"
    assert row["last_error"] == "telegram_retry_after"
    assert datetime.fromisoformat(row["available_at"]) > datetime.now(UTC)


async def test_network_retry_is_bounded_and_bad_request_is_terminal(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("network", text="network"))
    await router.emit(event("bad", text="bad"))

    async def sender(_chat_id, text, _parse_mode):
        if text == "network":
            raise telegram_error(TelegramNetworkError)
        raise telegram_error(TelegramBadRequest)

    worker = TelegramOutboxWorker(
        bot=FakeBot(sender),
        db=db,
        max_attempts=2,
        retry_base=0,
        retry_jitter=0,
        per_chat_interval=0,
    )
    # event order is deterministic by id; process each row twice so network
    # reaches its bounded terminal attempt and bad request is immediate.
    await worker.process_once()
    await worker.process_once()
    await worker.process_once()
    network = await db.fetchone(
        "SELECT status, last_error FROM notification_outbox WHERE event_key='network'"
    )
    bad = await db.fetchone(
        "SELECT status, last_error FROM notification_outbox WHERE event_key='bad'"
    )
    assert network["status"] == "FAILED"
    assert network["last_error"] == "transport_error"
    assert bad["status"] == "FAILED"
    assert bad["last_error"] == "telegram_bad_request"


async def test_forbidden_disables_only_one_recipient_and_worker_continues(db):
    await setup_account_owner(db, account_id="a1", user_id=1, chat_id=101)
    await AccountRepo(db).upsert("a2", "follower", "a2", "0xa2")
    await TelegramUserRepo(db).upsert(2, 102, account_id="a2")
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("blocked", account_id="a1"))
    await router.emit(event("healthy", account_id="a2"))

    async def sender(chat_id, _text, _parse_mode):
        if chat_id == 101:
            raise telegram_error(TelegramForbiddenError)

    worker = TelegramOutboxWorker(
        bot=FakeBot(sender),
        db=db,
        per_chat_interval=0,
    )
    await worker.process_once()
    await worker.process_once()
    blocked = await db.fetchone("SELECT status FROM notification_outbox WHERE event_key='blocked'")
    healthy = await db.fetchone("SELECT status FROM notification_outbox WHERE event_key='healthy'")
    user = await TelegramUserRepo(db).get(1)
    other = await TelegramUserRepo(db).get(2)
    assert blocked["status"] == "FAILED"
    assert healthy["status"] == "SENT"
    assert user["notifications_enabled"] == 0
    assert other["notifications_enabled"] == 1


async def test_worker_sends_plain_text_bounded_and_parse_mode_none(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("long", text="x" * 5000))
    bot = FakeBot()
    worker = TelegramOutboxWorker(bot=bot, db=db, per_chat_interval=0)

    await worker.process_once()

    assert len(bot.calls) == 1
    assert len(bot.calls[0][1]) == 4096
    assert bot.calls[0][2] is None


async def test_same_chat_is_serialized_and_paced(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("paced-1", text="first"))
    await router.emit(event("paced-2", text="second"))
    sent_at: list[float] = []

    async def sender(_chat_id, _text, _parse_mode):
        sent_at.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.005)

    worker = TelegramOutboxWorker(
        bot=FakeBot(sender),
        db=db,
        concurrency=2,
        per_chat_interval=0.02,
        poll_interval=0.001,
    )
    await worker.start()
    try:
        for _ in range(500):
            counts = await worker.status()
            if counts["PENDING"] == 0 and counts["SENDING"] == 0:
                break
            await asyncio.sleep(0.002)
    finally:
        await worker.stop(timeout=2)

    assert len(sent_at) == 2
    assert sent_at[1] - sent_at[0] >= 0.018


async def test_recipient_outbox_summary_has_counts_and_last_sent_only(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("summary-1", text="first"))
    await router.emit(event("summary-2", text="second"))

    worker = TelegramOutboxWorker(bot=FakeBot(), db=db, per_chat_interval=0)
    await worker.process_once()

    summary = await NotificationOutboxRepo(db).counts_for_user(1)
    assert summary["PENDING"] == 1
    assert summary["SENT"] == 1
    assert summary["TOTAL"] == 2
    assert isinstance(summary["last_sent_at"], str)
    assert "text" not in summary
    assert await NotificationOutboxRepo(db).counts_for_user(999) == {
        "PENDING": 0,
        "SENDING": 0,
        "SENT": 0,
        "FAILED": 0,
        "last_sent_at": None,
        "TOTAL": 0,
    }


async def test_worker_start_stop_is_concurrent_idempotent(db):
    worker = TelegramOutboxWorker(
        bot=FakeBot(),
        db=db,
        concurrency=2,
        poll_interval=0.001,
        per_chat_interval=0,
    )

    await asyncio.gather(worker.start(), worker.start())
    assert (await worker.status())["WORKER_TASKS"] == 2
    await asyncio.gather(worker.stop(timeout=1), worker.stop(timeout=1))
    status = await worker.status()
    assert status["STARTED"] == 0
    assert status["WORKER_TASKS"] == 0

    await worker.start()
    assert (await worker.status())["WORKER_TASKS"] == 2
    await worker.stop(timeout=1)


async def test_stop_timeout_leaves_claimed_row_recoverable_by_lease(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("cancelled"))
    blocked = asyncio.Event()

    async def sender(_chat_id, _text, _parse_mode):
        await blocked.wait()

    worker = TelegramOutboxWorker(
        bot=FakeBot(sender),
        db=db,
        concurrency=1,
        poll_interval=0.001,
        lease_seconds=1,
        per_chat_interval=0,
    )
    await worker.start()
    for _ in range(100):
        if (await worker.status())["SENDING"] == 1:
            break
        await asyncio.sleep(0.002)
    assert (await worker.status())["SENDING"] == 1

    await worker.stop(timeout=0.01)
    row = await db.fetchone("SELECT status FROM notification_outbox WHERE event_key='cancelled'")
    assert row["status"] == "SENDING"

    recovery_now = datetime.now(UTC) + timedelta(seconds=2)
    outbox = NotificationOutboxRepo(db)
    await outbox.recover_expired(now=recovery_now)
    recovered = await outbox.claim_one("recovery", now=recovery_now)
    assert recovered is not None
    assert recovered["status"] == "SENDING"
    assert await outbox.fail(recovered["id"], "test", worker_id="recovery")


async def test_restart_recovers_pending_and_expired_sending(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("restart"))
    outbox = NotificationOutboxRepo(db)
    row = await outbox.claim_one("crashed", lease_seconds=30)
    assert row is not None
    await db.execute_write(
        "UPDATE notification_outbox SET lease_until=?, status='SENDING' WHERE id=?",
        ("2020-01-01T00:00:00+00:00", row["id"]),
    )

    bot = FakeBot()
    worker = TelegramOutboxWorker(bot=bot, db=db, per_chat_interval=0)
    assert await worker.process_once()
    final = await db.fetchone("SELECT status FROM notification_outbox WHERE id=?", (row["id"],))
    assert final["status"] == "SENT"


async def test_two_workers_atomic_claim_one_row(db):
    await setup_account_owner(db)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("one-row"))
    calls = 0

    async def sender(_chat_id, _text, _parse_mode):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)

    worker1 = TelegramOutboxWorker(bot=FakeBot(sender), db=db, worker_id="w1", per_chat_interval=0)
    worker2 = TelegramOutboxWorker(bot=FakeBot(sender), db=db, worker_id="w2", per_chat_interval=0)
    await asyncio.gather(worker1.process_once(), worker2.process_once())

    assert calls == 1
    row = await db.fetchone("SELECT status FROM notification_outbox WHERE event_key='one-row'")
    assert row["status"] == "SENT"


async def test_hundred_recipient_burst_is_bounded_by_worker_tasks(db):
    users = TelegramUserRepo(db)
    for user_id in range(100):
        await users.upsert(user_id + 1, 10_000 + user_id, role="ADMIN")
    router = NotificationRouter(
        db,
        admin_ids=range(1, 101),
        console=lambda _event: asyncio.sleep(0),
    )
    await router.emit(event("burst", account_id=None))
    bot = FakeBot()
    worker = TelegramOutboxWorker(
        bot=bot,
        db=db,
        concurrency=4,
        per_chat_interval=0,
        poll_interval=0.001,
    )
    await worker.start()
    try:
        for _ in range(500):
            counts = await worker.status()
            if counts["PENDING"] == 0 and counts["SENDING"] == 0:
                break
            await asyncio.sleep(0.002)
    finally:
        await worker.stop(timeout=2)
    assert len(bot.calls) == 100
    assert bot.max_active <= 4


async def test_confirmed_command_recovery_is_safe_and_idempotent(db):
    await setup_account_owner(db)
    commands = CommandRepo(db)
    await commands.insert(
        "cmd-1",
        "a1",
        None,
        "MARKET_OPEN",
        '{"private_key":"must-not-leak"}',
        1,
        "client-1",
    )
    await commands.set_status("cmd-1", "CONFIRMED", last_error="raw exchange error")
    await commands.insert(
        "cmd-old",
        "a1",
        None,
        "OLD_COMMAND",
        '{"payload":"old"}',
        1,
        "client-old",
    )
    await commands.set_status("cmd-old", "CONFIRMED")
    await db.execute_write(
        "UPDATE execution_commands SET updated_at=? WHERE command_key=?",
        ("2020-01-01T00:00:00+00:00", "cmd-old"),
    )
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    assert [row["command_key"] for row in await commands.confirmed(since=cutoff)] == ["cmd-1"]
    assert await router.recover_confirmed_commands(commands, since=cutoff) == 1
    assert await router.recover_confirmed_commands(commands, since=cutoff) == 0
    row = await db.fetchone("SELECT * FROM notification_outbox WHERE event_key='command:cmd-1'")
    assert row is not None
    assert (
        await db.fetchone("SELECT 1 FROM notification_outbox WHERE event_key='command:cmd-old'")
        is None
    )
    assert "private_key" not in row["text"]
    assert "raw exchange error" not in row["text"]
    assert "Открыта позиция" in row["text"]


async def test_router_enqueue_does_not_call_bot_sender(db):
    await setup_account_owner(db)

    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    await router.emit(event("outage"))
    # The router has no Bot dependency at all; enqueue is complete while the
    # eventual sender remains untouched.
    assert (await router.backlog())["PENDING"] == 1


async def _seed_user_event(db, *, kind="POSITION_CLOSED", eligible=True, recorded_ms=None):
    from copytradeaster.storage.events import EventRepository
    from copytradeaster.storage.history import HistoryScope

    scope = HistoryScope("a1", "testnet")
    base_ms = int(datetime(2026, 8, 23, tzinfo=UTC).timestamp() * 1000)
    facts = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": "62400",
        "exit_price": "64100",
        "realized_gross": "18.42",
        "duration_ms": 8_280_000,
    }
    return await EventRepository(db).record(
        scope,
        kind=kind,
        occurred_at=base_ms,
        recorded_at=base_ms if recorded_ms is None else recorded_ms,
        facts=facts,
        dedup_key=f"evt:test:{kind}:{eligible}:{recorded_ms}",
        trade_ref="ref-1",
        notify_eligible=eligible,
    )


async def test_recover_user_events_emits_only_eligible_with_trade_link(db):
    await setup_account_owner(db)
    from copytradeaster.storage.history import HistoryScope

    floor_ms = int(datetime(2026, 8, 23, tzinfo=UTC).timestamp() * 1000)
    await _seed_user_event(db, recorded_ms=floor_ms + 1)
    await _seed_user_event(db, eligible=False, recorded_ms=floor_ms + 2)
    await _seed_user_event(db, recorded_ms=floor_ms - 1)

    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    from copytradeaster.storage.events import EventRepository

    recovered = await router.recover_user_events(
        EventRepository(db), [HistoryScope("a1", "testnet")], since_ms=floor_ms
    )
    assert recovered == 1
    row = await db.fetchone("SELECT * FROM notification_outbox")
    assert row["kind"] == "user.POSITION_CLOSED"
    assert row["trade_ref"] == "ref-1" and row["action"] == "open_trade"
    assert "Закрыта BTCUSDT · LONG" in row["text"]
    assert "Итог сделки: 18,42 USDT до комиссий" in row["text"]
    # Replay dedupes per recipient: no second row for the same stored event.
    assert (
        await router.recover_user_events(
            EventRepository(db), [HistoryScope("a1", "testnet")], since_ms=floor_ms
        )
        == 1
    )
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM notification_outbox"))["n"] == 1


async def test_recover_user_events_pages_past_first_two_hundred(db):
    await setup_account_owner(db)
    from copytradeaster.storage.events import EventRepository
    from copytradeaster.storage.history import HistoryScope

    scope = HistoryScope("a1", "testnet")
    floor_ms = int(datetime(2026, 8, 23, tzinfo=UTC).timestamp() * 1000)
    facts = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": "62400",
        "exit_price": "64100",
        "realized_gross": "18.42",
        "duration_ms": 8_280_000,
    }
    events = EventRepository(db)
    for i in range(204):
        await events.record(
            scope,
            kind="POSITION_CLOSED",
            occurred_at=floor_ms - 10_000 + i,
            recorded_at=floor_ms - 10_000 + i,
            facts=facts,
            dedup_key=f"evt:page:{i}",
            trade_ref="ref-old",
            notify_eligible=True,
        )
    await events.record(
        scope,
        kind="POSITION_CLOSED",
        occurred_at=floor_ms + 1,
        recorded_at=floor_ms + 1,
        facts=facts,
        dedup_key="evt:page:fresh",
        trade_ref="ref-1",
        notify_eligible=True,
    )
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    assert await router.recover_user_events(events, [scope], since_ms=floor_ms) == 1
    row = await db.fetchone("SELECT * FROM notification_outbox")
    assert row["trade_ref"] == "ref-1"


async def test_recover_user_events_skips_foreign_and_invalid_scopes(db):
    await setup_account_owner(db)
    from copytradeaster.storage.events import EventRepository
    from copytradeaster.storage.history import HistoryScope

    floor_ms = int(datetime(2026, 8, 23, tzinfo=UTC).timestamp() * 1000)
    await _seed_user_event(db, recorded_ms=floor_ms + 1)
    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    events = EventRepository(db)
    assert await router.recover_user_events(events, ["not-a-scope"], since_ms=floor_ms) == 0
    assert await router.recover_user_events(events, [], since_ms=floor_ms) == 0
    with pytest.raises(ValueError):
        await router.recover_user_events(events, [HistoryScope("a1", "testnet")], since_ms=-1)


async def test_recover_user_events_skips_unrenderable_rows(db):
    await setup_account_owner(db)
    from copytradeaster.storage.events import StoredEvent, UserEvent
    from copytradeaster.storage.history import HistoryScope

    broken = StoredEvent(
        scope=HistoryScope("a1", "testnet"),
        event=UserEvent(
            event_ref="broken",
            kind="FUTURE_KIND",
            trade_ref=None,
            occurred_at=1,
            recorded_at=2,
            facts={"symbol": "BTCUSDT"},
            dedup_key="broken",
            notify_eligible=True,
        ),
        seq=1,
    )

    class StubEvents:
        async def list_owned(self, scope, **kwargs):
            del scope
            return [] if kwargs.get("after_seq", 0) >= 1 else [broken]

    router = NotificationRouter(db, console=lambda _event: asyncio.sleep(0))
    scopes = [HistoryScope("a1", "testnet")]
    result = await router.recover_user_events(StubEvents(), scopes, since_ms=0)  # type: ignore[arg-type]
    assert result == 0
    assert (await router.backlog())["PENDING"] == 0


def test_trade_keyboard_needs_proven_ref_known_action_and_base_url():
    from copytradeaster.notifications.worker import trade_keyboard

    assert trade_keyboard(None, None, None) is None
    assert trade_keyboard("ref-1", None, "https://example.com") is None
    assert trade_keyboard("ref-1", "bogus", "https://example.com") is None
    assert trade_keyboard("", "open_trade", "https://example.com") is None
    assert trade_keyboard("ref-1", "open_trade", None) is None
    markup = trade_keyboard("ref-1", "open_trade", "https://example.com/")
    assert markup is not None
    (button,) = markup.inline_keyboard[0]
    assert button.text == "Открыть сделку"
    assert button.web_app is not None and "ref-1" in button.web_app.url


async def test_worker_attaches_button_only_for_structured_rows(db):
    await setup_account_owner(db)
    from copytradeaster.storage.repositories import NotificationOutboxRepo

    seen: list[dict] = []

    class MarkupBot:
        async def send_message(self, *, chat_id, text, parse_mode=None, reply_markup=None):
            seen.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
            return {"chat_id": chat_id}

    outbox = NotificationOutboxRepo(db)
    await outbox.enqueue(
        "evt-structured",
        1,
        101,
        "user.POSITION_CLOSED",
        "INFO",
        "Закрыта BTCUSDT",
        account_id="a1",
        trade_ref="ref-1",
        action="open_trade",
    )
    await outbox.enqueue(
        "evt-legacy", 1, 101, "execution.confirmed", "INFO", "legacy text", account_id="a1"
    )
    from copytradeaster.notifications.worker import TelegramOutboxWorker

    worker = TelegramOutboxWorker(
        bot=MarkupBot(), db=db, per_chat_interval=0, webapp_base_url="https://example.com"
    )
    assert await worker.process_once()
    assert await worker.process_once()
    by_key = {call["text"]: call for call in seen}
    assert by_key["Закрыта BTCUSDT"]["reply_markup"] is not None
    assert by_key["legacy text"]["reply_markup"] is None


async def test_prune_terminal_removes_only_old_terminal_rows(db):
    from datetime import timedelta

    from copytradeaster.storage.repositories import NotificationOutboxRepo

    await setup_account_owner(db)
    outbox = NotificationOutboxRepo(db)
    await outbox.enqueue("old-sent", 1, 101, "k", "INFO", "old", account_id="a1")
    await outbox.enqueue("new-sent", 1, 101, "k", "INFO", "new", account_id="a1")
    await outbox.enqueue("pending", 1, 101, "k", "INFO", "wait", account_id="a1")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.execute_write(
        "UPDATE notification_outbox SET status='SENT' WHERE event_key='old-sent'"
    )
    await db.execute_write(
        "UPDATE notification_outbox SET status='SENT', updated_at=? WHERE event_key='old-sent'",
        (old,),
    )
    await db.execute_write(
        "UPDATE notification_outbox SET status='SENT' WHERE event_key='new-sent'"
    )

    removed = await outbox.prune_terminal(before=datetime.now(UTC) - timedelta(days=7))
    assert removed == 1
    remaining = {
        row["event_key"] for row in await db.fetchall("SELECT event_key FROM notification_outbox")
    }
    assert remaining == {"new-sent", "pending"}
    with pytest.raises(ValueError):
        await outbox.prune_terminal(before=datetime.now(UTC), statuses=("PENDING",))
    with pytest.raises(ValueError):
        await outbox.prune_terminal(before=datetime.now(UTC), limit=0)


async def test_prune_terminal_applies_failed_thirty_day_floor(db):
    from datetime import timedelta

    from copytradeaster.storage.repositories import NotificationOutboxRepo

    await setup_account_owner(db)
    outbox = NotificationOutboxRepo(db)
    now = datetime.now(UTC)
    for key, status, age_days in (
        ("failed-young", "FAILED", 10),
        ("failed-old", "FAILED", 40),
        ("sent-old", "SENT", 8),
    ):
        await outbox.enqueue(key, 1, 101, "k", "INFO", key, account_id="a1")
        await db.execute_write(
            "UPDATE notification_outbox SET status=?, updated_at=? WHERE event_key=?",
            (status, (now - timedelta(days=age_days)).isoformat(), key),
        )
    removed = await outbox.prune_terminal(before=now - timedelta(days=30), statuses=("FAILED",))
    assert removed == 1
    remaining = {
        row["event_key"] for row in await db.fetchall("SELECT event_key FROM notification_outbox")
    }
    assert remaining == {"failed-young", "sent-old"}
    removed = await outbox.prune_terminal(before=now - timedelta(days=7), statuses=("SENT",))
    assert removed == 1
    remaining = {
        row["event_key"] for row in await db.fetchall("SELECT event_key FROM notification_outbox")
    }
    assert remaining == {"failed-young"}


async def test_worker_start_prunes_old_terminal_rows(db):
    from datetime import timedelta

    from copytradeaster.notifications.worker import TelegramOutboxWorker
    from copytradeaster.storage.repositories import NotificationOutboxRepo

    await setup_account_owner(db)
    outbox = NotificationOutboxRepo(db)
    await outbox.enqueue("stale-sent", 1, 101, "k", "INFO", "stale", account_id="a1")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.execute_write(
        "UPDATE notification_outbox SET status='SENT' WHERE event_key='stale-sent'"
    )
    await db.execute_write(
        "UPDATE notification_outbox SET status='SENT', updated_at=? WHERE event_key='stale-sent'",
        (old,),
    )
    worker = TelegramOutboxWorker(bot=FakeBot(), db=db, per_chat_interval=0, concurrency=1)
    await worker.start()
    try:
        assert (
            await db.fetchone(
                "SELECT 1 AS one FROM notification_outbox WHERE event_key='stale-sent'"
            )
            is None
        )
    finally:
        await worker.stop()
