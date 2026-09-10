from decimal import Decimal as D

import pytest

from copytradeaster.aster.models import UserTradeDTO
from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes


def fill(id, side, qty, price="100", pnl="0", leg="BOTH"):
    return UserTradeDTO(
        id=id,
        orderId=id,
        symbol="BTCUSDT",
        side=side,
        positionSide=leg,
        qty=qty,
        price=price,
        quoteQty=str(D(qty) * D(price)),
        realizedPnl=pnl,
        commission="0.1",
        commissionAsset="USDT",
        time=id,
    )


def project(fills, seed=None, sources=None):
    return project_episodes(
        fills,
        symbol="BTCUSDT",
        seeds=seed or {PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
        order_sources=sources,
    )


async def test_increase_partial_close_and_final_close_aggregate_own_result():
    rows = [
        fill(1, "BUY", "2"),
        fill(2, "BUY", "1", "130"),
        fill(3, "SELL", "1", "120", "10"),
        fill(4, "SELL", "2", "140", "60"),
    ]
    (episode,) = project(rows, sources={n: "copy" for n in range(1, 5)})
    assert episode.average_entry == D(110)
    assert episode.average_exit == D(400) / D(3)
    assert episode.realized_pnl_gross == D(70)
    assert episode.remaining_qty == 0
    assert episode.closed_at == 4
    assert episode.source == "copy"
    assert [a.role for a in episode.allocations] == ["entry", "entry", "exit", "exit"]


def test_flip_splits_quantity_and_assigns_realized_pnl_only_to_closed_leg():
    long, short = project([fill(1, "BUY", "2"), fill(2, "SELL", "3", "110", "20")])
    assert long.exit_qty == D(2) and long.realized_pnl_gross == D(20)
    assert short.entry_qty == D(1) and short.remaining_qty == D(1)
    assert short.direction == "short" and short.realized_pnl_gross == 0
    assert long.public_ref != short.public_ref
    assert long.allocations[-1].quantity + short.allocations[0].quantity == D(3)


def test_hedge_short_buy_is_reduction_not_new_long():
    (episode,) = project(
        [fill(1, "SELL", "2", leg="SHORT"), fill(2, "BUY", "1", "90", "10", leg="SHORT")],
        {PosSide.SHORT: PositionSeed(D(0))},
    )
    assert episode.direction == "short"
    assert episode.remaining_qty == D(1)
    assert episode.exit_qty == D(1)


def test_hedge_legs_remain_independent():
    episodes = project(
        [fill(1, "BUY", "2", leg="LONG"), fill(2, "SELL", "3", leg="SHORT")],
        {PosSide.LONG: PositionSeed(D(0)), PosSide.SHORT: PositionSeed(D(0))},
    )
    assert {e.direction: e.remaining_qty for e in episodes} == {"long": D(2), "short": D(3)}


def test_projection_still_requires_fills_of_the_seeded_book():
    """Selecting the authoritative book is the caller's job, never guessed here."""
    with pytest.raises(ValueError, match="no authoritative seed"):
        project([fill(1, "BUY", "1", leg="LONG")])


def test_known_existing_seed_partial_until_flat_then_next_episode_complete():
    first, second = project(
        [fill(1, "SELL", "2", "110", "20"), fill(2, "BUY", "1")],
        {PosSide.BOTH: PositionSeed(D(2), "placeholder")},
    )
    assert first.public_ref == "placeholder"
    assert first.opened_at is None and first.average_entry is None
    assert first.quality == "partial_history" and first.closed_at == 1
    assert second.quality == "complete"


def test_unknown_seed_never_guesses_flat_from_equal_buys_and_sells():
    (episode,) = project(
        [fill(1, "BUY", "2"), fill(2, "SELL", "2", "110", "20")],
        {PosSide.BOTH: PositionSeed(None, "unknown-position")},
    )
    assert episode.remaining_qty is None and episode.closed_at is None
    assert episode.entry_qty == 0 and episode.exit_qty == 0
    assert episode.realized_pnl_gross == D(20)
    assert all(a.role == "unknown" for a in episode.allocations)


def test_sort_and_deduplicate_without_double_counting():
    a, b = fill(1, "BUY", "2"), fill(2, "SELL", "1", "110", "10")
    assert project([b, a, a]) == project([a, b])


@pytest.mark.parametrize(
    "rows",
    [
        [fill(1, "BUY", "1", pnl="2")],
        [fill(1, "BUY", "1"), fill(1, "BUY", "2")],
    ],
)
def test_inconsistent_facts_rejected(rows):
    with pytest.raises(ValueError):
        project(rows)


def test_hedge_overclose_requires_gap_reconciliation():
    with pytest.raises(ValueError, match="gap"):
        project([fill(1, "SELL", "2", leg="LONG")], {PosSide.LONG: PositionSeed(D(0))})


def test_copy_external_mix_and_unknown_stay_honest():
    rows = [fill(1, "BUY", "1"), fill(2, "BUY", "1")]
    assert project(rows, sources={1: "copy", 2: "external"})[0].source == "mixed"
    assert project(rows, sources={1: "copy"})[0].source == "unknown"
