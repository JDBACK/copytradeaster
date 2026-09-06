"""Shared history presenters: proven facts become UI DTOs, nothing guessed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from copytradeaster.application.models import (
    ActivityFilter,
    PositionProtectionState,
    PositionSide,
    PositionSummaryDTO,
    SnapshotFreshness,
)
from copytradeaster.application.trade_views import (
    build_overview,
    episode_side,
    feed_kinds,
    present_event,
    present_fill,
    present_trade,
    present_trade_detail,
    quantity_assets,
    risk_budget_steps,
)
from copytradeaster.storage.episodes import StoredEpisode
from copytradeaster.storage.events import StoredEvent, UserEvent
from copytradeaster.storage.history import HistoryScope
from tests.unit.history.test_projector import fill, project


def summary(**updates):
    base = {
        "symbol": "BTCUSDT",
        "side": PositionSide.LONG,
        "qty": D("0.012"),
        "entry_price": D("62400"),
        "mark_price": D("63433.3"),
        "unrealized_profit": D("12.40"),
        "leverage": 20,
        "protection_state": PositionProtectionState.PROTECTED,
    }
    base.update(updates)
    return PositionSummaryDTO(**base)


def test_open_trade_card_uses_remainder_and_no_net():
    (episode,) = project([fill(1, "BUY", "2")])
    item = present_trade(episode, quantity_asset="BTC")
    assert item.state.value == "open" and item.side is PositionSide.LONG
    assert item.executed_qty == "2" and item.realized_pnl_gross_usdt == "0"
    assert item.net_pnl_usdt is None and item.quality.value == "complete"
    assert item.quantity_asset == "BTC"
    assert item.opened_at == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=1)
    assert item.closed_at is None


def test_closed_trade_card_uses_exit_total_and_duration():
    rows = [fill(1, "BUY", "2"), fill(2, "SELL", "2", "140", "60")]
    episodes = project(rows)
    closed = next(episode for episode in episodes if episode.closed_at is not None)
    item = present_trade(closed, quantity_asset=None)
    assert item.state.value == "closed" and item.executed_qty == "2"
    assert item.realized_pnl_gross_usdt == "60" and item.quantity_asset is None
    detail = present_trade_detail(StoredEpisode(closed, 3), quantity_asset=None)
    assert detail.trade.trade_ref == closed.public_ref
    assert (detail.entry_qty, detail.exit_qty, detail.remaining_qty) == ("2", "2", "0")
    assert detail.average_entry == "100" and detail.average_exit == "140"
    assert detail.duration_ms == 1 and detail.revision == 3
    assert detail.result_state == "gross_only"


def test_unknown_direction_and_partial_quality_stay_honest():
    (episode,) = project(
        [fill(1, "BUY", "2")],
        seed=None,
    )
    assert episode_side(episode.direction) in (PositionSide.LONG, None)
    assert present_trade(episode, quantity_asset="BTC").source.value == "unknown"


def test_partial_placeholder_reports_proven_remainder():
    from copytradeaster.domain.models import PosSide
    from copytradeaster.history.models import PositionSeed

    (episode,) = project([], seed={PosSide.BOTH: PositionSeed(D(2), "placeholder")}, sources={})
    item = present_trade(episode, quantity_asset="BTC")
    assert item.executed_qty == "2" and item.quality.value == "partial_history"
    assert item.side is PositionSide.LONG and item.opened_at is None


def test_unproven_remainder_stays_none_not_zero():
    from dataclasses import replace

    (episode,) = project([fill(1, "BUY", "2")])
    item = present_trade(replace(episode, remaining_qty=None), quantity_asset="BTC")
    assert item.state.value == "open" and item.executed_qty is None


def test_fill_and_event_presenters_keep_ids_and_kinds():
    (row,) = [fill(1, "BUY", "2")]
    facts = present_fill(row)
    assert (facts.trade_id, facts.order_id) == ("1", "1")
    assert facts.side == "BUY" and facts.quantity == "2"
    stored = StoredEvent(
        HistoryScope("a", "testnet"),
        UserEvent(
            event_ref="e1",
            kind="POSITION_OPENED",
            trade_ref="t1",
            occurred_at=60000,
            recorded_at=61000,
            facts={"symbol": "BTCUSDT"},
            dedup_key="k",
            notify_eligible=True,
        ),
        seq=7,
    )
    item = present_event(stored)
    assert item.event_ref == "e1" and item.kind == "POSITION_OPENED"
    assert item.occurred_at == datetime(1970, 1, 1, 0, 1, tzinfo=UTC)


def test_overview_aggregates_proven_snapshot_only():
    overview = build_overview(
        [
            summary(),
            summary(symbol="ETHUSDT", unrealized_profit=D("-2.40")),
            summary(
                symbol="SOLUSDT",
                protection_state=PositionProtectionState.MISSING_STOP,
            ),
        ],
        available_usdt=D("1240"),
        as_of=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
        freshness=SnapshotFreshness.FRESH,
    )
    assert overview.unrealized_pnl_usdt == "22.4" and overview.available_usdt == "1240"
    assert overview.risk_coverage.value == "complete"


def test_overview_stale_marks_partial_and_missing_available():
    overview = build_overview(
        [summary()],
        available_usdt=None,
        as_of=None,
        freshness=SnapshotFreshness.STALE,
    )
    assert overview.risk_coverage.value == "partial"
    assert overview.available_usdt is None and overview.as_of is None


def test_feed_kinds_split_trade_and_system():
    assert set(feed_kinds(ActivityFilter.TRADE) or ()) == {
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
        "PROTECTION_SET",
        "PROTECTION_CHANGED",
        "PROTECTION_UNCONFIRMED",
    }
    assert set(feed_kinds(ActivityFilter.SYSTEM) or ()) == {
        "RISK_CHANGED",
        "COPY_PAUSED",
        "COPY_RESUMED",
        "CONNECTIVITY_LOST",
        "CONNECTIVITY_RESTORED",
        "ENTRY_SKIPPED",
    }
    assert feed_kinds(ActivityFilter.ALL) is None
    assert quantity_assets(["BTCUSDT", "ETHUSDT"], {"BTCUSDT": "BTC"}) == {
        "BTCUSDT": "BTC",
        "ETHUSDT": None,
    }


def test_bad_history_time_rejected():
    import dataclasses

    (episode,) = project([fill(1, "BUY", "2")])
    broken = dataclasses.replace(episode, opened_at=-1)
    with pytest.raises(ValueError, match="nonnegative integer"):
        present_trade(broken, quantity_asset=None)


def test_risk_budget_steps_cover_the_whole_grid_exactly():
    steps = risk_budget_steps(
        D("1240"), min_percent=D("0.005"), max_percent=D("0.05"), step=D("0.005")
    )
    assert len(steps) == 10
    assert steps[0].risk_percent == D("0.005") and steps[0].budget_usdt == D("6.200")
    assert steps[-1].risk_percent == D("0.05") and steps[-1].budget_usdt == D("62.000")
    assert next(s for s in steps if s.risk_percent == D("0.02")).budget_usdt == D("24.80")


@pytest.mark.parametrize(
    "available, minimum, maximum, step",
    [
        (D("NaN"), D("0.005"), D("0.05"), D("0.005")),
        (D("-1"), D("0.005"), D("0.05"), D("0.005")),
        (D("1240"), D("0.05"), D("0.005"), D("0.005")),
        (D("1240"), D("0.005"), D("0.05"), D("0")),
    ],
)
def test_risk_budget_steps_reject_invalid_ranges(available, minimum, maximum, step):
    with pytest.raises(ValueError):
        risk_budget_steps(available, min_percent=minimum, max_percent=maximum, step=step)
