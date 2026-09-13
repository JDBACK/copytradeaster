"""Compare the bounded money read against the unbounded read on a real DB copy.

Run: PYTHONPATH=src .venv/bin/python scripts/verify_episode_money_bounds.py \
        [--source data/copytrade.sqlite3] [--account copyc1] [--limit 20]

The repository bounds added for P4 must not change a single value. This probe
runs ``load_episode_money`` twice per episode on the same copy - once with the
bounds honoured (production path) and once with the bounds stripped, which
reproduces the old full-scan read - and compares the resulting money, issues
and proof field by field. The source database is opened read-only through the
SQLite backup API, so a live WAL database is never locked or written.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copytradeaster.history.accounting import load_episode_money  # noqa: E402
from copytradeaster.storage.database import Database  # noqa: E402
from copytradeaster.storage.episodes import EpisodeRepository  # noqa: E402
from copytradeaster.storage.history import HistoryRepository, HistoryScope  # noqa: E402


class WholeHistoryRepository(HistoryRepository):
    """History repository with the P4 range bounds stripped (legacy read)."""

    async def cashflows_for_symbols(self, scope, symbols, *, start_ms=None, end_ms=None):
        return await super().cashflows_for_symbols(scope, symbols)

    async def windows(self, scope, stream, symbol, *, start_ms=None, end_ms=None):
        return await super().windows(scope, stream, symbol)


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


async def _refs(db: Database, account: str, limit: int) -> list[str]:
    rows = await db.fetchall(
        "SELECT public_ref FROM follower_episodes WHERE account_id=? AND environment='testnet' "
        "AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
        (account, limit),
    )
    return [str(row["public_ref"]) for row in rows]


async def _read(history, episodes, scope, ref: str):
    return await load_episode_money(scope, (ref,), history_repo=history, episodes_repo=episodes)


async def main() -> None:
    argv = sys.argv[1:]

    def option(name: str, default: str) -> str:
        return argv[argv.index(name) + 1] if name in argv else default

    source = Path(option("--source", "data/copytrade.sqlite3")).resolve()
    account = option("--account", "copyc1")
    limit = int(option("--limit", "20"))
    with tempfile.TemporaryDirectory(prefix="money-bounds-") as folder:
        copy_path = Path(folder) / "probe.sqlite3"
        _backup(source, copy_path)
        db = Database(copy_path)
        await db.connect()
        try:
            bounded_repo = HistoryRepository(db)
            legacy_repo = WholeHistoryRepository(db)
            episodes = EpisodeRepository(db)
            scope = HistoryScope(account, "testnet")
            refs = await _refs(db, account, limit)
            if not refs:
                print(f"no closed episodes for account={account!r}")
                return
            mismatches: list[str] = []
            bounded_s = legacy_s = 0.0
            net_bounded = 0
            for ref in refs:
                mark = time.perf_counter()
                new = await _read(bounded_repo, episodes, scope, ref)
                bounded_s += time.perf_counter() - mark
                mark = time.perf_counter()
                old = await _read(legacy_repo, episodes, scope, ref)
                legacy_s += time.perf_counter() - mark
                fresh = new.money.get(ref)
                if fresh is not None and fresh.net is not None:
                    net_bounded += 1
                if new.money != old.money or new.proof != old.proof:
                    mismatches.append(ref)
                    print(f"MISMATCH {ref}")
                    print(f"  bounded money={new.money.get(ref)}")
                    print(f"  legacy  money={old.money.get(ref)}")
                    print(f"  bounded proof={new.proof}")
                    print(f"  legacy  proof={old.proof}")
            print(f"source={source}")
            print(f"account={account} episodes={len(refs)} mismatches={len(mismatches)}")
            print(
                f"bounded total={bounded_s:.3f}s mean={bounded_s / len(refs) * 1000:.1f}ms | "
                f"legacy total={legacy_s:.3f}s mean={legacy_s / len(refs) * 1000:.1f}ms"
            )
            print(f"net_complete(bounded)={net_bounded}")
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
