"""Measure the durable money read (``load_episode_money``) on a real database copy.

Run: PYTHONPATH=src .venv/bin/python scripts/benchmark_episode_money.py \
        [--source data/copytrade.sqlite3] [--account copyc1] [--limit 20]

The source database is opened read-only through ``sqlite3.Connection.backup``
into a temporary file, so a live WAL database is never touched or locked. The
probe prints wall time per episode plus the rows actually materialized per SQL
statement, which is what makes an unbounded history scan visible before a mass
backfill turns it into a production latency problem.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copytradeaster.history.accounting import load_episode_money  # noqa: E402
from copytradeaster.storage.database import Database  # noqa: E402
from copytradeaster.storage.episodes import EpisodeRepository  # noqa: E402
from copytradeaster.storage.history import HistoryRepository, HistoryScope  # noqa: E402


class CountingDatabase(Database):
    """Database wrapper that counts returned rows per statement shape."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.rows_by_sql: Counter[str] = Counter()
        self.calls_by_sql: Counter[str] = Counter()

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        rows = await super().fetchall(sql, params)
        key = " ".join(sql.split())[:96]
        self.rows_by_sql[key] += len(rows)
        self.calls_by_sql[key] += 1
        return rows


def _backup(source: Path, target: Path) -> None:
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()


async def _recent_refs(db: Database, account: str, limit: int) -> list[str]:
    rows = await db.fetchall(
        "SELECT public_ref FROM follower_episodes WHERE account_id=? AND environment='testnet' "
        "AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
        (account, limit),
    )
    return [str(row["public_ref"]) for row in rows]


async def main() -> None:
    argv = sys.argv[1:]

    def option(name: str, default: str) -> str:
        return argv[argv.index(name) + 1] if name in argv else default

    source = Path(option("--source", "data/copytrade.sqlite3")).resolve()
    account = option("--account", "copyc1")
    limit = int(option("--limit", "20"))
    with tempfile.TemporaryDirectory(prefix="money-probe-") as folder:
        copy_path = Path(folder) / "probe.sqlite3"
        started = time.perf_counter()
        _backup(source, copy_path)
        backup_s = time.perf_counter() - started
        db = CountingDatabase(copy_path)
        await db.connect()
        try:
            history = HistoryRepository(db)
            episodes = EpisodeRepository(db)
            scope = HistoryScope(account, "testnet")
            refs = await _recent_refs(db, account, limit)
            if not refs:
                print(f"no closed episodes for account={account!r} in {source}")
                return
            db.rows_by_sql.clear()
            db.calls_by_sql.clear()
            latencies: list[float] = []
            net_complete = 0
            for ref in refs:
                mark = time.perf_counter()
                batch = await load_episode_money(
                    scope,
                    (ref,),
                    history_repo=history,
                    episodes_repo=episodes,
                )
                latencies.append(time.perf_counter() - mark)
                money = batch.money.get(ref)
                if money is not None and money.net is not None:
                    net_complete += 1
            total = sum(latencies)
            print(f"source={source}")
            print(f"account={account} episodes={len(refs)} net_complete={net_complete}")
            print(f"backup={backup_s:.2f}s db_bytes={copy_path.stat().st_size}")
            print(
                f"money_read total={total:.3f}s mean={total / len(latencies) * 1000:.1f}ms "
                f"max={max(latencies) * 1000:.1f}ms"
            )
            print(
                f"sql_calls={sum(db.calls_by_sql.values())} sql_rows={sum(db.rows_by_sql.values())}"
            )
            for sql, rows in db.rows_by_sql.most_common():
                print(f"  rows={rows:<8} calls={db.calls_by_sql[sql]:<6} {sql}")
            # The Mini App reads a whole closed-trades page as ONE batch, so the
            # per-ref loop above is not the production shape: measure that call
            # too, with its own row accounting.
            db.rows_by_sql.clear()
            db.calls_by_sql.clear()
            mark = time.perf_counter()
            page = await load_episode_money(
                scope, tuple(refs), history_repo=history, episodes_repo=episodes
            )
            page_s = time.perf_counter() - mark
            complete = sum(1 for money in page.money.values() if money.net is not None)
            print(
                f"page_batch refs={len(refs)} total={page_s:.3f}s net_complete={complete} "
                f"sql_calls={sum(db.calls_by_sql.values())} "
                f"sql_rows={sum(db.rows_by_sql.values())}"
            )
            for sql, rows in db.rows_by_sql.most_common(6):
                print(f"  page rows={rows:<8} calls={db.calls_by_sql[sql]:<6} {sql}")
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
