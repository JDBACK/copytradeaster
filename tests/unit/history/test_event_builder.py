"""Event derivation: transitions become stable immutable drafts."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.history.event_builder import episode_events
from copytradeaster.history.models import EpisodeTransition, PositionSeed, ProjectionResult
from copytradeaster.history.projector import project_with_transitions
from copytradeaster.storage.events import StoredEvent, UserEvent
from copytradeaster.storage.history import HistoryScope
from tests.unit.history.test_projector import fill

RECORDED = 1000
ACCT = "acct"
ENV = "testnet"
SCOPE = HistoryScope(ACCT, ENV)


def by_id(*fills):
    return {row.trade_id: row for row in fills}


def project(rows, seed=None):
    return project_with_transitions(
        rows,
        symbol="BTCUSDT",
        seeds=seed or {PosSide.BOTH: PositionSeed(D(0))},
        reference=lambda f, d: f"trade-{f.trade_id}-{d}",
    )


def published(result):
    return {episode.public_ref: episode for episode in result.episodes}


def derive(old, result, fills, eligible=True, existing=(), recorded=RECORDED, floor=None):
    return episode_events(
        old,
        result,
        fills,
        list(existing),
        account_id=ACCT,
        environment=ENV,
        notify_eligible=eligible,
        recorded_at=recorded,
        rollout_floor_ms=floor,
    )


def stored_row(seq, ref, kind, occurred, key):
    return StoredEvent(
        SCOPE,
        UserEvent(
            event_ref=f"legacy-{seq}",
            kind=kind,
            trade_ref=ref,
            occurred_at=occurred,
            recorded_at=occurred,
            facts={"symbol": "BTCUSDT"},
            dedup_key=key,
            notify_eligible=True,
        ),
        seq,
    )


def persist(drafts, start=1):
    rows = []
    for offset, draft in enumerate(drafts):
        rows.append(
            StoredEvent(
                SCOPE,
                UserEvent(
                    event_ref=f"e{start + offset}",
                    kind=draft.kind,
                    trade_ref=draft.trade_ref,
                    occurred_at=draft.occurred_at,
                    recorded_at=draft.recorded_at,
                    facts=dict(draft.facts),
                    dedup_key=draft.dedup_key,
                    notify_eligible=draft.notify_eligible,
                ),
                start + offset,
            )
        )
    return rows


def repro_rows():
    return [
        fill(1, "BUY", "2"),
        fill(2, "BUY", "1", "130"),
        fill(3, "SELL", "1", "120", "10"),
        fill(4, "SELL", "2", "140", "60"),
    ]


def test_open_emits_its_own_quantity_and_price_with_stable_key():
    rows = [fill(1, "BUY", "2")]
    (draft,) = derive({}, project(rows), by_id(*rows))
    assert draft.kind == "POSITION_OPENED" and draft.notify_eligible is True
    assert draft.occurred_at == 1 and draft.recorded_at == RECORDED
    assert draft.dedup_key == f"hist1:fill:{ACCT}:{ENV}:BTCUSDT:1:entry:0"
    assert draft.facts["symbol"] == "BTCUSDT" and draft.facts["direction"] == "long"
    assert draft.facts["entry_qty"] == "2" and draft.facts["entry_price"] == "100"
    assert draft.facts["quality"] == "complete" and draft.facts["source"] == "unknown"
    again = derive({}, project(rows), by_id(*rows), eligible=False)
    assert [item.dedup_key for item in again] == [draft.dedup_key]


def test_repeat_projection_emits_nothing():
    rows = [fill(1, "BUY", "2")]
    result = project(rows)
    assert derive(published(result), result, by_id(*rows)) == []


def test_increase_emits_added_quantity_with_fill_price():
    first, second = fill(1, "BUY", "2"), fill(2, "BUY", "1", "130")
    old = published(project([first]))
    (draft,) = derive(old, project([first, second]), by_id(first, second))
    assert draft.kind == "POSITION_INCREASED"
    assert draft.facts["added_qty"] == "1" and draft.facts["price"] == "130"
    assert draft.facts["remaining_qty"] == "3" and draft.occurred_at == 2
    assert draft.dedup_key == f"hist1:fill:{ACCT}:{ENV}:BTCUSDT:2:entry:0"


def test_partial_reduction_emits_own_gross_and_remainder():
    first, second = fill(1, "BUY", "2"), fill(2, "BUY", "1", "130")
    third = fill(3, "SELL", "1", "120", "10")
    old = published(project([first, second]))
    (draft,) = derive(old, project([first, second, third]), by_id(first, second, third))
    assert draft.kind == "POSITION_REDUCED"
    assert draft.facts["closed_qty"] == "1" and draft.facts["close_price"] == "120"
    assert draft.facts["realized_gross"] == "10"
    assert draft.facts["remaining_qty"] == "2" and draft.occurred_at == 3


def test_full_close_emits_open_and_single_close_with_result():
    rows = [fill(1, "BUY", "2"), fill(2, "SELL", "2", "140", "60")]
    opened, closed = derive({}, project(rows), by_id(*rows))
    assert opened.kind == "POSITION_OPENED" and closed.kind == "POSITION_CLOSED"
    assert opened.trade_ref == closed.trade_ref
    assert opened.facts["entry_qty"] == "2" and opened.facts["entry_price"] == "100"
    assert closed.facts["realized_gross"] == "60"
    assert closed.facts["entry_price"] == "100" and closed.facts["exit_price"] == "140"
    assert closed.facts["duration_ms"] == 1
    assert closed.dedup_key == f"hist1:fill:{ACCT}:{ENV}:BTCUSDT:2:exit:0"


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
    assert opened.dedup_key == f"hist1:fill:{ACCT}:{ENV}:BTCUSDT:2:entry:0"
    assert closed.dedup_key == f"hist1:fill:{ACCT}:{ENV}:BTCUSDT:2:exit:0"


def test_repro_batch_yields_open_increase_reduce_close():
    rows = repro_rows()
    opened, increased, reduced, closed = derive({}, project(rows), by_id(*rows))
    assert [draft.kind for draft in (opened, increased, reduced, closed)] == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
    ]
    assert (opened.facts["entry_qty"], opened.facts["entry_price"]) == ("2", "100")
    assert (increased.facts["added_qty"], increased.facts["price"]) == ("1", "130")
    assert reduced.facts["realized_gross"] == "10" and reduced.facts["remaining_qty"] == "2"
    assert closed.facts["entry_qty"] == "3" and closed.facts["realized_gross"] == "70"


def test_sequential_publications_match_single_batch():
    rows = repro_rows()
    (batch,) = [derive({}, project(rows), by_id(*rows))]
    sequential = []
    for width in range(1, 5):
        old = published(project(rows[: width - 1])) if width > 1 else {}
        sequential.extend(derive(old, project(rows[:width]), by_id(*rows[:width])))
    assert [(item.kind, item.dedup_key) for item in sequential] == [
        (item.kind, item.dedup_key) for item in batch
    ]
    assert [item.facts for item in sequential] == [item.facts for item in batch]


def test_stale_fact_at_fresh_frontier_is_recorded_without_push():
    rows = repro_rows()
    drafts = derive({}, project(rows), by_id(*rows), recorded=10_000_000)
    assert [draft.kind for draft in drafts] == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
    ]
    assert [draft.notify_eligible for draft in drafts] == [False] * 4


def test_future_fact_never_pushes():
    rows = [fill(1, "BUY", "2")]
    (draft,) = derive({}, project(rows), by_id(*rows), recorded=0)
    assert draft.kind == "POSITION_OPENED" and draft.notify_eligible is False


def test_rollout_floor_silences_older_facts_only():
    rows = repro_rows()
    drafts = derive({}, project(rows), by_id(*rows), floor=3)
    assert [draft.notify_eligible for draft in drafts] == [False, False, True, True]


def test_bad_rollout_floor_rejected():
    rows = [fill(1, "BUY", "2")]
    with pytest.raises(ValueError, match="rollout_floor_ms"):
        derive({}, project(rows), by_id(*rows), floor=-1)


def test_bad_scope_rejected():
    rows = [fill(1, "BUY", "2")]
    with pytest.raises(ValueError, match="scope"):
        episode_events(
            {},
            project(rows),
            by_id(*rows),
            [],
            account_id="",
            environment=ENV,
            notify_eligible=True,
            recorded_at=RECORDED,
        )


def test_foreign_scope_rows_rejected():
    rows = [fill(1, "BUY", "2")]
    (episode,) = project(rows).episodes
    foreign = stored_row(1, episode.public_ref, "POSITION_OPENED", 1, "evt:open:x")
    foreign = StoredEvent(HistoryScope("other", ENV), foreign.event, foreign.seq)
    with pytest.raises(ValueError, match="scope mismatch"):
        derive({}, project(rows), by_id(*rows), existing=[foreign])


def test_legacy_lineage_is_rederived_and_linked_silently():
    rows = repro_rows()
    result = project(rows)
    (episode,) = result.episodes
    ref = episode.public_ref
    legacy = [
        stored_row(1, ref, "POSITION_OPENED", 1, f"evt:open:{ref}"),
        stored_row(2, ref, "POSITION_CLOSED", 4, f"evt:close:{ref}"),
    ]
    drafts = derive(published(result), result, by_id(*rows), existing=legacy)
    kinds = [draft.kind for draft in drafts]
    assert kinds == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
        "HISTORY_CORRECTED",
    ]
    opened = drafts[0]
    assert (opened.facts["entry_qty"], opened.facts["entry_price"]) == ("2", "100")
    correction = drafts[-1]
    assert correction.trade_ref == ref and correction.notify_eligible is False
    assert correction.occurred_at == 4 and correction.recorded_at == RECORDED
    assert correction.dedup_key == f"hist1:corrected:{ACCT}:{ENV}:BTCUSDT:{ref}"
    assert correction.facts["symbol"] == "BTCUSDT"
    assert correction.facts["superseded_count"] == 2
    assert "legacy-1" in correction.facts["superseded_event_refs"]


def test_correction_links_only_once():
    rows = repro_rows()
    result = project(rows)
    (episode,) = result.episodes
    ref = episode.public_ref
    legacy = [stored_row(1, ref, "POSITION_OPENED", 1, f"evt:open:{ref}")]
    first = derive(published(result), result, by_id(*rows), existing=legacy)
    assert first[-1].kind == "HISTORY_CORRECTED"
    again = derive(
        published(result), result, by_id(*rows), existing=[*legacy, *persist(first[-1:])]
    )
    assert [draft.kind for draft in again] == [
        "POSITION_OPENED",
        "POSITION_INCREASED",
        "POSITION_REDUCED",
        "POSITION_CLOSED",
    ]


def test_modern_rows_make_republish_a_noop():
    rows = repro_rows()
    result = project(rows)
    drafts = derive({}, result, by_id(*rows))
    assert derive(published(result), result, by_id(*rows), existing=persist(drafts)) == []


def test_placeholder_without_fills_emits_nothing():
    result = project([], seed={PosSide.BOTH: PositionSeed(D(2), "placeholder")})
    assert len(result.episodes) == 1 and result.episodes[0].allocations == ()
    assert result.transitions == ()
    assert derive({}, result, {}) == []


def test_unknown_seed_allocations_emit_nothing_until_proven_entry():
    rows = [fill(1, "BUY", "2")]
    seed = {PosSide.BOTH: PositionSeed(None, "unknown-position")}
    result = project(rows, seed)
    assert result.transitions == ()
    assert derive({}, result, by_id(*rows), eligible=False) == []


def test_missing_fill_aborts_without_guessed_price():
    rows = [fill(1, "BUY", "2")]
    with pytest.raises(ValueError, match="proven fact"):
        derive({}, project(rows), {})


def test_bad_recorded_at_rejected():
    rows = [fill(1, "BUY", "2")]
    with pytest.raises(ValueError, match="recorded_at"):
        episode_events(
            {},
            project(rows),
            by_id(*rows),
            [],
            account_id=ACCT,
            environment=ENV,
            notify_eligible=True,
            recorded_at=-1,
        )


def test_scope_change_rejected():
    import dataclasses

    rows = [fill(1, "BUY", "2")]
    result = project(rows)
    (episode,) = result.episodes
    moved = dataclasses.replace(episode, symbol="ETHUSDT")
    with pytest.raises(ValueError, match="scope"):
        derive(
            {episode.public_ref: episode},
            ProjectionResult(episodes=(moved,), transitions=result.transitions),
            by_id(*rows),
        )


def test_orphan_transition_rejected():
    transition = EpisodeTransition(
        ref="ghost",
        symbol="BTCUSDT",
        leg=PosSide.BOTH,
        trade_id=9,
        order_id=9,
        role="entry",
        occurred_at=9,
        qty=D(1),
        remainder=D(1),
        price=D(100),
        closed_gross=D(0),
    )
    with pytest.raises(ValueError, match="without a projected episode"):
        derive(
            {},
            ProjectionResult(episodes=(), transitions=(transition,)),
            by_id(fill(9, "BUY", "1")),
        )
