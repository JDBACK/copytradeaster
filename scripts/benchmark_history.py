"""Offline history capacity probe on a temporary synthetic SQLite DB; no credentials/network.

Run: PYTHONPATH=src .venv/bin/python scripts/benchmark_history.py [--accounts N]
JSON output includes table/index allocation, WAL, backup, commit latency,
full-schema publication lag per account (fills → episodes/events/allocations/
boundaries), request counts, and a 30/90/365-day storage forecast with a
free-space threshold recommendation. Workload assumptions are printed with
the numbers; re-run when the schema or the load shape changes.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copytradeaster.aster.models import IncomeDTO, OrderDTO, PositionDTO, UserTradeDTO  # noqa: E402
from copytradeaster.history.capture import HistoryCapture  # noqa: E402
from copytradeaster.history.reader import Window, WindowRead  # noqa: E402
from copytradeaster.history.sync import HistorySynchronizer  # noqa: E402
from copytradeaster.storage.database import Database  # noqa: E402
from copytradeaster.storage.episodes import EpisodeRepository  # noqa: E402
from copytradeaster.storage.events import EventRepository  # noqa: E402
from copytradeaster.storage.history import HistoryRepository, HistoryScope  # noqa: E402

FILLS_PER_ACCOUNT = 1000
ASSUMED_FILLS_PER_DAY_ACCOUNT = 20
FORECAST_ACCOUNTS = 100


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="history-capacity-") as folder:
        path = Path(folder) / "history.sqlite3"
        db = Database(path)
        await db.connect()
        await db.migrate()
        repo = HistoryRepository(db)
        latencies = []
        try:
            accounts = 30
            if "--accounts" in sys.argv:
                accounts = int(sys.argv[sys.argv.index("--accounts") + 1])
            for account in range(accounts):
                label = f"synthetic-{account:03d}"
                await db.execute_write(
                    "INSERT INTO accounts(id,role,label,user_address) VALUES (?,'follower',?,?)",
                    (label, label, label),
                )
                scope = HistoryScope(label, "testnet")
                fills = tuple(
                    UserTradeDTO(
                        id=9007199254740993 + n,
                        orderId=9007199254700000 + n // 2,
                        symbol="BTCUSDT",
                        side="BUY" if n % 2 == 0 else "SELL",
                        positionSide="BOTH",
                        qty="0.002",
                        price="65000.12345678",
                        quoteQty="130.00024691356",
                        # Entries carry no realized PnL; exits close (alternating flat).
                        realizedPnl="0" if n % 2 == 0 else "0.12345678",
                        commission="0.06500012345678",
                        commissionAsset="USDT",
                        time=1788000000000 + n,
                    )
                    for n in range(1000)
                )
                window = Window(fills[0].time, fills[-1].time)
                start = time.perf_counter()
                await repo.commit_fills(
                    scope,
                    "BTCUSDT",
                    window,
                    WindowRead(fills, (window,), (), (), 1),
                    checked_at=window.end,
                )
                latencies.append((time.perf_counter() - start) * 1000)
                flows = tuple(
                    IncomeDTO(
                        symbol="BTCUSDT",
                        incomeType="COMMISSION",
                        income="-0.06500012345678",
                        asset="USDT",
                        time=fill.time,
                        tranId=str(fill.trade_id),
                        tradeId=str(fill.trade_id),
                    )
                    for fill in fills
                )
                await repo.commit_income(
                    scope, window, WindowRead(flows, (window,), (), (), 1), checked_at=window.end
                )
                for n in range(500):
                    order = OrderDTO(
                        orderId=9007199254700000 + n,
                        clientOrderId=f"ct-{account}-{n}",
                        symbol="BTCUSDT",
                        side="BUY",
                        type="MARKET",
                        status="FILLED",
                        origQty="0.004",
                        executedQty="0.004",
                        avgPrice="65000.12345678",
                        price="0",
                        stopPrice="0",
                        reduceOnly=False,
                        closePosition=False,
                        positionSide="BOTH",
                        workingType="MARK_PRICE",
                        timeInForce="GTC",
                        updateTime=window.end,
                    )
                    await repo.observe_order(
                        scope,
                        order,
                        kind="MARKET_OPEN",
                        command_key=f"cmd-{account}-{n}",
                        observed_at=window.end,
                    )
            publish_ms: list[float] = []
            snapshot_reads = [0]
            episodes_total = 0
            events_total = 0

            class _FlatSnapshots:
                async def position_risk(self, account_id, symbol):
                    return [
                        PositionDTO.model_validate(
                            {
                                "symbol": symbol,
                                "positionAmt": "0",
                                "entryPrice": "0",
                                "markPrice": "65000",
                                "leverage": 10,
                                "liquidationPrice": "0",
                                "marginType": "cross",
                                "positionSide": "BOTH",
                                "unRealizedProfit": "0",
                                "updateTime": 1788000000000,
                            }
                        )
                    ]

            class _CoveredReader:
                """Coverage is already durable; the import is a cheap no-op."""

                async def fills(self, account_id, symbol, window, request_budget=8):
                    return WindowRead((), (window,), (), (), 1)

                async def income(self, account_id, window, request_budget=8):
                    return WindowRead((), (window,), (), (), 1)

            class _Tick:
                """One clock value per boundary read; then pins the frontier."""

                def __init__(self, values: list[int], pinned: int) -> None:
                    self._values = values
                    self._pinned = pinned

                def __call__(self) -> int:
                    if self._values:
                        return self._values.pop(0)
                    return self._pinned

            def _tick(values: list[int], pinned: int) -> _Tick:
                return _Tick(values, pinned)

            episodes = EpisodeRepository(db)
            user_events = EventRepository(db)
            for account in range(accounts):
                label = f"synthetic-{account:03d}"
                scope = HistoryScope(label, "testnet")
                capture = HistoryCapture(
                    repo,
                    episodes,
                    events=user_events,
                    clock_margin_ms=1,
                    on_request=lambda: snapshot_reads.__setitem__(0, snapshot_reads[0] + 1),
                )
                synchronizer = HistorySynchronizer(_CoveredReader(), repo)  # type: ignore[arg-type]
                first_fill = 1788000000000
                last_fill = first_fill + FILLS_PER_ACCOUNT - 1
                ticks = [last_fill, last_fill, last_fill + 1, last_fill + 1]
                start = time.perf_counter()
                stored, _ = await capture.capture_and_publish(
                    scope,
                    "BTCUSDT",
                    Window(first_fill, last_fill),
                    snapshots=_FlatSnapshots(),
                    synchronizer=synchronizer,
                    clock_ms=_tick(ticks, last_fill),
                    notify_eligible=True,
                )
                publish_ms.append((time.perf_counter() - start) * 1000)
                episodes_total += len(stored)
            events_total = (await db.fetchone("SELECT COUNT(*) AS n FROM user_activity_events"))[
                "n"
            ]
            allocations_total = (
                await db.fetchone("SELECT COUNT(*) AS n FROM follower_episode_allocations")
            )["n"]
            allocation = await db.fetchall(
                "SELECT name,sum(pgsize) bytes FROM dbstat GROUP BY name"
            )
            wal = Path(str(path) + "-wal")
            wal_bytes = wal.stat().st_size if wal.exists() else 0
            cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await cursor.close()
            with (
                sqlite3.connect(path) as source,
                sqlite3.connect(Path(folder) / "backup.sqlite3") as dest,
            ):
                source.backup(dest)
            db_bytes = path.stat().st_size
            # Forecast: measured bytes per synthetic account (1000 fills +
            # cashflows + 500 observations + full publication) scaled to the
            # assumed daily fill rate. Events/fill and protection churn come
            # from this run, not from guesses.
            fills_total = accounts * FILLS_PER_ACCOUNT
            bytes_per_fill = db_bytes / fills_total
            daily_bytes = FORECAST_ACCOUNTS * ASSUMED_FILLS_PER_DAY_ACCOUNT * bytes_per_fill
            forecast = {f"{days}d_gb": round(daily_bytes * days / 1e9, 3) for days in (30, 90, 365)}
            print(
                json.dumps(
                    {
                        "accounts": accounts,
                        "fills": fills_total,
                        "cashflows": fills_total,
                        "observations": accounts * 500,
                        "episodes_published": episodes_total,
                        "events_stored": events_total,
                        "allocations_indexed": allocations_total,
                        "db_bytes": db_bytes,
                        "wal_before_checkpoint_bytes": wal_bytes,
                        "backup_bytes": (Path(folder) / "backup.sqlite3").stat().st_size,
                        "fill_batch_1000_ms_p50": round(statistics.median(latencies), 2),
                        "fill_batch_1000_ms_max": round(max(latencies), 2),
                        "publish_per_account_ms_p50": round(statistics.median(publish_ms), 2),
                        "publish_per_account_ms_p95": round(
                            statistics.quantiles(publish_ms, n=20)[-1]
                            if len(publish_ms) >= 20
                            else max(publish_ms),
                            2,
                        ),
                        "snapshot_reads_total": snapshot_reads[0],
                        "allocation": {row["name"]: row["bytes"] for row in allocation},
                        "assumptions": {
                            "fills_per_day_per_account": ASSUMED_FILLS_PER_DAY_ACCOUNT,
                            "forecast_accounts": FORECAST_ACCOUNTS,
                            "events_per_fill_measured": round(events_total / fills_total, 3),
                            "bytes_per_fill_measured": round(bytes_per_fill, 1),
                        },
                        "forecast_gb": forecast,
                        "free_space_threshold_gb": round(max(forecast["365d_gb"] * 0.5, 1.0), 2),
                    },
                    indent=2,
                )
            )
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
