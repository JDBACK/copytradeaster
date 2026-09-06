"""C5 money wiring: service list/detail return accountant-matching proof views."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from copytradeaster.application.models import TradeUnavailableError
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.models import IncomeDTO, UserTradeDTO
from copytradeaster.domain.models import PosSide, RiskPolicy
from copytradeaster.history.accounting import AccountingProof, account_episodes
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    SettingsRepo,
    TelegramUserRepo,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
USER_ID = 7
OTHER_ID = 8
USER_ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_ADDRESS = "0x2222222222222222222222222222222222222222"
BASE_MS = int(NOW.timestamp() * 1000)
DAY = 86_400_000


class FakeHistoryRuntime:
    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.cfg = SimpleNamespace(reconcile_interval_s=15, fee_buffer_pct="0.001")
        self.snapshot: dict | None = {"positions": {}, "available_usdt": "1240"}
        self.snapshot_at: datetime | None = NOW
        self.release = asyncio.Event()
        self.release.set()
        self.assets: dict[str, str | None] = {"BTCUSDT": "BTC"}

    async def cached_snapshot(self, account_id: str):
        del account_id
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def refresh_snapshot(self, account_id: str):
        del account_id
        await self.release.wait()
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def symbol_assets(self, account_id: str, symbols: list[str]):
        del account_id
        return {symbol: self.assets.get(symbol) for symbol in symbols}


def _fill(
    tid: int,
    side: str,
    qty: str,
    price: str = "100",
    pnl: str = "0",
    leg: str = "BOTH",
    t: int = 0,
) -> UserTradeDTO:
    return UserTradeDTO(
        id=tid,
        orderId=tid,
        symbol="BTCUSDT",
        side=side,
        positionSide=leg,
        qty=qty,
        price=price,
        quoteQty=str(D(qty) * D(price)),
        realizedPnl=pnl,
        commission="0.1",
        commissionAsset="USDT",
        time=t,
    )


def _flow(tran: int, trade: str, amount: str = "-0.1", t: int = 0) -> IncomeDTO:
    return IncomeDTO(
        symbol="BTCUSDT",
        incomeType="COMMISSION",
        income=amount,
        asset="USDT",
        time=t,
        tranId=str(tran),
        tradeId=trade,
    )


def _funding(tran: int, amount: str, t: int) -> IncomeDTO:
    return IncomeDTO(
        symbol="BTCUSDT",
        incomeType="FUNDING_FEE",
        income=amount,
        asset="USDT",
        time=t,
        tranId=str(tran),
        tradeId="",
    )


@pytest.fixture
async def money_env(tmp_path: Path):
    db = Database(tmp_path / "trade_money_c5.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("h1", "follower", "h1", USER_ADDRESS)
    await AccountRepo(db).upsert("h2", "follower", "h2", OTHER_ADDRESS)
    await TelegramUserRepo(db).upsert(USER_ID, 700, account_id="h1")
    await TelegramUserRepo(db).upsert(OTHER_ID, 800, account_id="h2")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=D("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        "h1",
    )
    settings = SettingsRepo(db)
    await settings.set_flag("ready", "true")
    await settings.set_flag("allow_new_entries", "true")
    runtime = FakeHistoryRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        testnet_check=True,
    )
    yield db, runtime, service
    await db.close()


async def _publish(
    db: Database,
    episodes,
    scope: HistoryScope | None = None,
) -> list:
    scope = scope or HistoryScope("h1", "testnet")
    repo = EpisodeRepository(db)
    stored = []
    for episode in episodes:
        ref = await repo.reserve_ref(
            scope, episode.symbol, episode.position_side, episode.public_ref
        )
        stored.append(
            await repo.save(
                scope, dataclasses.replace(episode, public_ref=ref), expected_revision=0
            )
        )
    await repo.rebuild_allocations(scope, "BTCUSDT")
    return stored


async def _commit_fills(
    db: Database, rows: list[UserTradeDTO], window: Window, scope: HistoryScope
) -> None:
    history = HistoryRepository(db)
    await history.commit_fills(
        scope,
        "BTCUSDT",
        window,
        WindowRead(tuple(rows), (window,), (), (), len(rows)),
        checked_at=window.end,
    )


async def _commit_income(
    db: Database, rows: list[IncomeDTO], window: Window, scope: HistoryScope
) -> None:
    history = HistoryRepository(db)
    await history.commit_income(
        scope,
        window,
        WindowRead(tuple(rows), (window,), (), (), len(rows)),
        checked_at=window.end,
    )


def _window(t0: int, t1: int) -> Window:
    start = max(t0 - DAY, 0)
    end = t1 + DAY
    if end - start > 7 * DAY:
        start = end - 7 * DAY
    return Window(start, end)


async def test_full_proof_net_set_and_matches_accountant(money_env) -> None:
    db, _, service = money_env
    scope = HistoryScope("h1", "testnet")
    t0 = BASE_MS - 2 * DAY
    rows = [
        _fill(1, "BUY", "1", t=t0),
        _fill(2, "SELL", "1", "110", "10", t=t0 + 3_600_000),
    ]
    (projected,) = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"full-{f.trade_id}-{d}",
    )
    (stored,) = await _publish(db, [projected], scope)
    ref = stored.episode.public_ref
    window = _window(t0, t0 + 3_600_000)
    timed_flows = [_flow(1, "1", t=t0), _flow(2, "2", t=t0 + 3_600_000)]
    await _commit_fills(db, rows, window, scope)
    await _commit_income(db, timed_flows, window, scope)

    expected = account_episodes(
        [stored.episode],
        rows,
        timed_flows,
        proof=AccountingProof(income_complete=True, builder_policy="included_in_commission"),
    )[stored.episode.public_ref]
    assert expected.net is not None and expected.issues == ()

    (item,) = (await service.trades(USER_ID, period="all")).items
    assert item.trade_ref == ref
    assert item.realized_pnl_gross_usdt == "10"
    assert item.net_pnl_usdt is not None and D(item.net_pnl_usdt) == expected.net
    assert item.result_state == "net_complete"
    assert item.money is not None and item.money.net == item.net_pnl_usdt
    assert item.money.gross == "10" and item.money.issues == ()
    assert item.money.commission.known_amount == "-0.2"
    assert item.money.commission.state.value == "complete"
    assert item.money.funding.state.value == "complete"
    assert item.money.builder.state.value == "complete"
    assert item.quantity_asset == "BTC"

    detail = await service.trade(USER_ID, ref)
    assert detail.trade.net_pnl_usdt == item.net_pnl_usdt
    assert detail.result_state == "net_complete"
    assert detail.money is not None and detail.money.net == item.net_pnl_usdt
    assert detail.revision == stored.revision and detail.money.revision == stored.revision


async def test_partial_proof_null_without_invented_zero(money_env) -> None:
    db, _, service = money_env
    scope = HistoryScope("h1", "testnet")
    t0 = BASE_MS - 2 * DAY
    rows = [
        _fill(1, "BUY", "1", t=t0),
        _fill(2, "SELL", "1", "110", "10", t=t0 + 3_600_000),
    ]
    (projected,) = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"part-{f.trade_id}-{d}",
    )
    (stored,) = await _publish(db, [projected], scope)
    window = _window(t0, t0 + 3_600_000)
    await _commit_fills(db, rows, window, scope)
    # No income coverage committed: a non-empty local flow list must not
    # invent completeness, so the service keeps gross_only with a named gap.

    (item,) = (await service.trades(USER_ID, period="all")).items
    assert item.trade_ref == stored.episode.public_ref
    assert item.realized_pnl_gross_usdt == "10"
    assert item.net_pnl_usdt is None
    assert item.result_state == "gross_only"
    assert item.money is not None and item.money.net is None
    assert "income_incomplete" in item.money.issues
    assert item.money.gross == "10"

    detail = await service.trade(USER_ID, stored.episode.public_ref)
    assert detail.trade.net_pnl_usdt is None and detail.result_state == "gross_only"
    assert detail.money is not None and detail.money.net is None


async def test_shared_flip_across_pages_counts_commission_once(money_env) -> None:
    db, _, service = money_env
    scope = HistoryScope("h1", "testnet")
    t0 = BASE_MS - 2 * DAY
    rows = [
        _fill(1, "BUY", "1", t=t0),
        _fill(2, "SELL", "2", "110", "10", t=t0 + 3_600_000),
        _fill(3, "BUY", "1", "100", "5", t=t0 + 7_200_000),
    ]
    long, short = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"flip-{f.trade_id}-{d}",
    )
    stored = await _publish(db, [long, short], scope)
    refs = {s.episode.public_ref for s in stored}
    window = _window(t0, t0 + 7_200_000)
    flows = [
        _flow(1, "1", t=t0),
        _flow(2, "2", t=t0 + 3_600_000),
        _flow(3, "3", t=t0 + 7_200_000),
    ]
    await _commit_fills(db, rows, window, scope)
    await _commit_income(db, flows, window, scope)

    first = await service.trades(USER_ID, period="all", limit=1)
    assert first.next_cursor is not None and len(first.items) == 1
    # The sibling outside this page still splits the shared fill exactly:
    # no guessed full charge and no shared_fill_commission gap.
    assert first.items[0].money is not None
    assert "shared_fill_commission" not in first.items[0].money.issues
    assert first.items[0].net_pnl_usdt is not None

    second = await service.trades(USER_ID, period="all", limit=1, cursor=first.next_cursor)
    assert len(second.items) == 1
    got = {first.items[0].trade_ref, second.items[0].trade_ref}
    assert got == refs
    total = sum((D(i.money.commission.known_amount) for i in (*first.items, *second.items)), D(0))
    assert total == D("-0.3")
    assert {i.money.commission.known_amount for i in (*first.items, *second.items)} == {"-0.15"}


async def test_funding_without_unique_leg_stays_unattributed(money_env) -> None:
    db, _, service = money_env
    scope = HistoryScope("h1", "testnet")
    t0 = BASE_MS - 4 * DAY
    rows = [
        _fill(1, "BUY", "2", t=t0, leg="LONG"),
        _fill(2, "SELL", "2", t=t0 + DAY, leg="SHORT"),
        _fill(3, "SELL", "2", "140", "60", t=t0 + 2 * DAY, leg="LONG"),
        _fill(4, "BUY", "2", "100", "10", t=t0 + 3 * DAY, leg="SHORT"),
    ]
    long, short = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.LONG: PositionSeed(D(0)), PosSide.SHORT: PositionSeed(D(0))},
        reference=lambda f, d: f"hedge-{d}-{f.trade_id}",
    )
    assert long.closed_at is not None and short.closed_at is not None
    stored = await _publish(db, [long, short], scope)
    window = _window(t0, t0 + 3 * DAY)
    funding_at = t0 + DAY + 3_600_000
    assert long.opened_at <= funding_at <= long.closed_at
    assert short.opened_at <= funding_at <= short.closed_at
    flows = [
        _flow(1, "1", t=t0),
        _flow(2, "2", t=t0 + DAY),
        _flow(3, "3", t=t0 + 2 * DAY),
        _flow(4, "4", t=t0 + 3 * DAY),
        _funding(5, "-2", funding_at),
    ]
    await _commit_fills(db, rows, window, scope)
    await _commit_income(db, flows, window, scope)

    page = await service.trades(USER_ID, period="all")
    assert len(page.items) == 2
    for item in page.items:
        assert item.money is not None
        assert item.money.funding.known_amount == "0"
        assert "funding_unattributed" in item.money.issues
        assert item.net_pnl_usdt is None and item.result_state == "gross_only"

    for s in stored:
        detail = await service.trade(USER_ID, s.episode.public_ref)
        assert detail.money is not None
        assert "funding_unattributed" in detail.money.issues
        assert detail.trade.net_pnl_usdt is None


async def test_trade_hides_foreign_money_ref(money_env) -> None:
    db, _, service = money_env
    scope = HistoryScope("h1", "testnet")
    t0 = BASE_MS - 2 * DAY
    rows = [
        _fill(1, "BUY", "1", t=t0),
        _fill(2, "SELL", "1", "110", "10", t=t0 + 3_600_000),
    ]
    (projected,) = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"own-{f.trade_id}-{d}",
    )
    (stored,) = await _publish(db, [projected], scope)
    window = _window(t0, t0 + 3_600_000)
    await _commit_fills(db, rows, window, scope)
    await _commit_income(db, [_flow(1, "1", t=t0), _flow(2, "2", t=t0 + 3_600_000)], window, scope)
    assert (await service.trades(OTHER_ID, period="all")).items == ()
    with pytest.raises(TradeUnavailableError):
        await service.trade(OTHER_ID, stored.episode.public_ref)
