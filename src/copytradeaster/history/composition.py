"""Production history wiring, separate from trading and transport handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.reader import HistoryReader
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.history.worker import HistoryJob, HistoryWorker
from copytradeaster.storage.history import HistoryScope

if TYPE_CHECKING:
    from copytradeaster.app import App


def build_history_worker(app: App) -> HistoryWorker:
    async def discover() -> list[HistoryJob]:
        if app.cfg.dry_run:
            return []
        jobs = []
        environment = "testnet" if "testnet" in app.cfg.rest_base_url else "mainnet"
        for follower in app.follower_registry.snapshot():
            if follower.execution_mode != "REAL":
                continue  # Shadow currently has no canonical history endpoints.
            scope = HistoryScope(follower.account_id, environment)
            jobs.append(HistoryJob(scope, None))
            jobs.append(HistoryJob(scope, None, repair=True))
            rows = await app.db.fetchall(
                "SELECT symbol FROM follower_order_observations "
                "WHERE account_id=? AND environment=? "
                "UNION SELECT symbol FROM follower_cash_flows WHERE account_id=? AND environment=? "
                "UNION SELECT json_extract(payload,'$.symbol') AS symbol FROM execution_commands "
                "WHERE account_id=? AND json_valid(payload)",
                (scope.account_id, environment, scope.account_id, environment, scope.account_id),
            )
            symbols = set(app.cfg.symbol_whitelist)
            symbols.update(row["symbol"] for row in rows if row["symbol"])
            for symbol in sorted(symbols):
                jobs.append(HistoryJob(scope, symbol))
                jobs.append(HistoryJob(scope, symbol, capture=True))
        return jobs

    def synchronizer(scope: HistoryScope) -> HistorySynchronizer:
        return HistorySynchronizer(HistoryReader(app._client_for(scope.account_id)), app.history)

    capture = build_history_capture(app)

    async def capture_job(scope: HistoryScope, symbol: str) -> bool:
        return await capture.run_scheduled(
            scope,
            symbol,
            snapshots=app._client_for(scope.account_id),
            synchronizer=synchronizer(scope),
            now_ms=int(time.time() * 1000),
        )

    worker = HistoryWorker(discover, synchronizer, capture_job)
    capture.set_on_request(worker.note_snapshot_request)

    def mark_dirty(account_id: str, environment: str, symbol: str) -> None:
        worker.mark_dirty(HistoryScope(account_id, environment), symbol)

    app.history_mark_dirty = mark_dirty
    return worker


def build_history_capture(app: App) -> HistoryCapture:
    """Share one capture instance per process: its locks serialize a key."""
    return HistoryCapture(app.history, app.episodes, events=app.user_events, commands=app.commands)
