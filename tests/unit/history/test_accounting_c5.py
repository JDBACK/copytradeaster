"""C5 accountant regressions: (symbol, trade_id) keying and exact shared splits."""

from __future__ import annotations

from decimal import Decimal as D

from copytradeaster.aster.models import IncomeDTO, UserTradeDTO
from copytradeaster.domain.models import PosSide
from copytradeaster.history.accounting import AccountingProof, account_episodes
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes

PROOF = AccountingProof(income_complete=True, builder_policy="included_in_commission")


def _fill(
    tid: int,
    side: str,
    qty: str,
    price: str = "100",
    pnl: str = "0",
    leg: str = "BOTH",
    t: int | None = None,
    sym: str = "BTCUSDT",
) -> UserTradeDTO:
    return UserTradeDTO(
        id=tid,
        orderId=tid,
        symbol=sym,
        side=side,
        positionSide=leg,
        qty=qty,
        price=price,
        quoteQty=str(D(qty) * D(price)),
        realizedPnl=pnl,
        commission="0.1",
        commissionAsset="USDT",
        time=t if t is not None else tid,
    )


def _flow(
    tran: int,
    trade: str,
    sym: str = "BTCUSDT",
    amount: str = "-0.1",
    t: int = 1,
) -> IncomeDTO:
    return IncomeDTO(
        symbol=sym,
        incomeType="COMMISSION",
        income=amount,
        asset="USDT",
        time=t,
        tranId=str(tran),
        tradeId=trade,
    )


def test_same_trade_id_on_different_symbols_does_not_merge() -> None:
    btc = project_episodes(
        [_fill(1, "BUY", "1", t=1000), _fill(2, "SELL", "1", "110", "10", t=2000)],
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"btc-{f.trade_id}-{d}",
    )
    eth = project_episodes(
        [
            _fill(1, "BUY", "2", t=1000, sym="ETHUSDT"),
            _fill(2, "SELL", "2", "110", "20", t=2000, sym="ETHUSDT"),
        ],
        symbol="ETHUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"eth-{f.trade_id}-{d}",
    )
    episodes = [*btc, *eth]
    fills = [
        _fill(1, "BUY", "1", t=1000),
        _fill(2, "SELL", "1", "110", "10", t=2000),
        _fill(1, "BUY", "2", t=1000, sym="ETHUSDT"),
        _fill(2, "SELL", "2", "110", "20", t=2000, sym="ETHUSDT"),
    ]
    flows = [
        _flow(1, "1", "BTCUSDT", t=1000),
        _flow(2, "2", "BTCUSDT", t=2000),
        _flow(3, "1", "ETHUSDT", t=1000),
        _flow(4, "2", "ETHUSDT", t=2000),
    ]
    money = account_episodes(episodes, fills, flows, proof=PROOF)
    assert set(money) == {e.public_ref for e in episodes}
    assert all(v.net is not None and v.issues == () for v in money.values())
    btc_refs = {e.public_ref for e in btc}
    eth_refs = {e.public_ref for e in eth}
    btc_comm = sum((money[r].commission for r in btc_refs), D(0))
    eth_comm = sum((money[r].commission for r in eth_refs), D(0))
    assert btc_comm == D("-0.2") and eth_comm == D("-0.2")


def test_shared_flip_commission_counted_once_with_exact_total() -> None:
    rows = [
        _fill(1, "BUY", "1", t=1000),
        _fill(2, "SELL", "2", "110", "10", t=2000),
        _fill(3, "BUY", "1", "100", "5", t=3000),
    ]
    long, short = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    flows = [_flow(1, "1", t=1000), _flow(2, "2", t=2000), _flow(3, "3", t=3000)]
    money = account_episodes([long, short], rows, flows, proof=PROOF)
    total = money[long.public_ref].commission + money[short.public_ref].commission
    assert total == D("-0.3")
    assert money[long.public_ref].commission == D("-0.15")
    assert money[short.public_ref].commission == D("-0.15")
    assert money[long.public_ref].net == D("9.85")
    assert money[short.public_ref].net == D("4.85")


def test_uneven_shared_split_preserves_signed_total_exactly() -> None:
    rows = [
        _fill(1, "BUY", "2", t=1000),
        _fill(2, "SELL", "3", "110", "20", t=2000),
        _fill(3, "BUY", "1", "100", "10", t=3000),
    ]
    long, short = project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )
    flows = [_flow(1, "1", t=1000), _flow(2, "2", t=2000), _flow(3, "3", t=3000)]
    money = account_episodes([long, short], rows, flows, proof=PROOF)
    total = money[long.public_ref].commission + money[short.public_ref].commission
    assert total == D("-0.3")
    assert all(v.net is not None and v.issues == () for v in money.values())
