"""Application composition root. Boot order is fixed by docs/ARCHITECTURE.md §12."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx

from copytradeaster.aster.models import OrderDTO
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.shadow import ShadowExchange
from copytradeaster.aster.trading_api import AsterClient, AsterError
from copytradeaster.aster.user_stream import UserStream
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.logging_utils import DEFAULT_LOG_PATH, get_logger, setup_logging
from copytradeaster.master.projector import MasterProjector
from copytradeaster.notifications.console import ConsoleNotifier
from copytradeaster.notifications.formatter import format_confirmed_action
from copytradeaster.notifications.models import NotificationEvent, NotificationSeverity
from copytradeaster.notifications.protocol import NotificationCallback, Notifier, as_notifier
from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidationRepo,
    CommandRepo,
    CopyTradeRepo,
    MasterEventRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.builder_attribution import BuilderAttributionProvider
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor, resolve_boot_unknowns
from copytradeaster.trading.leverage import LeverageManager
from copytradeaster.trading.reconciler import Reconciler
from copytradeaster.trading.registry import FollowerRegistry

log = get_logger(__name__)
CLOCK_DRIFT_MAX_MS = 2000
MASTER_RESNAPSHOT_EVERY_SWEEPS = 10  # ~10x reconcile_interval; heals projector drift
INGEST_QUEUE_MAX = 1000
STREAM_READY_TIMEOUT_S = 30.0
TESTNET_REST_URL = "https://fapi.asterdex-testnet.com"
TESTNET_WS_URL = "wss://fstream.asterdex-testnet.com/ws"
_SAFE_FRAGMENT = re.compile(r"[^A-Za-z0-9_.:-]+")


def _safe_fragment(value: object, fallback: str = "unknown") -> str:
    """Return a bounded identifier suitable for user-facing plain text/keys."""
    text = _SAFE_FRAGMENT.sub("_", str(value or ""))[:80].strip("_")
    return text or fallback


def _error_category(exc: BaseException) -> str:
    """Map an exception to a safe category; details stay in local logs."""
    if isinstance(exc, AsterError):
        return f"aster_{_safe_fragment(exc.code, 'error')}"
    return _safe_fragment(type(exc).__name__, "error")


def validate_shadow_boot(cfg: Settings, accounts) -> None:
    """Fail closed for enabled SHADOW accounts and preserve disabled restarts."""
    main_row = next((row for row in accounts if str(row["id"]) == "main"), None)
    if main_row is not None and str(main_row["execution_mode"] or "REAL") != "REAL":
        raise RuntimeError("main account must always use execution_mode=REAL")

    shadow_rows = [
        row
        for row in accounts
        if str(row["role"]) == "follower"
        and int(row["enabled"] or 0)
        and str(row["execution_mode"] or "REAL") == "SHADOW"
    ]
    if not shadow_rows:
        return
    if not cfg.shadow_accounts_enabled:
        raise RuntimeError("SHADOW accounts found but CT_SHADOW_ACCOUNTS_ENABLED is false")
    rest = str(cfg.rest_base_url).rstrip("/")
    ws = str(cfg.ws_user_base).rstrip("/")
    if rest != TESTNET_REST_URL or ws != TESTNET_WS_URL:
        raise RuntimeError(
            "enabled SHADOW accounts require the official Aster testnet REST/WS URLs"
        )


@dataclass(slots=True)
class FollowerLifecycle:
    """Locked application boundary for replacement/disconnect workflows.

    The caller may perform its durable pause and DB/cache commit while this
    context is held, then publish through the CAS methods below.  No database
    policy is hidden here; the context only provides the lock order and safe
    exchange-state checks needed by the application service.
    """

    app: App
    runtime: FollowerRuntime
    _active: bool = field(default=False, init=False, repr=False)
    _safe_to_remove: bool = field(default=False, init=False, repr=False)

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("follower lifecycle context is not active")

    async def snapshot(self):
        self._require_active()
        return await self.app.coordinator.snapshot_follower_locked(self.runtime)

    async def assert_safe_to_remove(self) -> None:
        self._require_active()
        self._safe_to_remove = False
        actual = await self.snapshot()
        if any(position.qty != 0 for position in actual.positions.values()):
            raise RuntimeError(f"follower {self.runtime.account_id!r} is not flat")
        if actual.open_orders:
            raise RuntimeError(f"follower {self.runtime.account_id!r} has open orders")
        in_flight = await self.app.commands.in_flight()
        if any(str(row["account_id"]) == self.runtime.account_id for row in in_flight):
            raise RuntimeError(f"follower {self.runtime.account_id!r} has in-flight commands")
        self._safe_to_remove = True

    async def publish_replacement(self, replacement: FollowerRuntime) -> FollowerRuntime:
        self._require_active()
        if replacement.account_id != self.runtime.account_id:
            raise ValueError("replacement account_id must match the lifecycle account")
        previous = await self.app.follower_registry.publish_replacement(
            replacement, expected=self.runtime
        )
        return previous

    async def unpublish_after_commit(self) -> FollowerRuntime:
        """CAS-unpublish after the caller's durable/cache commit.

        ``assert_safe_to_remove`` is intentionally not repeated here: a
        disconnect commit may invalidate the credential provider, so another
        signed snapshot would turn a safe remove into a partial failure.
        """
        self._require_active()
        if not self._safe_to_remove:
            raise RuntimeError("unpublish_after_commit requires a successful assert_safe_to_remove")
        self._safe_to_remove = False
        return await self.app.follower_registry.unpublish(
            self.runtime.account_id, expected=self.runtime
        )

    async def remove_when_safe(self) -> FollowerRuntime:
        await self.assert_safe_to_remove()
        return await self.unpublish_after_commit()


class App:
    def __init__(
        self,
        cfg: Settings,
        notifier: Notifier | NotificationCallback | None = None,
        *,
        db: Database | None = None,
    ) -> None:
        self.cfg = cfg
        # The notifier is a short enqueue boundary.  It is intentionally
        # injected before any engine component is composed so startup events
        # use exactly the same sink as runtime events.  ``as_notifier`` also
        # adapts an async callback for small tests and embedding callers.
        self.notifier = as_notifier(notifier if notifier is not None else ConsoleNotifier())
        self._notification_token = uuid.uuid4().hex[:16]
        # Stage 2 composes the durable notification router before the trading
        # components.  Accepting one explicit Database instance lets the
        # router and App share the same connection without late mutation of
        # notifier internals or a second SQLite connection.
        self.db = db if db is not None else Database(cfg.db_path)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.limiter = RateLimits(
            cfg.weight_limit_per_min, cfg.orders_limit_per_min, cfg.orders_limit_per_10s
        )
        self.credentials = SqliteCredentials(
            self.db, cfg.encryption_key_bytes(), trading_chain_id=714
        )
        self.settings = SettingsRepo(self.db)
        self.events = MasterEventRepo(self.db)
        self.trades = CopyTradeRepo(self.db)
        self.links = OrderLinkRepo(self.db)
        self.commands = CommandRepo(self.db)
        self.history = HistoryRepository(self.db)
        self.nonces = NonceStore(self.db)
        self.basis = SyncBasisRepo(self.db)
        self.follower_registry = FollowerRegistry()
        self.shadow_exchange = ShadowExchange(
            hedge=cfg.position_mode == "HEDGE",
            auto_trigger_conditionals=cfg.shadow_auto_trigger_conditionals,
        )
        self.accounts = AccountRepo(self.db)
        self.validations = AgentValidationRepo(self.db)
        self.builder_attribution = BuilderAttributionProvider(
            accounts=self.accounts,
            validations=self.validations,
            credentials=self.credentials,
            builder_address=cfg.builder_address,
            builder_name=cfg.builder_name,
            max_fee_rate=cfg.max_fee_rate,
        )
        self.executor = Executor(
            self._client_for,
            self.commands,
            self.links,
            {},
            settings=self.settings,
            on_confirmed=self._on_confirmed,
            attribution_provider=self.builder_attribution,
            builder_invalidator=self.accounts.clear_builder_snapshot,
            order_observer=self._observe_history_order,
        )
        self.master_client = self._make_client()
        self.projector = MasterProjector(
            self.events,
            self.trades,
            self.links,
            self.settings,
            self._on_delta,
            on_critical=self._on_master_critical,
        )
        # Leverage control-plane is composed BEFORE the coordinator: the
        # coordinator reads confirmed values from it on the trading hot path.
        self.leverage = LeverageManager(
            client_for=self._client_for,
            symbols=tuple(cfg.symbol_whitelist),
            accounts=self._real_follower_ids,
            catalog_account="main",
        )
        self.coordinator = Coordinator(
            followers=self.follower_registry,
            settings=self.settings,
            executor=self.executor,
            trades=self.trades,
            links=self.links,
            filters_provider=self._filters,
            app_flags=self.settings,
            symbol_whitelist=cfg.symbol_whitelist,
            fee_buffer_pct=cfg.fee_buffer_pct,
            working_type=cfg.working_type,
            notifier=self.notifier,
            basis=self.basis,
            snapshot_symbols=set(cfg.follower_snapshot_symbols) or None,
            leverage=self.leverage,
        )
        self.reconciler = Reconciler(self.coordinator)
        self.stream: UserStream | None = None
        self.stop_event = asyncio.Event()
        self._stream_ready = asyncio.Event()
        self._ingest_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=INGEST_QUEUE_MAX)
        self._ingest_dropped = 0
        self._ingest_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._leverage_task: asyncio.Task | None = None
        self._filters_cache: dict = {}
        self._ready = False
        self._stream_connect_count = 0
        self._stream_reconnect_count = 0
        self._stop_lock = asyncio.Lock()
        self._stop_task: asyncio.Task | None = None
        self._stop_complete = False

    # --- construction helpers ------------------------------------------------------

    def _make_client(self) -> AsterClient:
        return AsterClient(
            http=self.http,
            base_url=self.cfg.rest_base_url,
            limiter=self.limiter,
            identities=self.credentials,
            keys=self.credentials,
            nonces=self.nonces,
            dry_run=self.cfg.dry_run,
        )

    def _real_follower_ids(self) -> list[str]:
        """Accounts eligible for leverage provisioning (REAL execution only)."""
        return [
            fr.account_id for fr in self.follower_registry.snapshot() if fr.execution_mode == "REAL"
        ]

    def _client_for(self, account_id: str) -> AsterClient:
        """Fail closed: an unknown account must NEVER route to the master."""
        if account_id == "main":
            return self.master_client
        fr = self.follower_registry.get(account_id)
        if fr is not None:
            return fr.client
        raise KeyError(f"unknown account {account_id!r} - refusing to route to master")

    def _filters(self) -> dict:
        return self._filters_cache

    async def _observe_history_order(
        self,
        account_id: str,
        command_key: str,
        kind: str,
        order: OrderDTO,
    ) -> None:
        if self.cfg.dry_run or account_id == "main":
            return
        follower = self.follower_registry.get(account_id)
        if follower is None:
            return
        environment = (
            "shadow"
            if follower.execution_mode == "SHADOW"
            else "testnet"
            if "testnet" in self.cfg.rest_base_url
            else "mainnet"
        )
        await self.history.observe_order(
            HistoryScope(account_id, environment),
            order,
            kind=kind,
            command_key=command_key,
            observed_at=order.update_time,
        )

    async def _emit_notification(
        self,
        *,
        event_key: str,
        kind: str,
        severity: NotificationSeverity,
        text: str,
        account_id: str | None = None,
    ) -> None:
        """Best-effort structured notification boundary.

        Notification enqueue is deliberately isolated from engine state.  A
        broken sink (including SQLite/outbox pressure) is logged locally and
        never turns a successful boot, reconcile, or command confirmation
        into a trading failure.  Callers pass only already-sanitized plain
        text; exception details are represented by ``_error_category`` and
        remain in the local log.
        """
        event = NotificationEvent(
            event_key=event_key,
            account_id=account_id,
            kind=kind,
            severity=severity,
            text=text,
        )
        try:
            notifier = getattr(self, "notifier", None)
            if notifier is None:
                return
            await notifier.emit(event)
        except Exception:  # noqa: BLE001 - notification failure is isolated
            log.exception(
                "notification enqueue failed (event_key=%s kind=%s account_id=%s)",
                event.event_key,
                event.kind,
                event.account_id,
            )

    # --- boot -------------------------------------------------------------------------

    async def start(self) -> None:
        cfg = self.cfg
        setup_logging(
            cfg.log_level,
            log_path=getattr(cfg, "log_path", DEFAULT_LOG_PATH),
            max_bytes=getattr(cfg, "log_max_bytes", 10 * 1024 * 1024),
            backup_count=getattr(cfg, "log_backup_count", 5),
        )
        log.info("boot starting (db=%s dry_run=%s)", cfg.db_path, cfg.dry_run)

        # 1-2. storage + accounts/settings
        # The Stage 2 process bootstraps the Telegram admin allowlist and
        # notification recovery floor before the engine starts.  Standalone
        # Stage 1 callers still arrive with a disconnected database.
        if self.db.conn is None:
            await self.db.connect()
        await self.db.migrate()
        self._runtime_id = uuid.uuid4().hex
        await self.settings.start_runtime(self._runtime_id, now_ms())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        accounts = await AccountRepo_all(self.db)
        validate_shadow_boot(cfg, accounts)
        await self.credentials.load()
        followers: list[FollowerRuntime] = []
        for row in accounts:
            if row["role"] == "follower" and int(row["enabled"]):
                followers.append(self._runtime_for_row(row))
        await self.follower_registry.load_at_boot(followers)

        # 3. clock drift check (nonce window +-10s)
        server_ms = await self.master_client.server_time()
        drift = abs(server_ms - now_ms())
        if drift > CLOCK_DRIFT_MAX_MS:
            raise RuntimeError(f"clock drift {drift}ms exceeds {CLOCK_DRIFT_MAX_MS}ms")
        log.info("clock drift ok (%dms)", drift)

        # 4. resolve UNKNOWN commands from previous run
        resolved = await resolve_boot_unknowns(
            self._client_for,
            self.commands,
            self.links,
            order_observer=self._observe_history_order,
        )
        in_flight = await self.commands.in_flight()
        log.info(
            "boot resolution: confirmed=%d not_found=%d unresolved=%d",
            resolved.confirmed,
            resolved.failed_not_found,
            resolved.unresolved,
        )
        if in_flight or resolved.unresolved:
            await self.settings.set_flag("ready", "false")
            await self.settings.set_flag("degraded", f"boot-unresolved:{len(in_flight)}")
            await self._emit_notification(
                event_key=(f"degraded:boot-unresolved:{self._notification_token}:{len(in_flight)}"),
                kind="engine.degraded",
                severity=NotificationSeverity.WARN,
                text="Запуск остановлен: неразрешённые команды требуют восстановления.",
            )
            raise RuntimeError(
                f"boot recovery required: {len(in_flight)} in-flight command(s) unresolved"
            )

        # 5. bootstrap position modes + snapshots
        want_hedge = cfg.position_mode == "HEDGE"
        try:
            mode_is_hedge = await self.master_client.get_position_mode("main")
            if mode_is_hedge != want_hedge:
                log.warning("position mode mismatch on master - setting %s", cfg.position_mode)
                await self.master_client.set_position_mode("main", want_hedge)
        except Exception as exc:  # noqa: BLE001 - Testnet may fail account config reads
            # Do not turn a broken metadata endpoint into a total outage.  An
            # order carrying the wrong positionSide is rejected by Aster, so
            # risk-increasing execution still fails closed at the exchange.
            log.warning("master position mode unavailable (%s); continuing fail-closed", exc)
            category = _error_category(exc)
            await self._emit_notification(
                event_key=f"warning:position-mode:main:{self._notification_token}:{category}",
                kind="engine.position_mode",
                severity=NotificationSeverity.WARN,
                account_id="main",
                text=(
                    "Не удалось проверить режим позиции master; торговые команды с "
                    f"несовместимым режимом будут отклонены биржей (категория: {category})."
                ),
            )
        for fr in self.followers:
            if fr.execution_mode == "SHADOW":
                continue
            try:
                client = fr.client
                is_hedge = await client.get_position_mode(fr.account_id)
                if is_hedge != want_hedge:
                    log.warning("position mode mismatch on %s - fixing", fr.account_id)
                    await client.set_position_mode(fr.account_id, want_hedge)
            except Exception as exc:  # noqa: BLE001
                log.error("bootstrap failed for %s: %s", fr.account_id, exc)
                category = _error_category(exc)
                await self._emit_notification(
                    event_key=(
                        f"warning:position-mode:{_safe_fragment(fr.account_id)}:"
                        f"{self._notification_token}:{category}"
                    ),
                    kind="engine.position_mode",
                    severity=NotificationSeverity.WARN,
                    account_id=fr.account_id,
                    text=(
                        "Не удалось проверить режим позиции follower; требуется проверка "
                        f"(категория: {category})."
                    ),
                )
        exchange_info = await self.master_client.exchange_info(force=True)
        self._filters_cache = exchange_info.tradable_filters()
        await self.projector.rebuild_full(self.master_client, "main")

        # 6. leverage control-plane: seed the candidate catalog BEFORE follower
        # reconciliation so restart reconstruction from positionRisk validates
        # against the live generation instead of stamping revision -1 and
        # re-posting leverage. A failed probe keeps entries fail-closed; the
        # worker retries below.
        try:
            await self.leverage.refresh_catalog()
        except Exception as exc:  # noqa: BLE001 - worker retries; entries stay gated
            log.warning("initial leverage catalog refresh failed: %s", _error_category(exc))

        # 6b. startup reconciliation
        await self._emit_notification(
            event_key=f"app:{self._notification_token}:startup-reconciliation",
            kind="engine.startup",
            severity=NotificationSeverity.INFO,
            text="Запущена стартовая сверка позиций.",
        )
        await self.reconciler.reconcile(self.projector.state, reason="startup")
        self._leverage_task = asyncio.create_task(self.leverage.run(self.stop_event))

        # 7. streams + periodic sweep
        self.stream = UserStream(
            client=self.master_client,
            account_id="main",
            ws_base=cfg.ws_user_base,
            keepalive_min=cfg.listenkey_keepalive_min,
            on_event=self._on_stream_event,
            on_reconnect=self._on_stream_reconnect,
            on_connected=self._on_stream_connected,
        )
        stream_task = asyncio.create_task(self.stream.run(self.stop_event))
        self._stream_task = stream_task
        await self._wait_for_stream_startup()
        self._sweep_task = asyncio.create_task(self._periodic_sweep())
        self._ingest_task = asyncio.create_task(self._ingest_loop())
        # 8. READY
        await self.settings.set_flag("ready", "true")
        self._ready = True
        await self._emit_notification(
            event_key=f"app:{self._notification_token}:ready",
            kind="engine.ready",
            severity=NotificationSeverity.INFO,
            text="Движок готов к работе.",
        )
        log.info("READY")

    async def _wait_for_stream_startup(self) -> bool:
        """Prefer the low-latency stream, but keep REST reconciliation live.

        The startup snapshot and reconciliation have already completed before
        this method runs.  When Testnet's listenKey endpoint is unavailable,
        the UserStream task keeps retrying and ``_periodic_sweep`` temporarily
        performs an authoritative master REST snapshot on every cycle.
        """
        try:
            await asyncio.wait_for(self._stream_ready.wait(), timeout=STREAM_READY_TIMEOUT_S)
        except TimeoutError:
            log.warning("master user stream unavailable; starting with authoritative REST polling")
            await self._emit_notification(
                event_key=f"warning:master-stream:{self._notification_token}:rest-fallback",
                kind="engine.stream_fallback",
                severity=NotificationSeverity.WARN,
                account_id="main",
                text=(
                    "Поток master временно недоступен; движок использует регулярные "
                    "REST-снимки до восстановления подключения."
                ),
            )
            return False
        return True

    async def run_until_stopped(self) -> None:
        try:
            await self.start()
            await self.stop_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Idempotent, concurrent-safe teardown for READY and partial boots."""
        if not hasattr(self, "_stop_lock"):
            self._stop_lock = asyncio.Lock()
        if not hasattr(self, "_stop_task"):
            self._stop_task = None
        async with self._stop_lock:
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(self._stop_impl())
            task = self._stop_task
        await asyncio.shield(task)

    async def _stop_impl(self) -> None:
        if getattr(self, "_stop_complete", False):
            return
        log.info("stopping...")
        self._ready = False
        stop_event = getattr(self, "stop_event", None)
        task_names = (
            "_sweep_task",
            "_ingest_task",
            "_heartbeat_task",
            "_stream_task",
            "_leverage_task",
        )
        if stop_event is not None:
            stop_event.set()
        coordinator = getattr(self, "coordinator", None)
        if coordinator is not None:
            with contextlib.suppress(Exception):
                await coordinator.close_generation_admission()
            with contextlib.suppress(Exception):
                await coordinator.close_postfill_admission()

        tasks = []
        for name in task_names:
            task = getattr(self, name, None)
            setattr(self, name, None)
            if task is not None:
                tasks.append(task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        settings = getattr(self, "settings", None)
        if settings is not None:
            with contextlib.suppress(Exception):
                await settings.set_flag("ready", "false")
        if coordinator is not None:
            with contextlib.suppress(Exception):
                await coordinator.drain_postfill_tasks()
        http = getattr(self, "http", None)
        if http is not None:
            with contextlib.suppress(Exception):
                await http.aclose()
        db = getattr(self, "db", None)
        if db is not None:
            with contextlib.suppress(Exception):
                await db.close()
        self._stop_complete = True
        log.info("stopped")

    async def _heartbeat_loop(self) -> None:
        interval = min(max(self.cfg.reconcile_interval_s / 2, 1.0), 10.0)
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            if not await self.settings.heartbeat(self._runtime_id, now_ms()):
                log.error("runtime lease changed; stopping stale process")
                self.stop_event.set()
                return

    async def _consume(self, raw: dict) -> None:
        await self.projector.apply_raw(raw)

    async def _on_stream_event(self, event) -> None:
        """Async adapter required by UserStream's awaited callback contract."""
        self._enqueue(event.raw)

    async def _on_stream_connected(self) -> None:
        self._stream_connect_count += 1
        if not self._stream_ready.is_set():
            # UserStream awaits this callback before reading its first frame.
            # Therefore the second authoritative snapshot cannot race queued
            # pre-snapshot TRADE events and READY is published only afterwards.
            await self.projector.rebuild_full(self.master_client, "main")
            await self.reconciler.reconcile(self.projector.state, reason="post-stream-startup")
        self._stream_ready.set()

    def _enqueue(self, raw: dict) -> None:
        """Decouple WS ingest from follower execution: the stream loop must
        never wait on dozens of REST calls. Bounded queue drops the oldest
        event under pressure (state reconciliation heals any gap)."""
        q = self._ingest_queue
        if q.qsize() >= q.maxsize:
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
            self._ingest_dropped += 1
        q.put_nowait(raw)

    async def _ingest_loop(self) -> None:
        while not self.stop_event.is_set():
            raw = await self._ingest_queue.get()
            try:
                await self._consume(raw)
            except Exception as exc:  # noqa: BLE001 - one bad event must not kill ingest
                log.error("ingest processing failed: %s", exc)

    async def _on_delta(self, delta) -> None:
        # A user-data event is a trigger, not an authoritative market quote.
        # Refresh mark prices for affected symbols before allowing entry,
        # deviation, or stop-side decisions. If the read fails, fail closed for
        # this delta; the periodic/reconnect sweep will retry and closes remain
        # recoverable from the exchange snapshot.
        try:
            await self.projector.refresh_mark_prices(
                self.master_client, "main", delta.changed_symbols
            )
        except Exception as exc:  # noqa: BLE001 - safety gate around REST refresh
            log.error("master mark refresh failed: %s", exc)
            category = _error_category(exc)
            await self.settings.set_flag("degraded", f"master-mark-refresh:{category}")
            delta_stamp = _safe_fragment(getattr(delta.state, "event_time", 0), "snapshot")
            symbols = (
                "-".join(
                    sorted(
                        _safe_fragment(symbol) for symbol in getattr(delta, "changed_symbols", ())
                    )
                )
                or "all"
            )
            await self._emit_notification(
                event_key=(
                    f"degraded:master-mark-refresh:{self._notification_token}:"
                    f"{delta_stamp}:{symbols}:{category}"
                ),
                kind="engine.degraded",
                severity=NotificationSeverity.WARN,
                text=f"Не удалось обновить mark price master (категория: {category}).",
            )
            return
        for position in delta.state.positions.values():
            self.shadow_exchange.set_market_price(position.mark_price)
        await self.coordinator.on_master_delta(delta)

    async def _on_master_critical(self, reason: str) -> None:
        """Authoritative recovery for margin/liquidation/autoclose signals."""
        try:
            await self.projector.rebuild_full(self.master_client, "main")
            await self.reconciler.reconcile(self.projector.state, reason=f"critical:{reason}")
        except Exception as exc:  # noqa: BLE001 - keep stream alive, retry on sweep
            log.error("critical master resnapshot failed (%s): %s", reason, exc)
            category = _error_category(exc)
            safe_reason = _safe_fragment(reason)
            await self.settings.set_flag(
                "degraded", f"critical-resnapshot:{safe_reason}:{category}"
            )
            await self._emit_notification(
                event_key=(
                    f"critical:master-resnapshot:{self._notification_token}:"
                    f"{safe_reason}:{category}"
                ),
                kind="engine.critical_recovery",
                severity=NotificationSeverity.CRITICAL,
                text=(
                    "Критическое восстановление master не выполнено "
                    f"(событие: {safe_reason}, категория: {category})."
                ),
            )

    async def _on_stream_reconnect(self) -> None:
        self._stream_reconnect_count += 1
        log.info("master stream reconnected - resnapshotting")
        await self.projector.rebuild_full(self.master_client, "main")
        await self.reconciler.reconcile(self.projector.state, reason="reconnect")

    async def _on_confirmed(self, account_id: str, action, command_key: str | None = None) -> None:
        """Handle a durable confirmation without coupling it to delivery.

        The command key is supplied by ``Executor`` after its journal row is
        terminally CONFIRMED, so the notification key survives retries and
        restart recovery.  Post-fill admission is attempted independently of
        notification enqueue; a notifier outage cannot alter the confirmed
        execution result.
        """
        safe_account = _safe_fragment(account_id)
        safe_kind = _safe_fragment(getattr(action.kind, "value", action.kind))
        safe_symbol = _safe_fragment(getattr(action, "symbol", "symbol"))
        event_key = (
            f"command:{_safe_fragment(command_key, 'unknown')}"
            if command_key is not None
            else f"command:unknown:{safe_account}:{safe_kind}:{safe_symbol}"
        )
        formatted_text = format_confirmed_action(action, account_id=account_id)
        await self._emit_notification(
            event_key=event_key,
            kind="execution.confirmed",
            severity=NotificationSeverity.INFO,
            account_id=account_id,
            text=formatted_text,
        )
        try:
            await self.coordinator.on_command_confirmed(account_id, action)
        except Exception:  # noqa: BLE001 - post-fill must not rewrite CONFIRMED
            log.exception(
                "post-fill admission failed for confirmed command (account_id=%s key=%s)",
                safe_account,
                event_key,
            )

    async def _periodic_sweep(self) -> None:
        sweep_count = 0
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.cfg.reconcile_interval_s
                )
                return
            except TimeoutError:
                pass
            try:
                sweep_count += 1
                if (
                    not self._stream_ready.is_set()
                    or sweep_count % MASTER_RESNAPSHOT_EVERY_SWEEPS == 0
                ):
                    # authoritative master state: heals projector drift that the
                    # informational ACCOUNT_UPDATE only logs. Without a live
                    # stream this is also the primary state-change trigger.
                    await self.projector.rebuild_full(self.master_client, "main")
                report = await self.reconciler.reconcile(self.projector.state, reason="sweep")
                if all(created >= 0 for created in report.per_account.values()) and not (
                    await self.commands.in_flight()
                ):
                    await self.settings.clear_degraded()
            except Exception as exc:  # noqa: BLE001
                log.error("periodic sweep failed: %s", exc)

    # --- Stage 2 public follower/application boundaries ---------------------------

    @property
    def ready(self) -> bool:
        """Whether startup reconciliation and an authoritative refresh path are READY."""
        return self._ready

    @property
    def followers(self) -> list[FollowerRuntime]:
        """Compatibility view for Stage 1 scripts; mutations go through registry."""
        return list(self.follower_registry.snapshot())

    @followers.setter
    def followers(self, value: list[FollowerRuntime]) -> None:
        # Supports narrow App.__new__ fail-closed tests only.  Once Coordinator
        # is composed, replacing this object would split client routing from
        # generation snapshots, so normal runtime mutation must use the stable
        # registry instance.
        if hasattr(self, "coordinator"):
            raise RuntimeError("App.follower_registry is immutable after composition")
        self.follower_registry = FollowerRegistry(value)

    def runtime_for_row(self, row) -> FollowerRuntime:
        """Build the public runtime adapter for an enabled account row."""
        if not int(row["enabled"] or 0):
            raise ValueError(f"cannot publish disabled account {row['id']!r}")
        account_id = str(row["id"])
        execution_mode = str(row["execution_mode"] or "REAL")
        if execution_mode == "SHADOW":
            validate_shadow_boot(self.cfg, [row])
            self.shadow_exchange.add_account(account_id)
            client = self.shadow_exchange
        else:
            return self.new_real_follower_runtime(account_id)
        return FollowerRuntime(
            account_id=account_id,
            client=client,
            identity=None,
            execution_mode=execution_mode,
        )

    def new_real_follower_runtime(self, account_id: str) -> FollowerRuntime:
        """Build a REAL runtime whose client resolves encrypted cache on use.

        Credential replacement prepares this object before the atomic commit;
        the client deliberately holds no plaintext key and reads the updated
        ``SqliteCredentials`` cache only when a signed request is sent.
        """
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id is required")
        return FollowerRuntime(
            account_id=account_id,
            client=self._make_client(),
            identity=None,
            execution_mode="REAL",
        )

    def _runtime_for_row(self, row) -> FollowerRuntime:
        """Compatibility wrapper; callers should use :meth:`runtime_for_row`."""
        return self.runtime_for_row(row)

    async def register_follower(self, account_id: str) -> FollowerRuntime:
        """Publish only a follower that is not already in the runtime registry."""
        if self.follower_registry.get(account_id) is not None:
            raise ValueError(
                f"follower {account_id!r} already registered; use follower_lifecycle()"
            )
        row = await self.db.fetchone(
            "SELECT * FROM accounts WHERE id=? AND role='follower' AND enabled=1",
            (account_id,),
        )
        if row is None:
            raise KeyError(f"enabled follower account {account_id!r} not found")
        runtime = self.runtime_for_row(row)
        await self.follower_registry.publish_new(runtime)
        return runtime

    async def unregister_follower(self, account_id: str) -> FollowerRuntime | None:
        if self.follower_registry.get(account_id) is None:
            return None
        async with self.follower_lifecycle(account_id) as lifecycle:
            return await lifecycle.remove_when_safe()

    async def replace_follower(
        self, account_id: str, replacement: FollowerRuntime | None = None
    ) -> FollowerRuntime:
        """Replace a runtime only inside the explicit per-account lifecycle."""
        row = await self.db.fetchone(
            "SELECT * FROM accounts WHERE id=? AND role='follower' AND enabled=1",
            (account_id,),
        )
        if row is None:
            raise KeyError(f"enabled follower account {account_id!r} not found")
        new_runtime = replacement or self.runtime_for_row(row)
        async with self.follower_lifecycle(account_id) as lifecycle:
            await lifecycle.publish_replacement(new_runtime)
        return new_runtime

    @asynccontextmanager
    async def follower_lifecycle(self, account_id: str):
        """Hold follower then executor locks for DB/cache + registry lifecycle work."""
        runtime = self.follower_registry.get(account_id)
        if runtime is None:
            raise KeyError(f"follower runtime {account_id!r} not found")
        async with runtime.lock, self.executor.mutation_context(account_id):
            if self.follower_registry.get(account_id) is not runtime:
                raise RuntimeError(f"follower {account_id!r} changed before lifecycle lock")
            lifecycle = FollowerLifecycle(self, runtime)
            lifecycle._active = True
            try:
                yield lifecycle
            finally:
                lifecycle._active = False
                lifecycle._safe_to_remove = False

    @asynccontextmanager
    async def follower_mutation(self, account_id: str):
        """Compatibility view of the locked lifecycle runtime."""
        async with self.follower_lifecycle(account_id) as lifecycle:
            yield lifecycle.runtime

    async def pause_entries(self, timeout_s: float = 5.0) -> None:
        await self.coordinator.pause_barrier(timeout_s=timeout_s)

    async def resume_entries(self) -> None:
        await self.coordinator.resume_after_pause()

    async def reopen_entries_while_paused(self) -> None:
        """Open generation admission without ever enabling new entries."""
        if await self.settings.get_flag("allow_new_entries") != "false":
            raise RuntimeError("cannot reopen paused admission while entries are enabled")
        await self.coordinator.open_generation_admission()

    async def snapshot_follower(self, account_id: str):
        runtime = self.follower_registry.get(account_id)
        if runtime is None:
            raise KeyError(f"follower runtime {account_id!r} not found")
        return await self.coordinator.snapshot_follower(runtime)

    async def reconcile_follower(self, account_id: str, reason: str = "application"):
        if self.follower_registry.get(account_id) is None:
            raise KeyError(f"follower runtime {account_id!r} not found")
        return await self.reconciler.reconcile(
            self.projector.state,
            account_ids=[account_id],
            reason=reason,
        )


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def AccountRepo_all(db: Database):
    rows = await db.fetchall("SELECT * FROM accounts ORDER BY id")
    return rows
