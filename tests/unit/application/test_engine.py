"""Offline lifecycle tests for the bounded Stage 2 process."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from copytradeaster.application.engine import (
    NOTIFICATION_RECOVERY_FLOOR_KEY,
    Stage2BootstrapError,
    Stage2Process,
    _as_utc,
    _build_app,
    _build_application,
    _build_worker,
    _parse_floor,
    bootstrap_telegram_admins,
    build_stage2_process,
    recover_notification_outbox,
)
from copytradeaster.config import Settings
from copytradeaster.notifications.console import ConsoleNotifier
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    NotificationOutboxRepo,
    SettingsRepo,
    TelegramUserRepo,
)


class FakeApp:
    def __init__(self, db: Database, trace: list[str], *, fail_start: bool = False) -> None:
        self.db = db
        self.trace = trace
        self.stop_event = asyncio.Event()
        self.fail_start = fail_start

    async def start(self) -> None:
        self.trace.append("app.start")
        if self.fail_start:
            raise RuntimeError("app start failed")

    async def stop(self) -> None:
        self.trace.append("app.stop")
        await self.db.close()


class FakeTelegram:
    def __init__(self, trace: list[str], *, fail_polling: bool = False) -> None:
        self.trace = trace
        self.bot = object()
        self.fail_polling = fail_polling
        self.close_calls = 0

    async def run(self, stop_event: asyncio.Event, *, close_session: bool = False) -> None:
        assert close_session is False
        self.trace.append("telegram.run")
        if self.fail_polling:
            raise RuntimeError("polling failed")
        stop_event.set()

    async def shutdown(self, *, close_session: bool = False) -> None:
        assert close_session is False
        self.trace.append("telegram.shutdown")

    async def close_session(self) -> None:
        self.close_calls += 1
        self.trace.append("bot.close")


class FakeWorker:
    def __init__(self, bot: object, trace: list[str], *, fail_stop: bool = False) -> None:
        self.bot = bot
        self.trace = trace
        self.fail_stop = fail_stop

    async def start(self) -> None:
        self.trace.append("worker.start")

    async def stop(self, *, drain: bool = False, timeout: float | None = None) -> None:
        del timeout
        self.trace.append(f"worker.stop:{drain}")
        if self.fail_stop:
            raise RuntimeError("worker stop failed")


def make_settings(
    path: Path,
    *,
    admin_ids: list[int] | None = None,
    notifications: bool = True,
) -> Settings:
    return Settings(
        db_path=path,
        encryption_key="ab" * 32,
        telegram_admin_ids=admin_ids or [],
        telegram_notifications_enabled=notifications,
        telegram_bot_token="123:token",
        miniapp_enabled=False,
    )


def factories(
    trace: list[str],
    *,
    fail_app: bool = False,
    fail_polling: bool = False,
    fail_worker_stop: bool = False,
):
    built: dict[str, Any] = {}

    def app_factory(settings: Settings, db: Database, notifier: object) -> FakeApp:
        del settings, notifier
        app = FakeApp(db, trace, fail_start=fail_app)
        built["app"] = app
        return app

    def application_factory(app: FakeApp) -> object:
        built["application_app"] = app
        return object()

    def telegram_factory(settings: Settings, application: object) -> FakeTelegram:
        del settings, application
        telegram = FakeTelegram(trace, fail_polling=fail_polling)
        built["telegram"] = telegram
        return telegram

    def worker_factory(bot: object, db: Database, settings: Settings) -> FakeWorker:
        del db, settings
        worker = FakeWorker(bot, trace, fail_stop=fail_worker_stop)
        built["worker"] = worker
        return worker

    return app_factory, application_factory, telegram_factory, worker_factory, built


def build_process(
    settings: Settings,
    trace: list[str],
    *,
    clock: datetime | None = None,
    fail_app: bool = False,
    fail_polling: bool = False,
    fail_worker_stop: bool = False,
):
    app_factory, application_factory, telegram_factory, worker_factory, built = factories(
        trace,
        fail_app=fail_app,
        fail_polling=fail_polling,
        fail_worker_stop=fail_worker_stop,
    )
    kwargs = {
        "app_factory": app_factory,
        "application_factory": application_factory,
        "telegram_factory": telegram_factory,
        "worker_factory": worker_factory,
    }
    if clock is not None:
        kwargs["clock"] = lambda: clock
    kwargs["history_factory"] = lambda app: AsyncMock()
    return Stage2Process(settings, **kwargs), built


def build_production_process(
    settings: Settings,
    trace: list[str],
    *,
    clock: datetime | None = None,
    enable_user_event_delivery: bool = True,
    telegram_factory: Any = None,
    worker_factory: Any = None,
):
    """Build through the production factory with offline factories only."""
    app_factory, application_factory, default_telegram_factory, default_worker_factory, built = (
        factories(trace)
    )
    kwargs: dict[str, Any] = {
        "app_factory": app_factory,
        "application_factory": application_factory,
        "telegram_factory": telegram_factory or default_telegram_factory,
        "worker_factory": worker_factory or default_worker_factory,
        "enable_user_event_delivery": enable_user_event_delivery,
    }
    if clock is not None:
        kwargs["clock"] = lambda: clock
    process = build_stage2_process(settings, **kwargs)
    # build_stage2_process has no history_factory seam; set it on the instance
    # so the offline factories keep their AsyncMock history worker.
    process._history_factory = lambda app: AsyncMock()
    return process, built


async def test_process_order_shared_bot_and_idempotent_close(tmp_path: Path) -> None:
    trace: list[str] = []
    floor_time = datetime(2030, 1, 1, tzinfo=UTC)
    process, built = build_process(
        make_settings(tmp_path / "stage2.sqlite3", admin_ids=[99]),
        trace,
        clock=floor_time,
    )

    await process.run()
    await process.shutdown()

    assert trace == [
        "app.start",
        "worker.start",
        "telegram.run",
        "telegram.shutdown",
        "worker.stop:True",
        "app.stop",
        "bot.close",
    ]
    assert built["worker"].bot is built["telegram"].bot
    assert built["telegram"].close_calls == 1
    assert process.db.conn is None

    db = Database(tmp_path / "stage2.sqlite3")
    await db.connect()
    row = await db.fetchone("SELECT chat_id, role FROM telegram_users WHERE telegram_user_id=99")
    assert row["chat_id"] == 99
    assert row["role"] == "ADMIN"
    floor = await SettingsRepo(db).get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY)
    assert floor == floor_time.isoformat()
    await db.close()


async def test_concurrent_start_is_serialized_and_composes_once(tmp_path: Path) -> None:
    trace: list[str] = []
    process, _ = build_process(
        make_settings(tmp_path / "concurrent-start.sqlite3"),
        trace,
    )

    await asyncio.gather(process.start(), process.start(), process.start())

    assert trace.count("app.start") == 1
    assert trace.count("worker.start") == 1
    assert process.db.conn is not None
    await process.shutdown()


async def test_polling_failure_sets_app_stop_and_preserves_failure(tmp_path: Path) -> None:
    trace: list[str] = []
    process, built = build_process(
        make_settings(tmp_path / "failure.sqlite3"),
        trace,
        fail_polling=True,
        fail_worker_stop=True,
    )

    with pytest.raises(RuntimeError, match="polling failed"):
        await process.run()

    assert built["app"].stop_event.is_set()
    assert trace.index("worker.stop:True") < trace.index("app.stop") < trace.index("bot.close")
    assert built["telegram"].close_calls == 1


async def test_partial_app_start_failure_cleans_bot_and_db(tmp_path: Path) -> None:
    trace: list[str] = []
    process, built = build_process(
        make_settings(tmp_path / "partial.sqlite3"),
        trace,
        fail_app=True,
    )

    with pytest.raises(RuntimeError, match="app start failed"):
        await process.start()

    assert built["app"].stop_event.is_set() is False
    assert trace == ["app.start", "telegram.shutdown", "app.stop", "bot.close"]
    assert process.db.conn is None


async def test_admin_bootstrap_promotes_metadata_and_preserves_ownership(tmp_path: Path) -> None:
    path = tmp_path / "admin-conflict.sqlite3"
    db = Database(path)
    await db.connect()
    await db.migrate()
    await TelegramUserRepo(db).upsert(42, 420, role="USER")
    await db.close()

    process, built = build_process(
        make_settings(path, admin_ids=[42]),
        [],
    )
    await process.start()
    await process.shutdown()

    assert "app" in built
    check = Database(path)
    await check.connect()
    row = await TelegramUserRepo(check).get(42)
    assert row["role"] == "ADMIN"
    assert row["chat_id"] == 42
    await check.close()


async def test_first_floor_skips_stage1_and_next_boot_recovers_since_floor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "floor.sqlite3"
    db = Database(path)
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert(
        "tg-7",
        "follower",
        "owner",
        "0x1111111111111111111111111111111111111111",
    )
    await TelegramUserRepo(db).upsert(7, 700, account_id="tg-7")
    commands = CommandRepo(db)
    await commands.insert("old-command", "tg-7", None, "MARKET_OPEN", "{}", 1, None)
    old_time = datetime(2029, 12, 1, tzinfo=UTC).isoformat()
    await db.execute_write(
        "UPDATE execution_commands SET status='CONFIRMED', created_at=?, updated_at=? "
        "WHERE command_key='old-command'",
        (old_time, old_time),
    )
    await db.close()

    first_trace: list[str] = []
    first, _ = build_process(
        make_settings(path, admin_ids=[99]),
        first_trace,
        clock=datetime(2030, 1, 1, tzinfo=UTC),
    )
    await first.start()
    await first.shutdown()

    check = Database(path)
    await check.connect()
    assert await check.fetchone("SELECT 1 FROM notification_outbox") is None
    after_first = await SettingsRepo(check).get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY)
    assert after_first == "2030-01-01T00:00:00+00:00"
    t2 = datetime(2030, 1, 2, tzinfo=UTC).isoformat()
    await check.execute_write(
        "UPDATE execution_commands SET updated_at=? WHERE command_key='old-command'", (t2,)
    )
    await check.close()

    second_trace: list[str] = []
    second, _ = build_process(
        make_settings(path, admin_ids=[99]),
        second_trace,
        clock=datetime(2030, 1, 3, tzinfo=UTC),
    )
    await second.start()
    outbox = await second.db.fetchall(
        "SELECT event_key, telegram_user_id FROM notification_outbox ORDER BY id"
    )
    assert [(row["event_key"], row["telegram_user_id"]) for row in outbox] == [
        ("command:old-command", 7)
    ]
    await second.shutdown()

    check = Database(path)
    await check.connect()
    assert await SettingsRepo(check).get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY) == (
        "2030-01-03T00:00:00+00:00"
    )
    await check.close()


async def test_notifications_disabled_is_console_only_and_has_no_worker(tmp_path: Path) -> None:
    trace: list[str] = []
    process, built = build_process(
        make_settings(tmp_path / "console.sqlite3", notifications=False),
        trace,
        clock=datetime(2030, 1, 1, tzinfo=UTC),
    )
    await process.start()
    assert process.worker is None
    assert process.notifier.__class__.__name__ == "ConsoleNotifier"
    assert "worker" not in built
    await process.shutdown()


@pytest.mark.parametrize("bad_ids", [[True], ["not-an-id"], [-1]])
async def test_admin_bootstrap_rejects_invalid_ids(tmp_path: Path, bad_ids) -> None:
    db = Database(tmp_path / f"invalid-{str(bad_ids[0]).replace('-', 'n')}.sqlite3")
    await db.connect()
    await db.migrate()
    with pytest.raises(Stage2BootstrapError, match="positive integers"):
        await bootstrap_telegram_admins(db, bad_ids)
    await db.close()


async def test_admin_bootstrap_repairs_chat_and_preserves_follower_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "admin-shapes.sqlite3"
    db = Database(path)
    await db.connect()
    await db.migrate()
    users = TelegramUserRepo(db)
    await users.upsert(5, 500, role="ADMIN")
    await bootstrap_telegram_admins(db, [5])
    repaired = await users.get(5)
    assert repaired["chat_id"] == 5 and repaired["role"] == "ADMIN"

    await users.upsert(6, 7, role="USER")
    with pytest.raises(Stage2BootstrapError, match="existing chat"):
        await bootstrap_telegram_admins(db, [7])

    await AccountRepo(db).upsert(
        "tg-5",
        "follower",
        "owner",
        "0x1111111111111111111111111111111111111111",
    )
    await db.execute_write(
        "UPDATE telegram_users SET account_id=? WHERE telegram_user_id=?", ("tg-5", 5)
    )
    await bootstrap_telegram_admins(db, [5])
    owner_admin = await users.get(5)
    assert owner_admin["role"] == "ADMIN"
    assert owner_admin["account_id"] == "tg-5"
    await db.close()


async def test_recovery_floor_rejects_corruption_and_does_not_advance_on_failure(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "floor-errors.sqlite3")
    await db.connect()
    await db.migrate()
    settings_repo = SettingsRepo(db)
    await settings_repo.set_flag(NOTIFICATION_RECOVERY_FLOOR_KEY, "not-a-date")
    with pytest.raises(Stage2BootstrapError, match="recovery floor"):
        await recover_notification_outbox(
            db,
            None,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
    assert await settings_repo.get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY) == "not-a-date"

    await settings_repo.set_flag(
        NOTIFICATION_RECOVERY_FLOOR_KEY,
        "2030-01-01T00:00:00+00:00",
    )

    class FailingRecovery:
        async def recover_confirmed_commands(self, commands, *, since):
            del commands, since
            raise RuntimeError("recovery backend failed")

    with pytest.raises(RuntimeError, match="recovery backend failed"):
        await recover_notification_outbox(
            db,
            FailingRecovery(),  # type: ignore[arg-type]
            now=datetime(2030, 1, 2, tzinfo=UTC),
        )
    assert await settings_repo.get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY) == (
        "2030-01-01T00:00:00+00:00"
    )

    await recover_notification_outbox(
        db,
        None,
        now=datetime(2030, 1, 3, tzinfo=UTC),
    )
    assert await settings_repo.get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY) == (
        "2030-01-03T00:00:00+00:00"
    )
    await db.close()


def test_engine_factory_boundaries_and_utc_floor_parsing(tmp_path: Path, monkeypatch) -> None:
    naive = datetime(2030, 1, 1)
    assert _as_utc(naive).tzinfo is UTC
    assert _parse_floor("2030-01-01T00:00:00").tzinfo is UTC
    with pytest.raises(Stage2BootstrapError):
        _parse_floor("broken")

    settings = make_settings(tmp_path / "factory.sqlite3")
    db = Database(settings.db_path)
    app = _build_app(settings, db, ConsoleNotifier())
    assert app.db is db
    # App constructor owns an HTTP client even before start; close it here
    # without invoking any exchange endpoint.
    import asyncio

    asyncio.run(app.http.aclose())

    expected = object()
    from types import SimpleNamespace

    import copytradeaster.application.adapters as adapters

    monkeypatch.setattr(
        adapters,
        "build_stage2_composition",
        lambda current: SimpleNamespace(application=expected),
    )
    assert _build_application(app) is expected

    worker = _build_worker(object(), db, settings)
    assert worker is not None
    process = build_stage2_process(settings, db=db)
    assert isinstance(process, Stage2Process)
    asyncio.run(db.close())


async def test_history_worker_stops_before_app_closes_database(tmp_path: Path) -> None:
    trace = []
    process, _ = build_process(make_settings(tmp_path / "history.sqlite3"), trace)
    history = AsyncMock()

    async def start():
        trace.append("history.start")

    async def stop():
        assert process.db.conn is not None
        trace.append("history.stop")

    history.start.side_effect = start
    history.stop.side_effect = stop
    process._history_factory = lambda app: history
    await process.start()
    await process.shutdown()
    assert trace.index("app.start") < trace.index("history.start")
    assert trace.index("history.stop") < trace.index("app.stop")
    history.stop.assert_awaited_once()


# --- N5: production factory, durable delivery across boots and shutdown ---

_N5_CLOSED_FACTS = {
    "symbol": "BTCUSDT",
    "side": "LONG",
    "entry_price": "62400",
    "exit_price": "64100",
    "realized_gross": "18.42",
    "duration_ms": 8_280_000,
}


class _N5CaptureBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self, *, chat_id: int, text: str, parse_mode: Any = None, reply_markup: Any = None
    ) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


async def test_production_factory_enables_durable_delivery_and_low_level_seam_keeps_legacy(
    tmp_path: Path,
) -> None:
    """N5: the production factory enables durable delivery; the explicit
    low-level False keeps the legacy command path with no sweep task."""
    production, _ = build_production_process(
        make_settings(tmp_path / "n5-factory.sqlite3", admin_ids=[99]),
        [],
        clock=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert production._enable_user_event_delivery is True
    await production.start()
    assert production._delivery_task is not None
    assert production.notifier is not None
    assert production.notifier._user_events_only is True
    await production.shutdown()

    legacy, _ = build_production_process(
        make_settings(tmp_path / "n5-legacy.sqlite3", admin_ids=[99]),
        [],
        clock=datetime(2030, 1, 1, tzinfo=UTC),
        enable_user_event_delivery=False,
    )
    assert legacy._enable_user_event_delivery is False
    await legacy.start()
    assert legacy._delivery_task is None
    assert legacy.notifier is not None
    assert legacy.notifier._user_events_only is False
    await legacy.shutdown()


async def test_two_boots_sweep_over_1000_events_without_duplicates(tmp_path: Path) -> None:
    """N5: two production boots deliver a >1000-event backlog exactly once."""
    path = tmp_path / "n5-boots.sqlite3"
    db = Database(path)
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("a1", "follower", "a1", "0x" + "1" * 40)
    await TelegramUserRepo(db).upsert(1, 1, account_id="a1")
    await db.close()

    # Boot 1: the production factory records the floors and sends nothing.
    first, _ = build_production_process(
        make_settings(path, admin_ids=[99]), [], clock=datetime(2030, 1, 1, tzinfo=UTC)
    )
    await first.start()
    await first.shutdown()

    db = Database(path)
    await db.connect()
    events = EventRepository(db)
    scope = HistoryScope("a1", "testnet")
    base_ms = int(datetime(2030, 1, 2, tzinfo=UTC).timestamp() * 1000)
    for i in range(1001):
        await events.record(
            scope,
            kind="POSITION_CLOSED",
            occurred_at=base_ms + i,
            recorded_at=base_ms + i,
            facts=dict(_N5_CLOSED_FACTS),
            dedup_key=f"n5:boot:{i}",
            trade_ref=f"ref-{i}",
            notify_eligible=True,
        )
    await db.close()

    # Boot 2: the durable sweep drains the whole backlog once.
    second, _ = build_production_process(
        make_settings(path, admin_ids=[99]), [], clock=datetime(2030, 1, 3, tzinfo=UTC)
    )
    await second.start()
    await second.shutdown()

    db = Database(path)
    await db.connect()
    count = await db.fetchone("SELECT COUNT(*) AS n FROM notification_outbox")
    assert count is not None and int(count["n"]) == 1001
    dupes = await db.fetchall(
        "SELECT event_key, COUNT(*) AS n FROM notification_outbox"
        " GROUP BY event_key, telegram_user_id HAVING n > 1"
    )
    assert dupes == []
    await db.close()

    # Boot 3: a settled sweep replays to zero new rows.
    third, _ = build_production_process(
        make_settings(path, admin_ids=[99]), [], clock=datetime(2030, 1, 4, tzinfo=UTC)
    )
    await third.start()
    await third.shutdown()
    db = Database(path)
    await db.connect()
    count = await db.fetchone("SELECT COUNT(*) AS n FROM notification_outbox")
    assert count is not None and int(count["n"]) == 1001
    await db.close()


async def test_delivery_task_is_cancelled_before_database_close(tmp_path: Path) -> None:
    """N5: the recurring sweep task stops before the DB closes."""
    process, _ = build_production_process(
        make_settings(tmp_path / "n5-task.sqlite3"),
        [],
        clock=datetime(2030, 1, 1, tzinfo=UTC),
    )
    await process.start()
    task = process._delivery_task
    assert task is not None

    alive_at_close: list[bool] = []
    original_close = process.db.close

    async def close_with_probe() -> None:
        alive_at_close.append(process._delivery_task is not None)
        await original_close()

    process.db.close = close_with_probe  # type: ignore[method-assign]
    await process.shutdown()

    assert alive_at_close == [False]
    assert task.done()


async def test_delivery_sweep_failure_retries_without_killing_the_task(
    tmp_path: Path, monkeypatch
) -> None:
    """N5: one failed sweep logs and retries on the next cycle."""
    import copytradeaster.application.engine as engine

    process, _ = build_production_process(
        make_settings(tmp_path / "n5-retry.sqlite3"),
        [],
        clock=datetime(2030, 1, 1, tzinfo=UTC),
    )
    attempts: list[int] = []
    retried = asyncio.Event()

    async def failing_then_ok(db, router, *, now, environment):
        del db, router, now
        assert environment == "testnet"
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("sweep failed")
        retried.set()
        return 0

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(engine, "_recover_user_events_durable", failing_then_ok)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await process.start()
    try:
        await asyncio.wait_for(retried.wait(), timeout=5)
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)
        await process.shutdown()

    assert len(attempts) >= 2
    assert process._delivery_task is None


async def test_durable_delivery_suppresses_legacy_command_duplicates(tmp_path: Path) -> None:
    """N5: with durable delivery the user receives user events only; a
    legacy execution.confirmed row is superseded, never sent, and the
    confirmed command is not re-enqueued."""
    path = tmp_path / "n5-duplicates.sqlite3"
    db = Database(path)
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("a1", "follower", "a1", "0x" + "1" * 40)
    await TelegramUserRepo(db).upsert(1, 1, account_id="a1")
    commands = CommandRepo(db)
    await commands.insert("cmd-1", "a1", None, "MARKET_OPEN", "{}", 1, None)
    old_iso = datetime(2029, 12, 31, tzinfo=UTC).isoformat()
    await db.execute_write(
        "UPDATE execution_commands SET status='CONFIRMED', created_at=?, updated_at=?"
        " WHERE command_key='cmd-1'",
        (old_iso, old_iso),
    )
    await NotificationOutboxRepo(db).enqueue(
        "command:cmd-old", 1, 1, "execution.confirmed", "INFO", "legacy", account_id="a1"
    )
    await db.close()

    first, _ = build_production_process(
        make_settings(path, admin_ids=[99]), [], clock=datetime(2030, 1, 1, tzinfo=UTC)
    )
    await first.start()
    await first.shutdown()

    db = Database(path)
    await db.connect()
    base_ms = int(datetime(2030, 1, 2, tzinfo=UTC).timestamp() * 1000)
    await EventRepository(db).record(
        HistoryScope("a1", "testnet"),
        kind="POSITION_CLOSED",
        occurred_at=base_ms,
        recorded_at=base_ms,
        facts=dict(_N5_CLOSED_FACTS),
        dedup_key="n5:dup:event",
        trade_ref="ref-1",
        notify_eligible=True,
    )
    await db.close()

    bot = _N5CaptureBot()
    trace: list[str] = []

    def capture_telegram(settings: Settings, application: object) -> FakeTelegram:
        del settings, application
        telegram = FakeTelegram(trace)
        telegram.bot = bot
        return telegram

    second, _ = build_production_process(
        make_settings(path, admin_ids=[99]),
        trace,
        clock=datetime(2030, 1, 3, tzinfo=UTC),
        telegram_factory=capture_telegram,
        worker_factory=_build_worker,
    )
    await second.start()
    try:
        assert second.worker is not None
        # Wait until the production worker retires the legacy row; the
        # background loops may hold it briefly in SENDING.
        for _ in range(20):
            row = await second.db.fetchone(
                "SELECT status FROM notification_outbox WHERE event_key='command:cmd-old'"
            )
            if row is not None and str(row["status"]) in {"SENT", "FAILED", "DEAD"}:
                break
            await asyncio.sleep(0.05)
        rows = await second.db.fetchall(
            "SELECT event_key, kind, status, last_error FROM notification_outbox ORDER BY id"
        )
    finally:
        await second.shutdown()

    by_key = {str(row["event_key"]): row for row in rows}
    assert "command:cmd-1" not in by_key
    assert by_key["command:cmd-old"]["status"] == "FAILED"
    assert by_key["command:cmd-old"]["last_error"] == "superseded_by_user_events"
    user_rows = [row for row in rows if str(row["kind"]) == "user.POSITION_CLOSED"]
    assert len(user_rows) == 1
    assert str(user_rows[0]["event_key"]).startswith("user-event:")
