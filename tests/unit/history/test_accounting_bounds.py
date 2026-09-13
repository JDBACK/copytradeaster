"""P4 money-read bounds: the same proven money without the whole-history scan.

The close card reads one episode's money from a table that keeps growing (the
income backfill added thousands of COMMISSION rows to a single symbol). The
bound that makes that read cheap must not change a single value, and the
exchange's own timestamps are not aligned enough to derive that bound from the
episode window alone: a fill's COMMISSION flow can land before the episode's
``opened_at``. These regressions pin the value equality and the exact edge that
an episode-window-only bound would have broken.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal as D

import pytest

from copytradeaster.aster.models import IncomeDTO, UserTradeDTO
from copytradeaster.domain.models import PosSide
from copytradeaster.history.accounting import load_episode_money
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import AccountRepo

DAY = 86_400_000
T0 = 1_700_000_000_000
SCOPE = HistoryScope("f1", "testnet")
OPEN_LAG_MS = 650  # the commission flow really does precede the fill on the live account


def _fill(tid: int, side: str, qty: str, price: str, pnl: str, t: int) -> UserTradeDTO:
    return UserTradeDTO(
        id=tid,
        orderId=tid,
        symbol="BTCUSDT",
        side=side,
        positionSide="BOTH",
        qty=qty,
        price=price,
        quoteQty=str(D(qty) * D(price)),
        realizedPnl=pnl,
        commission="0.1",
        commissionAsset="USDT",
        time=t,
    )


def _flow(tran: int, trade: str, amount: str, t: int, kind: str = "COMMISSION") -> IncomeDTO:
    return IncomeDTO(
        symbol="BTCUSDT",
        incomeType=kind,
        income=amount,
        asset="USDT",
        time=t,
        tranId=str(tran),
        tradeId=trade,
    )


async def _publish(db: Database, *, flow_lag_ms: int) -> tuple[HistoryRepository, str]:
    """One closed episode (1 → 110, gross +10) whose first flow precedes its open."""
    rows = [
        _fill(1, "BUY", "1", "100", "0", T0),
        _fill(2, "SELL", "1", "110", "10", T0 + 3_600_000),
    ]
    (projected,) = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"ref-{f.trade_id}-{d}",
    )
    await AccountRepo(db).upsert("f1", "follower", "f1", "0xabc")
    episodes = EpisodeRepository(db)
    ref = await episodes.reserve_ref(SCOPE, "BTCUSDT", PosSide.BOTH, projected.public_ref)
    await episodes.save(SCOPE, dataclasses.replace(projected, public_ref=ref), expected_revision=0)
    await episodes.rebuild_allocations(SCOPE, "BTCUSDT")
    history = HistoryRepository(db)
    window = Window(T0 - DAY, T0 + 2 * DAY)
    await history.commit_fills(
        SCOPE, "BTCUSDT", window, WindowRead(tuple(rows), (window,), (), (), 2), checked_at=T0
    )
    flows = (
        _flow(11, "1", "-0.1", T0 - flow_lag_ms),
        _flow(12, "2", "-0.1", T0 + 3_600_000 - flow_lag_ms),
        _flow(13, "2", "10", T0 + 3_600_000 - flow_lag_ms, kind="REALIZED_PNL"),
    )
    await history.commit_income(
        SCOPE, window, WindowRead(flows, (window,), (), (), 3), checked_at=T0
    )
    return history, ref


class SpanRecordingRepository(HistoryRepository):
    """Repository that records the bounds the accountant asked for."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.cashflow_bounds: list[tuple[int | None, int | None]] = []
        self.window_bounds: list[tuple[int | None, int | None]] = []

    async def cashflows_for_symbols(self, scope, symbols, *, start_ms=None, end_ms=None):
        self.cashflow_bounds.append((start_ms, end_ms))
        return await super().cashflows_for_symbols(scope, symbols, start_ms=start_ms, end_ms=end_ms)

    async def windows(self, scope, stream, symbol, *, start_ms=None, end_ms=None):
        self.window_bounds.append((start_ms, end_ms))
        return await super().windows(scope, stream, symbol, start_ms=start_ms, end_ms=end_ms)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bounds.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


async def test_commission_before_the_episode_open_still_counts(db: Database) -> None:
    """The live fact: an episode-window-only bound drops its own opening fee."""
    history, ref = await _publish(db, flow_lag_ms=OPEN_LAG_MS)
    batch = await load_episode_money(
        SCOPE,
        (ref,),
        history_repo=history,
        episodes_repo=EpisodeRepository(db),
    )
    money = batch.money[ref]
    assert money.issues == ()
    assert money.commission == D("-0.2")
    assert money.net == D("9.8")


async def test_the_read_asks_for_bounded_scans(db: Database) -> None:
    """A bounded read is the point of P4; unbounded stays only for open starts."""
    _, ref = await _publish(db, flow_lag_ms=OPEN_LAG_MS)
    history = SpanRecordingRepository(db)
    await load_episode_money(
        SCOPE, (ref,), history_repo=history, episodes_repo=EpisodeRepository(db)
    )
    ((cashflows_start, cashflows_end),) = history.cashflow_bounds
    # The bound covers the matched flows (which precede their fills), not just
    # the episode window; it is never widened past what the accountant matches.
    assert cashflows_start == T0 - OPEN_LAG_MS
    assert T0 + 3_600_000 - OPEN_LAG_MS <= cashflows_end <= T0 + 3_600_000
    ((windows_start, windows_end),) = history.window_bounds
    assert windows_start == T0 - OPEN_LAG_MS
    assert windows_end == T0 + 3_600_000


async def test_unrelated_history_outside_the_span_never_reaches_the_accountant(
    db: Database,
) -> None:
    """Old flows and a distant window cannot change a closed episode's money."""
    history, ref = await _publish(db, flow_lag_ms=OPEN_LAG_MS)
    episodes = EpisodeRepository(db)
    baseline = await load_episode_money(SCOPE, (ref,), history_repo=history, episodes_repo=episodes)
    old_window = Window(T0 - 400 * DAY, T0 - 399 * DAY)
    old_fills = (
        _fill(91, "BUY", "1", "50", "0", T0 - 400 * DAY + 1),
        _fill(92, "SELL", "1", "60", "10", T0 - 400 * DAY + 2),
    )
    await history.commit_fills(
        SCOPE,
        "BTCUSDT",
        old_window,
        WindowRead(old_fills, (old_window,), (), (), 2),
        checked_at=T0 - 400 * DAY,
    )
    await history.commit_income(
        SCOPE,
        old_window,
        WindowRead(
            (
                _flow(191, "91", "-0.5", T0 - 400 * DAY + 1),
                _flow(192, "92", "-0.5", T0 - 400 * DAY + 2),
            ),
            (old_window,),
            (),
            (),
            2,
        ),
        checked_at=T0 - 400 * DAY,
    )
    recording = SpanRecordingRepository(db)
    after = await load_episode_money(SCOPE, (ref,), history_repo=recording, episodes_repo=episodes)
    assert after.money == baseline.money
    assert after.proof == baseline.proof
    start, _ = recording.cashflow_bounds[0]
    assert start is not None and start > T0 - 400 * DAY + 2
