"""Stage 2 process composition and bounded process lifecycle.

This module is the process boundary for the Telegram transport.  It owns one
SQLite connection, one :class:`~copytradeaster.app.App`, one Telegram Bot
session, and (when enabled) one outbox worker.  The application service and
the Telegram presenter remain transport-neutral; this module only wires their
explicit composition seams together.

The lifecycle is intentionally boring and ordered:

``connect -> migrate -> bootstrap admins/floor -> compose -> App.start ->
worker.start -> Telegram polling -> transport shutdown -> worker drain ->
App.stop -> Bot session close``.

Factories are explicit injection points for offline tests and for a future
composition root.  No optional method probing or duck-typed fallbacks are
used.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from copytradeaster.app import App
from copytradeaster.application.models import ApplicationProtocol
from copytradeaster.config import Settings
from copytradeaster.history.composition import build_history_worker
from copytradeaster.history.worker import HistoryWorker
from copytradeaster.logging_utils import get_logger
from copytradeaster.notifications.console import ConsoleNotifier
from copytradeaster.notifications.delivery import (
    ensure_user_event_rollout_floor,
    recover_user_events_durable,
)
from copytradeaster.notifications.protocol import Notifier
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.notifications.worker import TelegramOutboxWorker
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from copytradeaster.telegram.runtime import TelegramRuntime

NOTIFICATION_RECOVERY_FLOOR_KEY = "stage2_notification_recovery_floor"

logger = get_logger(__name__)


class Stage2BootstrapError(RuntimeError):
    """Raised when durable Stage 2 bootstrap data is unsafe to change."""


class Stage2TelegramRuntime(Protocol):
    """The small lifecycle surface required from a Telegram transport."""

    bot: Any

    async def run(self, stop_event: asyncio.Event, *, close_session: bool = False) -> None: ...

    async def shutdown(self, *, close_session: bool = False) -> None: ...

    async def close_session(self) -> None: ...


class Stage2OutboxWorker(Protocol):
    """The explicit worker lifecycle used by :class:`Stage2Process`."""

    async def start(self) -> None: ...

    async def stop(self, *, drain: bool = False, timeout: float | None = None) -> None: ...


DatabaseFactory = Callable[[Path], Database]
AppFactory = Callable[[Settings, Database, Notifier], App]
ApplicationFactory = Callable[[App], ApplicationProtocol]
TelegramFactory = Callable[[Settings, ApplicationProtocol], Stage2TelegramRuntime]
WorkerFactory = Callable[[Any, Database, Settings], Stage2OutboxWorker]
Clock = Callable[[], datetime]


def _build_app(settings: Settings, db: Database, notifier: Notifier) -> App:
    """Construct App with the process-owned DB and notification boundary."""
    return App(settings, db=db, notifier=notifier)


def _build_application(app: App) -> ApplicationProtocol:
    """Use the current explicit composition helper for the current App."""
    # Imported here so Stage 1 commands can still import the CLI without
    # constructing the Stage 2 Aster adapter or Telegram presenter.
    from copytradeaster.application.adapters import build_stage2_composition

    return build_stage2_composition(app).application


def _build_telegram(settings: Settings, application: ApplicationProtocol) -> TelegramRuntime:
    return TelegramRuntime.build(settings, application)


def _build_worker(bot: Any, db: Database, settings: Settings) -> TelegramOutboxWorker:
    # The worker deliberately has a stricter bound than the Telegram handler
    # task pool.  A config value of 16 must not accidentally create 16 SQLite
    # outbox workers or 16 concurrent sends.
    concurrency = min(4, max(1, int(settings.telegram_tasks_concurrency_limit)))
    return TelegramOutboxWorker(
        bot=bot,
        db=db,
        concurrency=concurrency,
        poll_interval=float(settings.telegram_outbox_poll_interval_s),
        webapp_base_url=settings.miniapp_public_url or None,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_floor(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise Stage2BootstrapError("invalid Stage 2 notification recovery floor") from exc
    return _as_utc(parsed)


async def bootstrap_telegram_admins(db: Database, admin_ids: list[int] | tuple[int, ...]) -> None:
    """Create configured admin identities without rewriting user ownership.

    Admin authority is derived from the immutable configured ID set, not from
    an exclusive database role.  An existing follower link is preserved while
    the row receives ADMIN metadata.  Chat IDs equal user IDs because Stage 2
    authorizes private chats only.
    """

    normalized_values: set[int] = set()
    for value in admin_ids:
        if isinstance(value, bool):
            raise Stage2BootstrapError("Telegram admin IDs must be positive integers")
        try:
            normalized_values.add(int(value))
        except (TypeError, ValueError) as exc:
            raise Stage2BootstrapError("Telegram admin IDs must be positive integers") from exc
    normalized = tuple(sorted(normalized_values))
    if any(value <= 0 for value in normalized):
        raise Stage2BootstrapError("Telegram admin IDs must be positive integers")
    if not normalized:
        return

    users = TelegramUserRepo(db)
    for telegram_user_id in normalized:
        row = await users.get(telegram_user_id)
        if row is not None and str(row["role"]) not in {"USER", "ADMIN"}:
            raise Stage2BootstrapError(f"Telegram admin {telegram_user_id} has an unsupported role")

        # A different Telegram identity may already own this private chat.
        # Do this read before any upsert so a later configured ID cannot
        # partially bootstrap after discovering a unique-chat conflict.
        chat_owner = await db.fetchone(
            "SELECT telegram_user_id FROM telegram_users WHERE chat_id=?",
            (telegram_user_id,),
        )
        if chat_owner is not None and int(chat_owner["telegram_user_id"]) != telegram_user_id:
            raise Stage2BootstrapError(
                f"Telegram admin {telegram_user_id} conflicts with an existing chat"
            )

    for telegram_user_id in normalized:
        row = await users.get(telegram_user_id)
        # Account ownership is preserved while metadata and the private chat
        # destination are normalized to the configured administrator.
        await users.upsert(
            telegram_user_id,
            telegram_user_id,
            role="ADMIN",
            account_id=None if row is None else row["account_id"],
        )


async def recover_notification_outbox(
    db: Database,
    notifier: NotificationRouter | None,
    *,
    now: datetime,
    enable_user_event_delivery: bool = False,
) -> None:
    """Recover confirmed commands after a durable, non-retroactive floor.

    The first Stage 2 boot records a floor and intentionally does not scan
    old Stage 1 confirmations.  On later boots, recovery must succeed before
    the floor advances; a failed recovery therefore retries on the next boot.
    Console-only mode still advances the floor but never creates an outbox or
    worker.

    With ``enable_user_event_delivery`` (S1 default off) the user-event leg
    uses the durable per-scope cursor instead of the legacy timestamp sweep
    and records a separate rollout floor on first enable, so accumulated
    backfill never blasts. Legacy command recovery is identical either way.
    """

    settings = SettingsRepo(db)
    current = await settings.get_flag(NOTIFICATION_RECOVERY_FLOOR_KEY)
    current_now = _as_utc(now)
    if current is None:
        await settings.set_flag(NOTIFICATION_RECOVERY_FLOOR_KEY, current_now.isoformat())
        if enable_user_event_delivery:
            await ensure_user_event_rollout_floor(db, now=current_now)
        return

    floor = _parse_floor(current)
    if notifier is not None:
        await notifier.recover_confirmed_commands(CommandRepo(db), since=floor)
        if enable_user_event_delivery:
            await _recover_user_events_durable(db, notifier, now=current_now)
        else:
            await _recover_user_events(db, notifier, floor)
    elif enable_user_event_delivery:
        # Console-only mode advances durable cursors without enqueueing, so
        # a later enable never retroactively sends the muted backlog.
        await _recover_user_events_durable(db, None, now=current_now)
    next_floor = max(floor, current_now)
    await settings.set_flag(NOTIFICATION_RECOVERY_FLOOR_KEY, next_floor.isoformat())


async def _recover_user_events(db: Database, notifier: NotificationRouter, floor: datetime) -> int:
    """Emit eligible user events recorded after the recovery floor.

    Crash-window trades notify from saved facts with their proven trade
    ref instead of re-guessing text from the command journal; backfill
    rows stay silent via notify_eligible. Only REAL follower accounts are
    scanned; SHADOW rows have no recipients and notify nobody.
    """
    # Mainnet is fail-closed (CT_ALLOW_MAINNET=false), so every history
    # scope is testnet today; revisit if mainnet is ever enabled.
    environment = "testnet"
    rows = await AccountRepo(db).get_all("follower")
    scopes = [
        HistoryScope(str(row["id"]), environment)
        for row in rows
        if str(row["execution_mode"] or "REAL") == "REAL"
    ]
    return await notifier.recover_user_events(
        EventRepository(db),
        scopes,
        since_ms=int(floor.timestamp() * 1000),
    )


async def _recover_user_events_durable(
    db: Database, notifier: NotificationRouter | None, *, now: datetime
) -> int:
    """Sweep eligible user events behind the durable per-scope cursor.

    The rollout floor is recorded on first enable; history collection is
    unaffected because this only reads the already-published event log.
    """
    # Mainnet is fail-closed (CT_ALLOW_MAINNET=false), so every history
    # scope is testnet today; revisit if mainnet is ever enabled.
    environment = "testnet"
    rows = await AccountRepo(db).get_all("follower")
    scopes = [
        HistoryScope(str(row["id"]), environment)
        for row in rows
        if str(row["execution_mode"] or "REAL") == "REAL"
    ]
    rollout_floor_ms = await ensure_user_event_rollout_floor(db, now=now)
    report = await recover_user_events_durable(
        db, notifier, EventRepository(db), scopes, rollout_floor_ms=rollout_floor_ms
    )
    logger.info(
        "user event delivery sweep recovered=%s skipped_ineligible=%s "
        "skipped_no_recipient=%s blocked=%s budget_exhausted=%s",
        report.recovered,
        report.skipped_ineligible,
        report.skipped_no_recipient,
        report.blocked_unrenderable,
        report.budget_exhausted,
    )
    return report.recovered


class Stage2Process:
    """Own the complete Stage 2 process lifecycle.

    ``start`` performs all local setup and starts the trading App but does not
    enter Telegram polling.  ``run`` adds polling and always performs the
    ordered two-phase shutdown.  The explicit factories make lifecycle tests
    independent from the Bot API and let a future Mini App reuse the same
    process composition.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        db_factory: DatabaseFactory = Database,
        app_factory: AppFactory = _build_app,
        application_factory: ApplicationFactory = _build_application,
        telegram_factory: TelegramFactory = _build_telegram,
        worker_factory: WorkerFactory = _build_worker,
        clock: Clock = _utc_now,
        history_factory: Callable[[App], HistoryWorker] = build_history_worker,
        enable_user_event_delivery: bool = False,
    ) -> None:
        self.settings = settings
        self.db = db if db is not None else db_factory(settings.db_path)
        self._app_factory = app_factory
        self._application_factory = application_factory
        self._telegram_factory = telegram_factory
        self._worker_factory = worker_factory
        self._history_factory = history_factory
        # S1 default off: the durable user-event consumer has no boot hook or
        # recurring drain until S3; history collection never depends on it.
        self._enable_user_event_delivery = bool(enable_user_event_delivery)
        self.history_worker: HistoryWorker | None = None
        self._clock = clock
        self.app: App | None = None
        self.application: ApplicationProtocol | None = None
        self.telegram: Stage2TelegramRuntime | None = None
        self.miniapp: Any | None = None
        self.worker: Stage2OutboxWorker | None = None
        self.notifier: Notifier | None = None
        self._worker_started = False
        self._transport_shutdown = False
        self._worker_stopped = False
        self._app_stopped = False
        self._bot_session_closed = False
        self._db_closed = False
        self._started = False
        self._shutdown_complete = False
        self._start_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()

    async def start(self) -> None:
        """Connect/migrate, prepare durable Stage 2 state, and start App."""
        async with self._start_lock:
            await self._start_once()

    async def _start_once(self) -> None:
        if self._started:
            return
        if self._shutdown_complete:
            raise RuntimeError("Stage 2 process instances cannot be restarted")
        try:
            if self.db.conn is None:
                await self.db.connect()
            await self.db.migrate()
            await bootstrap_telegram_admins(self.db, self.settings.telegram_admin_ids)

            if self.settings.telegram_notifications_enabled:
                notifier: Notifier = NotificationRouter(
                    self.db,
                    admin_ids=self.settings.telegram_admin_ids,
                )
                self.notifier = notifier
                await recover_notification_outbox(
                    self.db,
                    notifier,
                    now=self._clock(),
                    enable_user_event_delivery=self._enable_user_event_delivery,
                )
            else:
                # Explicit console-only mode: no durable router and no worker.
                notifier = ConsoleNotifier()
                self.notifier = notifier
                await recover_notification_outbox(
                    self.db,
                    None,
                    now=self._clock(),
                    enable_user_event_delivery=self._enable_user_event_delivery,
                )

            self.app = self._app_factory(self.settings, self.db, notifier)
            self.application = self._application_factory(self.app)
            self.telegram = self._telegram_factory(self.settings, self.application)

            # Code freshness marker: the operator can compare the running
            # process against the source tree instantly (staleness caused a
            # misleading acceptance session once).
            from copytradeaster import application as _app_pkg

            _svc = Path(_app_pkg.__file__).parent / "service.py"
            _marker = f"{_svc.stat().st_size}:{int(_svc.stat().st_mtime)}"
            logger.info("code marker application/service.py=%s", _marker)

            # App owns the shared DB close after all workers have drained.  It
            # must be READY/started before Telegram can begin polling.
            await self.app.start()
            self.history_worker = self._history_factory(self.app)
            await self.history_worker.start()

            if self.settings.miniapp_enabled:
                from copytradeaster.miniapp.server import MiniAppServer

                self.miniapp = MiniAppServer(self.settings, self.application)
                await self.miniapp.start()

            if self.settings.telegram_notifications_enabled:
                self.worker = self._worker_factory(self.telegram.bot, self.db, self.settings)
                await self.worker.start()
                self._worker_started = True
            self._started = True
        except BaseException:
            with contextlib.suppress(BaseException):
                await self.shutdown()
                # Preserve the startup failure; callers can invoke the
                # explicit shutdown seam separately to inspect teardown.
            raise

    async def run(self) -> None:
        """Run Telegram polling until stop, failure, cancellation, or Ctrl+C."""
        original_error: BaseException | None = None
        try:
            await self.start()
            if self.telegram is None or self.app is None:
                raise RuntimeError("Stage 2 process did not compose Telegram and App")
            try:
                await self.telegram.run(self.app.stop_event, close_session=False)
            except BaseException:
                # A polling conflict/network failure is an engine stop signal;
                # App.stop sees this event even when a transport fake does not.
                self.app.stop_event.set()
                raise
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            try:
                await self.shutdown()
            except BaseException:
                # Never replace a polling/startup exception with a teardown
                # error.  A direct shutdown call still reports teardown
                # failures to its caller.
                if original_error is None:
                    raise

    async def shutdown(self) -> None:
        """Drain in dependency order and close every owned resource once."""
        errors: list[BaseException] = []
        async with self._shutdown_lock:
            telegram = self.telegram
            if telegram is not None and not self._transport_shutdown:
                try:
                    # Keep Bot session open for the worker drain below.
                    await telegram.shutdown(close_session=False)
                except BaseException as exc:  # noqa: BLE001 - continue teardown
                    errors.append(exc)
                finally:
                    self._transport_shutdown = True

            worker = self.worker
            if worker is not None and not self._worker_stopped:
                try:
                    await worker.stop(drain=self._worker_started)
                except BaseException as exc:  # noqa: BLE001 - continue teardown
                    errors.append(exc)
                finally:
                    self._worker_stopped = True

            miniapp = getattr(self, "miniapp", None)
            if miniapp is not None:
                try:
                    await miniapp.stop()
                except BaseException as exc:  # noqa: BLE001 - continue teardown
                    errors.append(exc)
                finally:
                    self.miniapp = None

            if self.history_worker is not None:
                try:
                    await self.history_worker.stop()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    self.history_worker = None

            app = self.app
            if app is not None and not self._app_stopped:
                try:
                    await app.stop()
                except BaseException as exc:  # noqa: BLE001 - continue teardown
                    errors.append(exc)
                finally:
                    self._app_stopped = True

            if telegram is not None and not self._bot_session_closed:
                try:
                    await telegram.close_session()
                except BaseException as exc:  # noqa: BLE001 - continue teardown
                    errors.append(exc)
                finally:
                    self._bot_session_closed = True

            # App.stop closes this same Database in production.  The explicit
            # connection check also closes it when composition failed before
            # an App existed, or when a test App intentionally omits DB work.
            if not self._db_closed and self.db.conn is not None:
                try:
                    await self.db.close()
                except BaseException as exc:  # noqa: BLE001 - continue teardown
                    errors.append(exc)
                finally:
                    self._db_closed = True
            self._started = False
            self._shutdown_complete = True

        if errors:
            raise errors[0]


def build_stage2_process(
    settings: Settings,
    *,
    db: Database | None = None,
    db_factory: DatabaseFactory = Database,
    app_factory: AppFactory = _build_app,
    application_factory: ApplicationFactory = _build_application,
    telegram_factory: TelegramFactory = _build_telegram,
    worker_factory: WorkerFactory = _build_worker,
    clock: Clock = _utc_now,
    enable_user_event_delivery: bool = False,
) -> Stage2Process:
    """Build the production Stage 2 process with explicit test seams."""
    return Stage2Process(
        settings,
        db=db,
        db_factory=db_factory,
        app_factory=app_factory,
        application_factory=application_factory,
        telegram_factory=telegram_factory,
        worker_factory=worker_factory,
        clock=clock,
        enable_user_event_delivery=enable_user_event_delivery,
    )


__all__ = [
    "NOTIFICATION_RECOVERY_FLOOR_KEY",
    "Stage2BootstrapError",
    "Stage2Process",
    "Stage2OutboxWorker",
    "Stage2TelegramRuntime",
    "bootstrap_telegram_admins",
    "build_stage2_process",
    "recover_notification_outbox",
]
