"""Application composition root. Boot order is fixed by docs/ARCHITECTURE.md §12."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
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
from copytradeaster.master.projector import MasterProjector, symbols_for_raw
from copytradeaster.metrics import EngineMetrics
from copytradeaster.notifications.console import ConsoleNotifier
from copytradeaster.notifications.formatter import format_confirmed_action
from copytradeaster.notifications.models import NotificationEvent, NotificationSeverity
from copytradeaster.notifications.protocol import NotificationCallback, Notifier, as_notifier
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
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
from copytradeaster.trading.reconciler import Reconciler, build_snapshot_scope
from copytradeaster.trading.registry import FollowerRegistry
from copytradeaster.trading.snapshots import ACCOUNT_RESOURCES, FollowerSnapshotStore

log = get_logger(__name__)
CLOCK_DRIFT_MAX_MS = 2000
# Teardown bounds: every stop() step is joined with STOP_STEP_TIMEOUT_S and
# stop() itself with STOP_TOTAL_TIMEOUT_S, so a failed boot (half-constructed
# tasks, stuck joins) can never hang teardown — the impl keeps running in the
# background while the caller is released.
STOP_STEP_TIMEOUT_S = 5.0
STOP_TOTAL_TIMEOUT_S = 30.0
MASTER_RESNAPSHOT_EVERY_SWEEPS = 4  # ~4x reconcile_interval (~110s at 27s cadence;
# 10x reached ~270s — slower than test timeouts)
MASTER_BOUNDARY_MAX_PASSES = 3
MASTER_INGEST_MAX_AGE_S = 5.0
INGEST_QUEUE_MAX = 1000
STREAM_READY_TIMEOUT_S = 30.0
TESTNET_REST_URL = "https://fapi.asterdex-testnet.com"
TESTNET_WS_URL = "wss://fstream.asterdex-testnet.com/ws"
_SAFE_FRAGMENT = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class MasterIngestEnvelope:
    raw: dict
    generation: int
    received_at: float


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
        # Load-gate recorder: limiter weight/wait, reconcile durations, and
        # ingest age/depth are observed here and asserted live via
        # metrics_snapshot(). Constructed before the limiter so every
        # acquire is recorded from boot.
        self.metrics = EngineMetrics()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.limiter = RateLimits(
            cfg.weight_limit_per_min,
            cfg.orders_limit_per_min,
            cfg.orders_limit_per_10s,
            metrics=self.metrics,
        )
        self.credentials = SqliteCredentials(
            self.db,
            cfg.encryption_key_bytes(),
            trading_chain_id=cfg.network_profile.trading_chain_id,
        )
        self.settings = SettingsRepo(self.db)
        self.events = MasterEventRepo(self.db)
        self.trades = CopyTradeRepo(self.db)
        self.links = OrderLinkRepo(self.db)
        self.commands = CommandRepo(self.db)
        self.history = HistoryRepository(self.db)
        self.episodes = EpisodeRepository(self.db)
        self.user_events = EventRepository(self.db)
        self.history_mark_dirty = None
        self.history_live_publish = None
        self.nonces = NonceStore(self.db)
        self.basis = SyncBasisRepo(self.db)
        self.follower_registry = FollowerRegistry(max_real_followers=cfg.max_followers)
        self.follower_snapshots = FollowerSnapshotStore()
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
            mutation_context=self.executor.mutation_context,
            operator_max_leverage=cfg.operator_max_leverage,
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
            entry_admission=self._master_entry_admitted,
            operator_max_leverage=cfg.operator_max_leverage,
            max_total_risk_pct=cfg.max_total_risk_pct,
            snapshot_observer=self.follower_snapshots.publish,
        )
        self.reconciler = Reconciler(self.coordinator, metrics=self.metrics)
        self.stream: UserStream | None = None
        self.stop_event = asyncio.Event()
        self._stream_ready = asyncio.Event()
        self._stream_connected = False
        self._stream_epoch = 0
        self._entry_admission_ready = False
        self._ingest_queue: asyncio.Queue[MasterIngestEnvelope] = asyncio.Queue(
            maxsize=INGEST_QUEUE_MAX
        )
        self._ingest_dropped = 0
        # Distinct envelope generations currently queued (load-gate gauge).
        self._pending_generations: dict[int, int] = {}
        self._master_event_seq = 0
        self._master_boundary_active = False
        self._master_recovery_requested = asyncio.Event()
        self._master_recovery_reason = "master-dirty"
        self._master_recovery_task: asyncio.Task | None = None
        self._master_boundary_lock = asyncio.Lock()
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

    def _master_entry_admitted(self) -> bool:
        """Process-local proof required for any follower risk increase."""
        return (
            self._entry_admission_ready
            and len(self._real_follower_ids()) <= self.cfg.max_followers
            and self._stream_connected
            and not self.projector.dirty_symbols
        )

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
            else self.cfg.network_profile.name.lower()
        )
        await self.history.observe_order(
            HistoryScope(account_id, environment),
            order,
            kind=kind,
            command_key=command_key,
            observed_at=order.update_time,
        )
        hook = self.history_mark_dirty
        if hook is not None:
            try:
                hook(account_id, environment, order.symbol)
            except Exception:  # noqa: BLE001 - a hint must never break trading
                log.warning("history dirty hint failed account_id=%s", account_id)

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

    async def _mainnet_preflight(self) -> None:
        from copytradeaster.onboarding.models import DirectOnboardingConfig
        from copytradeaster.onboarding.preflight import run_mainnet_preflight

        cfg = self.cfg
        for account_id in ["main", *self._real_follower_ids()]:
            identity = self.credentials.identity(account_id)
            account = await self.db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
            builder = (
                cfg.builder_address
                if account is not None and account["identity_source"] == "builder"
                else ""
            )
            report = await run_mainnet_preflight(
                profile=cfg.network_profile,
                settings=cfg,
                reads=self._client_for(account_id),
                commands=self.commands,
                account_id=account_id,
                expected_signer_address=identity.signer,
                user_address=identity.user,
                agent_config=DirectOnboardingConfig.from_profile(
                    cfg.network_profile,
                    expected_egress_ips=tuple(cfg.aster_egress_ips),
                    require_agent_ip_whitelist=cfg.require_agent_ip_whitelist,
                    expected_position_mode=cfg.position_mode,
                ),
                builder_address=builder,
                builder_max_fee_rate=cfg.max_fee_rate,
                now_ms=now_ms,
            )
            if report.blocked:
                raise RuntimeError(
                    f"mainnet preflight {account_id}: {','.join(report.blocked_names())}"
                )

    async def start(self) -> None:
        cfg = self.cfg
        self._entry_admission_ready = False
        self._stream_connected = False
        self._stream_epoch = 0
        self._stream_ready.clear()
        self._ready = False
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

        if cfg.network_profile.is_mainnet:
            await self._mainnet_preflight()

        # 5. Mainnet requires a verified mode and never auto-converts it.
        # Testnet retains its explicit warn-and-fix bootstrap behavior.
        want_hedge = cfg.position_mode == "HEDGE"
        mode_is_hedge: bool | None = None
        try:
            mode_is_hedge = await self.master_client.get_position_mode("main")
        except Exception as exc:  # noqa: BLE001 - Testnet may fail account config reads
            if cfg.network_profile.is_mainnet:
                raise RuntimeError("mainnet master position mode unreadable") from exc
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
        if mode_is_hedge is not None and mode_is_hedge != want_hedge:
            if cfg.network_profile.is_mainnet:
                raise RuntimeError("mainnet master position mode changed after preflight")
            log.warning("position mode mismatch on master - setting %s", cfg.position_mode)
            await self.master_client.set_position_mode("main", want_hedge)
        for fr in self.followers:
            if fr.execution_mode == "SHADOW":
                continue
            try:
                client = fr.client
                is_hedge = await client.get_position_mode(fr.account_id)
                if is_hedge != want_hedge:
                    if cfg.network_profile.is_mainnet:
                        raise RuntimeError("mainnet follower mode changed after preflight")
                    log.warning("position mode mismatch on %s - fixing", fr.account_id)
                    await client.set_position_mode(fr.account_id, want_hedge)
            except Exception as exc:  # noqa: BLE001
                if cfg.network_profile.is_mainnet:
                    raise RuntimeError(
                        f"mainnet follower mode unverified: {fr.account_id}"
                    ) from exc
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
        # Feed REST marks to shadows (phantom-fill fix); WS deltas carry mark 0.
        self._sync_shadow_marks()

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
        await self._record_sweep_failures(
            await self.reconciler.reconcile(self.projector.state, reason="startup")
        )
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
            on_disconnected=self._on_stream_disconnected,
        )
        # Consume immediately: UserStream starts recv before its connected
        # callback so frames can accumulate while the post-connect REST
        # boundary runs. The generation boundary makes those frames harmless;
        # delaying the consumer until READY would hide queue pressure and
        # prevent the boundary from observing a non-quiescent stream.
        self._ingest_task = asyncio.create_task(self._ingest_loop())
        stream_task = asyncio.create_task(self.stream.run(self.stop_event))
        self._stream_task = stream_task
        stream_ready = await self._wait_for_stream_startup()
        self._sweep_task = asyncio.create_task(self._periodic_sweep())
        if not stream_ready:
            # The engine remains alive for close-only REST sweeps and future WS
            # retries, but it is deliberately not READY for new exposure.
            await self._set_master_not_ready("stream-startup-timeout")

    async def _wait_for_stream_startup(self) -> bool:
        """Wait for the post-connect boundary without opening on REST alone.

        The startup snapshot and reconciliation have already completed before
        this method runs.  When Testnet's listenKey endpoint is unavailable,
        UserStream keeps retrying and ``_periodic_sweep`` performs authoritative
        close-only REST convergence; new/increased exposure remains blocked.
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
                    "Поток master временно недоступен; разрешена только уменьшающая "
                    "риск REST-сверка до post-connect snapshot."
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
        """Idempotent, concurrent-safe teardown for READY and partial boots.

        Never hangs: _stop_impl bounds every step, and this wrapper bounds the
        total wait, so a failed boot always releases the teardown caller while
        the impl finishes in the background.
        """
        if not hasattr(self, "_stop_lock"):
            self._stop_lock = asyncio.Lock()
        if not hasattr(self, "_stop_task"):
            self._stop_task = None
        async with self._stop_lock:
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(self._stop_impl())
            task = self._stop_task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=STOP_TOTAL_TIMEOUT_S)
        except TimeoutError:
            # The impl task survives the shield and keeps releasing resources;
            # the caller must never hang teardown waiting for it.
            log.error(
                "stop timed out after %ss; background teardown continues", STOP_TOTAL_TIMEOUT_S
            )

    @staticmethod
    async def _bounded(label: str, coro, timeout_s: float = STOP_STEP_TIMEOUT_S) -> None:
        """Await one teardown step with a hard bound; failures stay local."""
        try:
            await asyncio.wait_for(coro, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - teardown steps never propagate
            log.warning("stop step %s failed: %s", label, _error_category(exc))

    async def _stop_impl(self) -> None:
        if getattr(self, "_stop_complete", False):
            return
        log.info("stopping...")
        try:
            self._ready = False
            self._entry_admission_ready = False
            self._stream_connected = False
            stop_event = getattr(self, "stop_event", None)
            if stop_event is not None:
                stop_event.set()
            coordinator = getattr(self, "coordinator", None)
            if coordinator is not None:
                await self._bounded(
                    "close-generation-admission", coordinator.close_generation_admission()
                )
                await self._bounded(
                    "close-postfill-admission", coordinator.close_postfill_admission()
                )

            task_names = (
                "_sweep_task",
                "_ingest_task",
                "_heartbeat_task",
                "_stream_task",
                "_leverage_task",
                "_master_recovery_task",
            )
            tasks = []
            for name in task_names:
                # Half-constructed boots leave these missing/None; detach first
                # so a concurrent stop() never double-joins the same task.
                task = getattr(self, name, None)
                setattr(self, name, None)
                if task is not None:
                    task.cancel()
                    tasks.append(task)
            if tasks:
                # One bound for the whole join: a task stuck in an
                # un-cancellable await cannot hang teardown. gather with
                # return_exceptions never raises from children; the wait_for
                # bounds the wait itself.
                await self._bounded(
                    "join-tasks",
                    asyncio.gather(*tasks, return_exceptions=True),
                )

            settings = getattr(self, "settings", None)
            if settings is not None:
                await self._bounded("ready-false", settings.set_flag("ready", "false"))
            if coordinator is not None:
                await self._bounded(
                    "drain-postfill",
                    coordinator.drain_postfill_tasks(),
                    timeout_s=STOP_STEP_TIMEOUT_S + 5.0,
                )
            # Release the master listenKey BEFORE closing HTTP: the DELETE
            # needs the socket pool, and an unreleased key holds the
            # exchange-side stream open after a failed boot. Best-effort and
            # bounded; fakes without close_listen_key are skipped.
            if getattr(self, "stream", None) is not None:
                master_client = getattr(self, "master_client", None)
                close_key = getattr(master_client, "close_listen_key", None)
                if callable(close_key):
                    await self._bounded("close-listen-key", close_key("main"))
            http = getattr(self, "http", None)
            if http is not None:
                await self._bounded("http-close", http.aclose())
            db = getattr(self, "db", None)
            if db is not None:
                await self._bounded("db-close", db.close())
        finally:
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

    async def _consume(self, envelope: MasterIngestEnvelope) -> None:
        # Match Coordinator's 5s master-freshness convention: equality is
        # accepted, only age strictly greater than the budget is stale.
        age_s = max(time.monotonic() - envelope.received_at, 0.0)
        metrics = getattr(self, "metrics", None)
        if metrics is not None:
            metrics.observe_ingest_age(age_s)
        if age_s > MASTER_INGEST_MAX_AGE_S:
            self._mark_master_dirty(envelope.raw, "ingest-backlog-stale")
            return
        await self.projector.apply_raw(envelope.raw, generation=envelope.generation)

    async def _on_stream_event(self, event) -> None:
        """Async adapter required by UserStream's awaited callback contract."""
        self._enqueue(event.raw, received_at=event.received_at)

    async def _on_stream_connected(self) -> None:
        self._stream_connect_count += 1
        self._stream_epoch = getattr(self, "_stream_epoch", 0) + 1
        self._stream_connected = True
        self.projector.mark_dirty("*")
        await self._establish_master_boundary("stream-connected")

    async def _on_stream_disconnected(self) -> None:
        self._stream_epoch = getattr(self, "_stream_epoch", 0) + 1
        self._stream_connected = False
        self.projector.mark_dirty("*")
        await self._set_master_not_ready("stream-disconnected")

    def _enqueue(self, raw: dict, *, received_at: float | None = None) -> None:
        """Queue one frame with the current authoritative generation."""
        q = self._ingest_queue
        self._master_event_seq = getattr(self, "_master_event_seq", 0) + 1
        if q.full():
            dropped: MasterIngestEnvelope | None = None
            with contextlib.suppress(asyncio.QueueEmpty):
                dropped = q.get_nowait()
                q.task_done()
            self._ingest_dropped += 1
            if dropped is not None:
                pending_overflow = getattr(self, "_pending_generations", None)
                if pending_overflow is not None:
                    self._release_pending_generation(
                        pending_overflow, getattr(dropped, "generation", None)
                    )
                self._mark_master_dirty(dropped.raw, "ingest-overflow")
        generation = self.projector.generation
        q.put_nowait(
            MasterIngestEnvelope(
                raw=raw,
                generation=generation,
                received_at=time.monotonic() if received_at is None else received_at,
            )
        )
        pending = getattr(self, "_pending_generations", None)
        if pending is not None:
            pending[generation] = pending.get(generation, 0) + 1
        metrics = getattr(self, "metrics", None)
        if metrics is not None:
            metrics.set_ingest(
                q.qsize(),
                len(pending) if pending is not None else 0,
                getattr(self, "_ingest_dropped", 0),
            )
        if getattr(self, "_master_boundary_active", False):
            # The REST reads and follower close-only sweep were not quiescent.
            # Close local entry admission synchronously and coalesce a new
            # authoritative boundary; the queued frame keeps its old
            # generation and cannot mutate the eventual snapshot.
            self._mark_master_dirty(raw, "event-during-master-boundary")

    def _mark_master_dirty(self, raw: dict, reason: str) -> None:
        self.projector.mark_dirty(*symbols_for_raw(raw))
        self._entry_admission_ready = False
        self._ready = False
        self._stream_ready.clear()
        self._master_recovery_reason = reason
        self._master_recovery_requested.set()
        if getattr(self, "_master_boundary_active", False):
            # The active boundary observes event_seq/dirty and performs the
            # next coalesced pass itself. Its finally block schedules recovery
            # only if a later event remains unconsumed.
            return
        self._ensure_master_recovery_task()

    def _ensure_master_recovery_task(self) -> None:
        stop_event = getattr(self, "stop_event", None)
        if stop_event is not None and stop_event.is_set():
            return
        task = getattr(self, "_master_recovery_task", None)
        if task is None or task.done():
            self._master_recovery_task = asyncio.create_task(self._master_recovery_loop())

    async def _master_recovery_loop(self) -> None:
        while self._master_recovery_requested.is_set() and not self.stop_event.is_set():
            self._master_recovery_requested.clear()
            reason = self._master_recovery_reason
            try:
                await self._establish_master_boundary(reason)
            except Exception as exc:  # noqa: BLE001 - periodic/reconnect retries
                log.error("dirty master recovery failed (%s): %s", reason, exc)
                await self._set_master_not_ready(f"master-recovery:{reason}:{_error_category(exc)}")
                return

    async def _set_master_not_ready(self, reason: str) -> None:
        self._entry_admission_ready = False
        self._ready = False
        self._stream_ready.clear()
        await self.settings.set_flag("ready", "false")
        await self.settings.set_flag("degraded", reason)

    def _publish_system_state_change(self) -> None:
        """Revalidate AccountState-backed DTOs without another Aster read.

        Dashboard, Positions and Status combine a follower snapshot with
        process-wide READY/degraded flags. A master boundary can publish a
        follower snapshot while those flags are deliberately fail-closed, then
        prove READY without changing follower state again. Re-publish the
        existing snapshot after that flag transition so live clients perform a
        cheap cache read and cannot retain the transient degraded verdict.

        ``FollowerSnapshotStore.republish`` preserves REST ``captured_at``;
        this is a presentation boundary, never fabricated exchange freshness.
        """
        snapshots = getattr(self, "follower_snapshots", None)
        registry = getattr(self, "follower_registry", None)
        if snapshots is None or registry is None:
            return
        for runtime in registry.snapshot():
            if runtime.execution_mode != "REAL":
                continue
            snapshots.republish(
                runtime.account_id,
                resources=ACCOUNT_RESOURCES,
                source="system-state",
            )

    async def _publish_master_ready(
        self,
        *,
        was_ready: bool,
        stream_epoch: int,
        event_seq: int,
    ) -> bool:
        """Publish READY only if the proven boundary is still current."""
        await self.settings.set_flag("ready", "true")
        if (
            not self._stream_connected
            or getattr(self, "_stream_epoch", 0) != stream_epoch
            or getattr(self, "_master_event_seq", 0) != event_seq
            or self.projector.dirty_symbols
        ):
            await self._set_master_not_ready("master-changed-before-ready")
            return False
        # No await between the final proof and the process-local admission
        # commit: this is the linearization point for startup/reconnect entry.
        self._entry_admission_ready = True
        self._ready = True
        self._stream_ready.set()
        if not was_ready:
            await self._emit_notification(
                event_key=f"app:{self._notification_token}:ready",
                kind="engine.ready",
                severity=NotificationSeverity.INFO,
                text="Движок готов к работе.",
            )
        log.info("READY master_generation=%d", self.projector.generation)
        return True

    @staticmethod
    def _reconcile_succeeded(report) -> bool:
        return all(created >= 0 for created in report.per_account.values())

    @staticmethod
    def _reachable_followers(report) -> set[str]:
        """Accounts whose sweep read+recompute succeeded (no -1, no failure)."""
        per_account = getattr(report, "per_account", None) or {}
        failures = getattr(report, "failures", None) or {}
        return {aid for aid, n in per_account.items() if n >= 0 and aid not in failures}

    async def _unsettled_for(self, account_ids: set[str]) -> list:
        """In-flight commands scoped to reachable accounts only.

        A faulted follower's unsettled rows must not freeze healthy ones;
        its closes retry every sweep once its reads heal.
        """
        if not account_ids:
            return []
        return [
            row for row in await self.commands.in_flight() if str(row["account_id"]) in account_ids
        ]

    async def _establish_master_boundary(self, reason: str) -> bool:
        """Rebuild master and validate followers before admitting increases."""
        lock = getattr(self, "_master_boundary_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._master_boundary_lock = lock
        async with lock:
            was_ready = self._ready
            self._master_boundary_active = True
            try:
                await self._set_master_not_ready(reason)
                stream_epoch = getattr(self, "_stream_epoch", 0)
                for attempt in range(1, MASTER_BOUNDARY_MAX_PASSES + 1):
                    recovery_requested = getattr(self, "_master_recovery_requested", None)
                    if recovery_requested is not None:
                        recovery_requested.clear()
                    event_seq = getattr(self, "_master_event_seq", 0)
                    await self.projector.rebuild_full(self.master_client, "main")
                    # REST snapshot carries real marks; WS deltas carry mark 0.
                    self._sync_shadow_marks()

                    close_only = await self.reconciler.reconcile(
                        self.projector.state, reason="post-stream-close-only"
                    )
                    # F2 isolation bug: a faulted follower (-2015 on every read)
                    # aborted the whole boundary here, freezing healthy ones.
                    # Per-follower failures are tolerated — recorded via degraded
                    # and excluded from completion, which is computed over
                    # REACHABLE followers only. Only master-side read failures
                    # (rebuild_full/reconcile raising) abort the boundary. A
                    # faulted follower has no readable snapshot so it gets no
                    # entries (sizing impossible); its closes retry every sweep.
                    await self._record_sweep_failures(close_only)
                    reachable = self._reachable_followers(close_only)
                    faulted = set(getattr(close_only, "per_account", None) or {}) - reachable
                    if faulted:
                        log.info(
                            "boundary tolerating faulted followers: %s",
                            ",".join(sorted(_safe_fragment(a) for a in faulted)),
                        )
                    if await self._unsettled_for(reachable):
                        raise RuntimeError("close-only follower reconciliation incomplete")
                    if (
                        not self._stream_connected
                        or getattr(self, "_stream_epoch", 0) != stream_epoch
                    ):
                        return False
                    if (
                        getattr(self, "_master_event_seq", 0) != event_seq
                        or self.projector.dirty_symbols
                    ):
                        log.info(
                            "master boundary pass %d/%d was not quiescent; retrying",
                            attempt,
                            MASTER_BOUNDARY_MAX_PASSES,
                        )
                        self.projector.mark_dirty("*")
                        continue

                    # PM-B3 requires the entry snapshot after the potentially
                    # slow close-only follower sweep. This second REST cutover
                    # is the state used for risk-increasing reconciliation.
                    await self.projector.rebuild_full(self.master_client, "main")
                    # Entry cutover carries the freshest REST marks.
                    self._sync_shadow_marks()
                    if (
                        not self._stream_connected
                        or getattr(self, "_stream_epoch", 0) != stream_epoch
                        or getattr(self, "_master_event_seq", 0) != event_seq
                        or self.projector.dirty_symbols
                    ):
                        self.projector.mark_dirty("*")
                        continue

                    # This assignment is adjacent to the quiescence proof and
                    # contains no await. Events after it belong to the new
                    # generation and are normal live-stream work.
                    self._entry_admission_ready = True
                    entry = await self.reconciler.reconcile(
                        self.projector.state, reason="post-stream-entry"
                    )
                    self._entry_admission_ready = False
                    # Same tolerance as close-only: a faulted follower gets no
                    # entries (its recompute raised before planning) while
                    # healthy followers converge; only reachable in-flight
                    # blocks admission.
                    await self._record_sweep_failures(entry)
                    if await self._unsettled_for(self._reachable_followers(entry)):
                        raise RuntimeError("entry follower reconciliation incomplete")
                    if (
                        not self._stream_connected
                        or getattr(self, "_stream_epoch", 0) != stream_epoch
                        or getattr(self, "_master_event_seq", 0) != event_seq
                        or self.projector.dirty_symbols
                    ):
                        self.projector.mark_dirty("*")
                        continue
                    published = await self._publish_master_ready(
                        was_ready=was_ready,
                        stream_epoch=stream_epoch,
                        event_seq=event_seq,
                    )
                    if published and await self._sweep_hold_reason(entry) is None:
                        await self.settings.clear_degraded()
                        self._publish_system_state_change()
                    return published

                self.projector.mark_dirty("*")
                await self._set_master_not_ready("master-boundary-not-quiescent")
                return False
            finally:
                self._master_boundary_active = False
                recovery_requested = getattr(self, "_master_recovery_requested", None)
                if recovery_requested is not None and recovery_requested.is_set():
                    self._ensure_master_recovery_task()

    @staticmethod
    def _release_pending_generation(pending: dict[int, int], generation) -> None:
        """Decrement one queued envelope's generation count (overflow/consume)."""
        if generation is None:
            return
        try:
            key = int(generation)
        except (TypeError, ValueError):
            return
        left = pending.get(key, 0) - 1
        if left <= 0:
            pending.pop(key, None)
        else:
            pending[key] = left

    async def _ingest_loop(self) -> None:
        while not self.stop_event.is_set():
            envelope = await self._ingest_queue.get()
            pending = getattr(self, "_pending_generations", None)
            if pending is not None:
                self._release_pending_generation(pending, getattr(envelope, "generation", None))
            try:
                await self._consume(envelope)
            except Exception as exc:  # noqa: BLE001 - one bad event must not kill ingest
                log.error("ingest processing failed: %s", exc)
            finally:
                self._ingest_queue.task_done()

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
            self.projector.mark_dirty("*")
            await self._establish_master_boundary(f"critical:{reason}")
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
        log.info("master stream transport reconnected - awaiting authoritative boundary")
        await self._set_master_not_ready("stream-reconnecting")

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

    def _sync_shadow_marks(self) -> None:
        """Push REST-snapshot marks into the shadow feed (phantom-fill fix).

        WS-trade projector positions carry mark_price=0, and
        ShadowExchange.set_market_price ignores non-positive prices, so the
        WS-delta sync alone leaves the shadow market at the 65000 default
        forever while BTC trades ~80k — shadow MARKET fills then execute at
        the phantom price and post_fill_cap flattens the leg as insane
        (measured RISK_CAP ~1s after every shadow open). The boundary/startup
        REST snapshots carry real marks; sync them here. Non-positive marks
        are skipped by set_market_price. The WS-delta sync stays as-is.
        """
        exchange = getattr(self, "shadow_exchange", None)
        state = getattr(getattr(self, "projector", None), "state", None)
        positions = getattr(state, "positions", None) or {}
        if exchange is None or not positions:
            return
        for position in positions.values():
            mark = getattr(position, "mark_price", None)
            if mark is None:
                continue
            exchange.set_market_price(mark)

    async def _sweep_snapshot_scope(self) -> set[str] | None:
        """Scoped production snapshot: whitelist + master episodes + open copy episodes.

        Replaces the weight-40 all-symbol openOrders read per follower per sweep
        with per-symbol weight-1 reads. Returns None (authoritative all-symbol)
        when the scope would be empty or unreadable — a sweep must never read
        nothing. The periodic boundary pass below always reconciles all-symbol,
        which is the bounded fallback healing any symbol a scoped sweep misses.
        """
        try:
            open_rows = await self.trades.all_open()
        except Exception as exc:  # noqa: BLE001 - fail closed to authoritative
            log.warning("sweep scope unreadable (%s); using all-symbol snapshot", exc)
            return None
        master_symbols = [str(key.symbol) for key in self.projector.state.positions]
        episode_symbols = [str(row["symbol"]) for row in open_rows]
        scope = build_snapshot_scope(
            whitelist=self.cfg.symbol_whitelist,
            master_symbols=master_symbols,
            episode_symbols=episode_symbols,
        )
        return scope or None

    async def _record_sweep_failures(self, report) -> list[str]:
        """Raise the degraded flag for every per-follower sweep failure.

        F2 gap: a follower whose reads+writes all raise (e.g. -2015) leaves
        no journal row, so without this the sweep was silent. Called on EVERY
        sweep (set-before-evaluate) with account + secret-safe error category
        only — no keys, signatures, or payloads. No order-flow change.
        """
        failures = getattr(report, "failures", None) or {}
        if not failures:
            # Foreign reports (doubles) may carry -1 without categories.
            per_account = getattr(report, "per_account", None) or {}
            failures = {aid: "error" for aid, n in per_account.items() if n < 0}
        recorded: list[str] = []
        for account_id in sorted(failures):
            reason = (
                f"follower:{_safe_fragment(account_id)}"
                f":sweep-failed:{_safe_fragment(failures[account_id])}"
            )
            await self.settings.set_flag("degraded", reason)
            recorded.append(reason)
        return recorded

    async def _sweep_hold_reason(self, report) -> str | None:
        """Authoritative convergence proof required before clearing degraded.

        BUG FIX: an exception-free sweep is NOT convergence. Degraded clears
        only when a sweep proves every follower converged with zero new
        commands (actual == target, so protections present and no unresolved
        divergence — any gap would have produced a corrective command), the
        master is clean, no command is in-flight/UNKNOWN/FAILED, and the
        leverage control-plane has no outstanding work. Liquidation safety is
        covered transitively: a liq-driven protection or close would itself be
        a nonzero command. Returns None when proven, else the itemized hold.
        """
        try:
            # Fresh sweep failures hold first: a sweep that just recorded a
            # failure can never clear degraded, even if every other proof held.
            failures = getattr(report, "failures", None) or {}
            if not failures:
                per_account = getattr(report, "per_account", None) or {}
                failures = {aid: "error" for aid, n in per_account.items() if n < 0}
            if failures:
                shown = ",".join(
                    f"{_safe_fragment(a)}:{_safe_fragment(c)}"
                    for a, c in sorted(failures.items())[:5]
                )
                return f"sweep-failed:{shown}"
            if not self._ready:
                return "not-ready"
            if not getattr(report, "authoritative", False):
                return "scoped-snapshot"
            diverged = {
                aid: n for aid, n in (getattr(report, "per_account", None) or {}).items() if n != 0
            }
            if diverged:
                shown = ",".join(
                    f"{_safe_fragment(a)}={n}" for a, n in sorted(diverged.items())[:5]
                )
                return f"unconverged:{shown}"
            evaluated = set(getattr(report, "per_account", None) or {})
            if {fr.account_id for fr in self.followers} - evaluated:
                return "incomplete-follower-report"
            if self.projector.dirty_symbols:
                shown = ",".join(
                    sorted(_safe_fragment(s) for s in self.projector.dirty_symbols)[:5]
                )
                return f"master-dirty:{shown}"
            in_flight = await self.commands.in_flight()
            if in_flight:
                statuses = ",".join(sorted({str(row["status"]) for row in in_flight}))
                return f"in-flight:{statuses}:{len(in_flight)}"
            if len(self._real_follower_ids()) > self.cfg.max_followers:
                return "real-follower-cap-exceeded"
            if any(
                not self.leverage.is_ready(aid, symbol)
                for aid in self._real_follower_ids()
                for symbol in self.cfg.symbol_whitelist
            ):
                return "leverage-not-ready"
            pending = self.leverage.pending_count()
            if pending:
                return f"leverage-pending:{pending}"
            return None
        except Exception as exc:  # noqa: BLE001 - a broken gate holds degraded, never clears
            return f"gate-error:{_error_category(exc)}"

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
                    not self._stream_connected
                    or self.projector.dirty_symbols
                    or sweep_count % MASTER_RESNAPSHOT_EVERY_SWEEPS == 0
                ):
                    # Without a live stream this stays close-only. With a live
                    # stream the two-phase boundary may reopen local increases.
                    # The boundary reconciles with the authoritative all-symbol
                    # snapshot, which is also the bounded fallback healing any
                    # symbol a scoped production sweep could not see.
                    await self._establish_master_boundary("periodic-snapshot")
                    continue
                # State marks (REST-sourced) keep shadows tracking between
                # boundaries; WS-delta zeros are skipped by the sync guard.
                self._sync_shadow_marks()
                scope = await self._sweep_snapshot_scope()
                report = await self.reconciler.reconcile(
                    self.projector.state, reason="sweep", snapshot_scope=scope
                )
                # Set-before-evaluate: record per-follower failures BEFORE the
                # hold check, so a sweep that just failed can never clear.
                # The hold check re-reads report.failures as a second latch.
                await self._record_sweep_failures(report)
                hold_reason = await self._sweep_hold_reason(report)
                if hold_reason is None:
                    await self.settings.clear_degraded()
                    self._publish_system_state_change()
                else:
                    log.info("sweep holds degraded (%s)", hold_reason)
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

    def metrics_snapshot(self) -> dict:
        """Bounded load-gate view: p50/p95/p99 per series plus live gauges.

        The load gate asserts weight/min, reconcile/follower durations,
        limiter waits, ingest queue age/depth, and pending generations here.
        """
        metrics = getattr(self, "metrics", None)
        if metrics is None:
            return {}
        queue = getattr(self, "_ingest_queue", None)
        pending = getattr(self, "_pending_generations", None)
        if queue is not None:
            # Gauges must never break the gate read.
            with contextlib.suppress(Exception):
                metrics.set_ingest(
                    queue.qsize(),
                    len(pending) if pending is not None else 0,
                    getattr(self, "_ingest_dropped", 0),
                )
        postfill = getattr(getattr(self, "coordinator", None), "_postfill_tasks", None)
        # Gauges must never break the gate read.
        with contextlib.suppress(Exception):
            metrics.set_postfill_pending(len(postfill) if postfill is not None else 0)
        return metrics.snapshot()


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def AccountRepo_all(db: Database):
    rows = await db.fetchall("SELECT * FROM accounts ORDER BY id")
    return rows
