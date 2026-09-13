"""The close-card money reader: proven net or an honest gap, never a guess.

The reader is what lets a Telegram close card print one result instead of a
gross figure the user would read as final, so it must return the accountant's
net only when the costs are fully proven, and nothing at all otherwise.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal as D

import pytest

from copytradeaster.aster.models import IncomeDTO, UserTradeDTO
from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.notifications.money import SqliteEpisodeMoneyReader
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import AccountRepo

DAY = 86_400_000
T0 = 1_700_000_000_000
SCOPE = HistoryScope("f1", "testnet")


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


def _flow(tran: int, trade: str, amount: str, t: int) -> IncomeDTO:
    return IncomeDTO(
        symbol="BTCUSDT",
        incomeType="COMMISSION",
        income=amount,
        asset="USDT",
        time=t,
        tranId=str(tran),
        tradeId=trade,
    )


async def _store(db: Database, *, with_income: bool) -> tuple[EpisodeRepository, str]:
    """Publish one closed episode (1 → 110, gross +10) plus its fills/income."""
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
    stored = await episodes.save(
        SCOPE, dataclasses.replace(projected, public_ref=ref), expected_revision=0
    )
    await episodes.rebuild_allocations(SCOPE, "BTCUSDT")
    history = HistoryRepository(db)
    window = Window(T0 - DAY, T0 + 2 * DAY)
    await history.commit_fills(
        SCOPE, "BTCUSDT", window, WindowRead(tuple(rows), (window,), (), (), 2), checked_at=T0
    )
    if with_income:
        flows = (_flow(11, "1", "-0.1", T0), _flow(12, "2", "-0.1", T0 + 3_600_000))
        await history.commit_income(
            SCOPE,
            window,
            WindowRead(flows, (window,), (), (), 1),
            checked_at=T0,
        )
    return episodes, stored.episode.public_ref


async def _reader(db: Database) -> SqliteEpisodeMoneyReader:
    return SqliteEpisodeMoneyReader(EpisodeRepository(db), HistoryRepository(db))


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "money.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


async def test_proven_costs_produce_the_net_the_accountant_returns(db: Database) -> None:
    _, ref = await _store(db, with_income=True)
    money = (await (await _reader(db)).money_for(SCOPE, ref)).get(ref)
    assert money is not None
    assert money.result_state == "net_complete"
    assert D(money.gross) == D("10")
    assert D(money.net) == D("9.8")  # 10 gross − 0.2 commissions
    assert money.commission.known_amount == "-0.2"


async def test_missing_income_keeps_the_gap_instead_of_inventing_a_net(db: Database) -> None:
    _, ref = await _store(db, with_income=False)
    money = (await (await _reader(db)).money_for(SCOPE, ref)).get(ref)
    assert money is not None
    assert money.result_state == "gross_only"
    assert money.net is None
    assert "income_incomplete" in money.issues


async def test_an_unknown_ref_reads_as_nothing(db: Database) -> None:
    assert await (await _reader(db)).money_for(SCOPE, "nope") == {}
