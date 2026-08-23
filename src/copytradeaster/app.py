"""Application composition root. Boot order is fixed by docs/ARCHITECTURE.md §12."""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.shadow import ShadowExchange
from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.aster.user_stream import UserStream
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.logging_utils import get_logger, setup_logging
from copytradeaster.master.projector import MasterProjector
from copytradeaster.notifications.console import notify
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CommandRepo,
    CopyTradeRepo,
    MasterEventRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor, resolve_boot_unknowns
from copytradeaster.trading.reconciler import Reconciler

log = get_logger(__name__)
CLOCK_DRIFT_MAX_MS = 2000
MASTER_RESNAPSHOT_EVERY_SWEEPS = 10  # ~10x reconcile_interval; heals projector drift
INGEST_QUEUE_MAX = 1000
STREAM_READY_TIMEOUT_S = 30.0


class App:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.db = Database(cfg.db_path)
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
        self.nonces = NonceStore(self.db)
        self.basis = SyncBasisRepo(self.db)
        self.followers: list[FollowerRuntime] = []
        self.shadow_exchange = ShadowExchange(
            hedge=cfg.position_mode == "HEDGE",
            auto_trigger_conditionals=cfg.shadow_auto_trigger_conditionals,
        )
        self.executor = Executor(
            self._client_for,
            self.commands,
            self.links,
            {},
            settings=self.settings,
            on_confirmed=self._on_confirmed,
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
        self.coordinator = Coordinator(
            followers=self.followers,
            settings=self.settings,
            executor=self.executor,
            trades=self.trades,
            links=self.links,
            filters_provider=self._filters,
            app_flags=self.settings,
            symbol_whitelist=cfg.symbol_whitelist,
            fee_buffer_pct=cfg.fee_buffer_pct,
            working_type=cfg.working_type,
            on_notify=notify,
            basis=self.basis,
            snapshot_symbols=set(cfg.follower_snapshot_symbols) or None,
        )
        self.reconciler = Reconciler(self.coordinator)
        self.stream: UserStream | None = None
        self.stop_event = asyncio.Event()
        self._stream_ready = asyncio.Event()
        self._ingest_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=INGEST_QUEUE_MAX)
        self._ingest_dropped = 0
        self._ingest_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._filters_cache: dict = {}
        self._ready = False

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

    def _client_for(self, account_id: str) -> AsterClient:
        """Fail closed: an unknown account must NEVER route to the master."""
        if account_id == "main":
            return self.master_client
        for fr in self.followers:
            if fr.account_id == account_id:
                return fr.client
        raise KeyError(f"unknown account {account_id!r} - refusing to route to master")

    def _filters(self) -> dict:
        return self._filters_cache

    # --- boot -------------------------------------------------------------------------

    async def start(self) -> None:
        cfg = self.cfg
        setup_logging(cfg.log_level)
        log.info("boot starting (db=%s dry_run=%s)", cfg.db_path, cfg.dry_run)

        # 1-2. storage + accounts/settings
        await self.db.connect()
        await self.db.migrate()
        accounts = await AccountRepo_all(self.db)
        shadow_rows = [row for row in accounts if str(row["execution_mode"] or "REAL") == "SHADOW"]
        if shadow_rows:
            if not cfg.shadow_accounts_enabled:
                raise RuntimeError("SHADOW accounts found but CT_SHADOW_ACCOUNTS_ENABLED is false")
            if "testnet" not in cfg.rest_base_url.lower():
                raise RuntimeError("SHADOW accounts are permitted only with an Aster testnet URL")
        main_row = next((row for row in accounts if str(row["id"]) == "main"), None)
        if main_row is not None and str(main_row["execution_mode"] or "REAL") != "REAL":
            raise RuntimeError("main account must always use execution_mode=REAL")
        await self.credentials.load()
        for row in accounts:
            if row["role"] == "follower" and int(row["enabled"]):
                execution_mode = str(row["execution_mode"] or "REAL")
                if execution_mode == "SHADOW":
                    self.shadow_exchange.add_account(str(row["id"]))
                    client = self.shadow_exchange
                else:
                    client = self._make_client()
                self.followers.append(
                    FollowerRuntime(
                        account_id=str(row["id"]),
                        client=client,
                        identity=None,
                        execution_mode=execution_mode,
                    )
                )

        # 3. clock drift check (nonce window +-10s)
        server_ms = await self.master_client.server_time()
        drift = abs(server_ms - now_ms())
        if drift > CLOCK_DRIFT_MAX_MS:
            raise RuntimeError(f"clock drift {drift}ms exceeds {CLOCK_DRIFT_MAX_MS}ms")
        log.info("clock drift ok (%dms)", drift)

        # 4. resolve UNKNOWN commands from previous run
        resolved = await resolve_boot_unknowns(self._client_for, self.commands, self.links)
        log.info("boot unknown-resolution: %d confirmed", resolved)

        # 5. bootstrap position modes + snapshots
        mode_is_hedge = await self.master_client.get_position_mode("main")
        want_hedge = cfg.position_mode == "HEDGE"
        if mode_is_hedge != want_hedge:
            log.warning("position mode mismatch on master - setting %s", cfg.position_mode)
            try:
                await self.master_client.set_position_mode("main", want_hedge)
            except Exception as exc:  # noqa: BLE001 - -4068: open positions block switch
                log.warning("master position mode not changed (%s); continuing as-is", exc)
                await notify(f"WARN master position mode not changed: {exc}")
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
        exchange_info = await self.master_client.exchange_info(force=True)
        self._filters_cache = exchange_info.tradable_filters()
        await self.projector.rebuild_full(self.master_client, "main")

        # 6. startup reconciliation
        await notify("startup reconciliation...")
        await self.reconciler.reconcile(self.projector.state, reason="startup")

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
        self._stream_task = stream_task  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(self._stream_ready.wait(), timeout=STREAM_READY_TIMEOUT_S)
        except TimeoutError as exc:
            raise RuntimeError("master user stream did not become ready") from exc
        self._sweep_task = asyncio.create_task(self._periodic_sweep())
        self._ingest_task = asyncio.create_task(self._ingest_loop())
        # 8. READY
        await self.settings.set_flag("ready", "true")
        self._ready = True
        await notify("READY")
        log.info("READY")

    async def run_until_stopped(self) -> None:
        await self.start()
        try:
            await self.stop_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        log.info("stopping...")
        self.stop_event.set()
        for task in (
            self._sweep_task,
            self._ingest_task,
            getattr(self, "_stream_task", None),
        ):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        with contextlib.suppress(Exception):
            await self.settings.set_flag("ready", "false")
        await self.http.aclose()
        await self.db.close()
        log.info("stopped")

    async def _consume(self, raw: dict) -> None:
        await self.projector.apply_raw(raw)

    async def _on_stream_event(self, event) -> None:
        """Async adapter required by UserStream's awaited callback contract."""
        self._enqueue(event.raw)

    async def _on_stream_connected(self) -> None:
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
            await self.settings.set_flag("degraded", f"master-mark-refresh:{exc}")
            await notify(f"WARN master mark refresh failed: {exc}")
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
            await self.settings.set_flag("degraded", f"critical-resnapshot:{reason}:{exc}")
            await notify(f"CRITICAL master resnapshot failed ({reason}): {exc}")

    async def _on_stream_reconnect(self) -> None:
        log.info("master stream reconnected - resnapshotting")
        await self.projector.rebuild_full(self.master_client, "main")
        await self.reconciler.reconcile(self.projector.state, reason="reconnect")

    async def _on_confirmed(self, account_id: str, action) -> None:
        text = f"[{account_id}] {action.kind.value} {action.symbol} ok"
        await notify(text)
        await self.coordinator.on_command_confirmed(account_id, action)

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
                if sweep_count % MASTER_RESNAPSHOT_EVERY_SWEEPS == 0:
                    # authoritative master state: heals projector drift that the
                    # informational ACCOUNT_UPDATE only logs
                    await self.projector.rebuild_full(self.master_client, "main")
                await self.reconciler.reconcile(self.projector.state, reason="sweep")
            except Exception as exc:  # noqa: BLE001
                log.error("periodic sweep failed: %s", exc)


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def AccountRepo_all(db: Database):
    rows = await db.fetchall("SELECT * FROM accounts ORDER BY id")
    return rows
