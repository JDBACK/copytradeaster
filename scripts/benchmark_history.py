"""Offline history capacity probe on a temporary synthetic SQLite DB; no credentials/network.

Run: PYTHONPATH=src .venv/bin/python scripts/benchmark_history.py
JSON output includes table/index allocation, WAL, backup and commit latency.
This measures current schema only; repeat when episodes/events are added.
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

from copytradeaster.aster.models import IncomeDTO, OrderDTO, UserTradeDTO  # noqa: E402
from copytradeaster.history.reader import Window, WindowRead  # noqa: E402
from copytradeaster.storage.database import Database  # noqa: E402
from copytradeaster.storage.history import HistoryRepository, HistoryScope  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="history-capacity-") as folder:
        path = Path(folder) / "history.sqlite3"
        db = Database(path)
        await db.connect()
        await db.migrate()
        repo = HistoryRepository(db)
        latencies = []
        try:
            for account in range(100):
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
                        realizedPnl="0.12345678",
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
            print(
                json.dumps(
                    {
                        "accounts": 100,
                        "fills": 100000,
                        "cashflows": 100000,
                        "observations": 50000,
                        "db_bytes": path.stat().st_size,
                        "wal_before_checkpoint_bytes": wal_bytes,
                        "backup_bytes": (Path(folder) / "backup.sqlite3").stat().st_size,
                        "fill_batch_1000_ms_p50": round(statistics.median(latencies), 2),
                        "fill_batch_1000_ms_max": round(max(latencies), 2),
                        "allocation": {row["name"]: row["bytes"] for row in allocation},
                    },
                    indent=2,
                )
            )
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
