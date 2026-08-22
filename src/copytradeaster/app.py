"""Application composition root. Boot order is fixed by docs/ARCHITECTURE.md §12."""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from copytradeaster.aster.rate_limits import RateLimits
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
    OrderLinkRepo,
    SettingsRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor, resolve_boot_unknowns
from copytradeaster.trading.reconciler import Reconciler

log = get_logger(__name__)

CLOCK_DRIFT_MAX_MS = 2000


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

        self.master_client = self._make_client()
        self.followers: list[FollowerRuntime] = []
        self.executor = Executor(
            self._client_for,
            self.commands,
            self.links,
            {},
            settings=self.settings,
            on_confirmed=self._on_confirmed,
        )
        self.projector = MasterProjector(
            self.events, self.trades, self.links, self.settings, self._on_delta
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
        )
        self.reconciler = Reconciler(self.coordinator)
        self.stream: UserStream | None = None
        self.stop_event = asyncio.Event()
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
            nonces=self.credentials,
            dry_run=self.cfg.dry_run,
        )

    def _client_for(self, account_id: str) -> AsterClient:
        if account_id == "main":
            return self.master_client
        for fr in self.followers:
            if fr.account_id == account_id:
                return fr.client
        return self.master_client

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
        await self.credentials.load()
        accounts = await AccountRepo_all(self.db)
        for row in accounts:
            if row["role"] == "follower" and int(row["enabled"]):
                self.followers.append(
                    FollowerRuntime(
                        account_id=str(row["id"]), client=self._make_client(), identity=None
                    )
                )

        # 3. clock drift check (nonce window +-10s)
        server_ms = await self.master_client.server_time()
        drift = abs(server_ms - now_ms())
        if drift > CLOCK_DRIFT_MAX_MS:
            raise RuntimeError(f"clock drift {drift}ms exceeds {CLOCK_DRIFT_MAX_MS}ms")
        log.info("clock drift ok (%dms)", drift)

        # 4. resolve UNKNOWN commands from previous run
        resolved = await resolve_boot_unknowns(self._client_for, self.commands)
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
            on_event=lambda event: self._consume(event.raw),
            on_reconnect=self._on_stream_reconnect,
        )
        stream_task = asyncio.create_task(self.stream.run(self.stop_event))
        self._stream_task = stream_task  # type: ignore[attr-defined]
        self._sweep_task = asyncio.create_task(self._periodic_sweep())

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
        for task in (self._sweep_task, getattr(self, "_stream_task", None)):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        with contextlib.suppress(Exception):
            await self.settings.set_flag("ready", "false")
        await self.http.aclose()
        await self.db.close()
        log.info("stopped")

    # --- runtime wiring --------------------------------------------------------------

    async def _consume(self, raw: dict) -> None:
        await self.projector.apply_raw(raw)

    async def _on_delta(self, delta) -> None:
        await self.coordinator.on_master_delta(delta)

    async def _on_stream_reconnect(self) -> None:
        log.info("master stream reconnected - resnapshotting")
        await self.projector.rebuild_full(self.master_client, "main")
        await self.reconciler.reconcile(self.projector.state, reason="reconnect")

    async def _on_confirmed(self, account_id: str, action) -> None:
        text = f"[{account_id}] {action.kind.value} {action.symbol} ok"
        await notify(text)

    async def _periodic_sweep(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.cfg.reconcile_interval_s
                )
                return
            except TimeoutError:
                pass
            try:
                await self.reconciler.reconcile(self.projector.state, reason="sweep")
            except Exception as exc:  # noqa: BLE001
                log.error("periodic sweep failed: %s", exc)


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def AccountRepo_all(db: Database):
    rows = await db.fetchall("SELECT * FROM accounts ORDER BY id")
    return rows
