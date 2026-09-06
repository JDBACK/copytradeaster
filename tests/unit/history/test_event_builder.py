"""Event derivation: episode transitions become stable immutable drafts."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.history.event_builder import episode_events
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projector import project_episodes
from tests.unit.history.test_projector import fill

RECORDED = 1000


def by_id(*fills):
    return {row.trade_id: row for row in fills}


def project(rows, seed=None):
    return project_episodes(
        rows,
        symbol="BTCUSDT",
        seeds=seed or {PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )


def published(episodes):
    return {episode.public_ref: episode for episode in episodes}


def derive(old, new, fills, eligible=True):
    return episode_events(old, new, fills, notify_eligible=eligible, recorded_at=RECORDED)


def test_open_emits_entry_facts_with_stable_key():
    rows = [fill(1, "BUY", "2")]
    (draft,) = derive({}, project(rows), by_id(*rows))
    assert draft.kind == "POSITION_OPENED" and draft.notify_eligible is True
    assert draft.occurred_at == 1 and draft.recorded_at == RECORDED
    assert draft.dedup_key.startswith("evt:open:")
    assert draft.facts["symbol"] == "BTCUSDT" and draft.facts["direction"] == "long"
    assert draft.facts["entry_qty"] == "2" and draft.facts["entry_price"] == "100"
    assert draft.facts["quality"] == "complete" and draft.facts["source"] == "unknown"
    again = derive({}, project(rows), by_id(*rows), eligible=False)
    assert [item.dedup_key for item in again] == [draft.dedup_key]


def test_repeat_projection_emits_nothing():
    rows = [fill(1, "BUY", "2")]
    episodes = project(rows)
    assert derive(published(episodes), episodes, by_id(*rows)) == []


def test_increase_emits_added_quantity_with_fill_price():
    first, second = fill(1, "BUY", "2"), fill(2, "BUY", "1", "130")
    old = published(project([first]))
    (draft,) = derive(old, project([first, second]), by_id(first, second))
    assert draft.kind == "POSITION_INCREASED"
    assert draft.facts["added_qty"] == "1" and draft.facts["price"] == "130"
    assert draft.facts["remaining_qty"] == "3" and draft.occurred_at == 2


def test_partial_reduction_emits_closed_part_and_remainder():
    first, second = fill(1, "BUY", "2"), fill(2, "BUY", "1", "130")
    third = fill(3, "SELL", "1", "120", "10")
    old = published(project([first, second]))
    (draft,) = derive(old, project([first, second, third]), by_id(first, second, third))
    assert draft.kind == "POSITION_REDUCED"
    assert draft.facts["closed_qty"] == "1" and draft.facts["close_price"] == "120"
    assert draft.facts["remaining_qty"] == "2" and draft.occurred_at == 3


def test_full_close_emits_open_and_single_close_with_result():
    rows = [fill(1, "BUY", "2"), fill(2, "SELL", "2", "140", "60")]
    opened, closed = derive({}, project(rows), by_id(*rows))
    assert opened.kind == "POSITION_OPENED" and closed.kind == "POSITION_CLOSED"
    assert opened.trade_ref == closed.trade_ref
    assert closed.facts["realized_gross"] == "60"
    assert closed.facts["entry_price"] == "100" and closed.facts["exit_price"] == "140"
    assert closed.facts["duration_ms"] == 1 and closed.dedup_key.startswith("evt:close:")


def test_close_after_earlier_publication_emits_only_close():
    buy, sell = fill(1, "BUY", "2"), fill(2, "SELL", "2", "140", "60")
    old = published(project([buy]))
    (draft,) = derive(old, project([buy, sell]), by_id(buy, sell))
    assert draft.kind == "POSITION_CLOSED" and draft.occurred_at == 2


def test_flip_closes_old_and_opens_new_without_guessed_link():
    buy, over = fill(1, "BUY", "2"), fill(2, "SELL", "3", "110", "20")
    old = published(project([buy]))
    drafts = derive(old, project([buy, over]), by_id(buy, over))
    kinds = sorted(item.kind for item in drafts)
    assert kinds == ["POSITION_CLOSED", "POSITION_OPENED"]
    opened = next(item for item in drafts if item.kind == "POSITION_OPENED")
    closed = next(item for item in drafts if item.kind == "POSITION_CLOSED")
    assert opened.trade_ref != closed.trade_ref
    assert opened.facts["direction"] == "short"


def test_placeholder_without_fills_emits_nothing():
    episodes = project([], seed={PosSide.BOTH: PositionSeed(D(2), "placeholder")})
    assert len(episodes) == 1 and episodes[0].allocations == ()
    assert derive({}, episodes, {}) == []


def test_unknown_seed_open_keeps_unknown_direction():
    rows = [fill(1, "BUY", "2")]
    seed = {PosSide.BOTH: PositionSeed(None, "unknown-position")}
    (draft,) = derive({}, project(rows, seed), by_id(*rows), eligible=False)
    assert draft.kind == "POSITION_OPENED" and draft.facts["direction"] == "unknown"
    assert draft.notify_eligible is False


def test_missing_fill_aborts_without_guessed_price():
    rows = [fill(1, "BUY", "2")]
    with pytest.raises(ValueError, match="proven fact"):
        derive({}, project(rows), {})


def test_bad_recorded_at_rejected():
    rows = [fill(1, "BUY", "2")]
    with pytest.raises(ValueError, match="recorded_at"):
        episode_events({}, project(rows), by_id(*rows), notify_eligible=True, recorded_at=-1)


def test_scope_change_rejected():
    import dataclasses

    rows = [fill(1, "BUY", "2")]
    (episode,) = project(rows)
    moved = dataclasses.replace(episode, symbol="ETHUSDT")
    with pytest.raises(ValueError, match="scope"):
        derive({episode.public_ref: episode}, [moved], by_id(*rows))
