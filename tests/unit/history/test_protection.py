"""Protection derivation: confirmed placements/cancels become stable drafts."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from copytradeaster.history.protection import (
    BaselineSlot,
    ProtectionFact,
    protection_events,
)

RECORDED = 5000


def place(coid, kind="PLACE_STOP", price="100", at=60, status="NEW"):
    return ProtectionFact(coid, kind, status, at, D(price) if price else None, None)


def cancel(coid, slot="stop", price="100", at=70):
    return ProtectionFact(coid, "CANCEL_ORDER", "CANCELED", at, D(price) if price else None, slot)


def derive(baseline, facts, eligible=True):
    return protection_events(
        "BTCUSDT", baseline, facts, recorded_at=RECORDED, notify_eligible=eligible
    )


def test_first_placement_sets_level_with_stable_key():
    (draft,) = derive({}, [place("ct-s1")])
    assert draft.kind == "PROTECTION_SET" and draft.trade_ref is None
    assert draft.occurred_at == 60 and draft.recorded_at == RECORDED
    assert draft.notify_eligible is True
    assert draft.dedup_key == "evt:prot:PROTECTION_SET:BTCUSDT:ct-s1"
    assert draft.facts == {"symbol": "BTCUSDT", "slot": "stop", "new_price": "100"}


def test_same_price_replacement_is_silent_but_price_move_changes():
    first = derive({}, [place("ct-s1")])
    again = derive({"stop": BaselineSlot(D(100), "ct-s1")}, [place("ct-s2", at=70)])
    assert again == []
    (changed,) = derive(
        {"stop": BaselineSlot(D(100), "ct-s1")}, [place("ct-s2", price="110", at=70)]
    )
    assert changed.kind == "PROTECTION_CHANGED"
    assert changed.facts["old_price"] == "100" and changed.facts["new_price"] == "110"
    assert first[0].dedup_key != changed.dedup_key


def test_replace_pair_emits_single_change_and_stale_cancel_is_silent():
    baseline = {"stop": BaselineSlot(D(100), "ct-s1")}
    (changed,) = derive(baseline, [place("ct-s2", price="110", at=70)])
    assert changed.kind == "PROTECTION_CHANGED"
    after = derive({"stop": BaselineSlot(D(110), "ct-s2")}, [cancel("ct-s1", at=80)])
    assert after == []


def test_cancel_of_tracked_order_without_replacement_unconfirms():
    (draft,) = derive({"stop": BaselineSlot(D(100), "ct-s1")}, [cancel("ct-s1", at=80)])
    assert draft.kind == "PROTECTION_UNCONFIRMED"
    assert draft.facts["last_price"] == "100" and draft.occurred_at == 80


def test_cancel_seeds_baseline_from_replaced_command():
    (draft,) = derive({}, [cancel("ct-old", price="90", at=80)])
    assert draft.kind == "PROTECTION_UNCONFIRMED"
    assert draft.facts["last_price"] == "90"


def test_unattributed_cancel_and_failed_placement_emit_nothing():
    assert derive({}, [cancel("ct-ghost", slot=None, price=None)]) == []
    assert derive({}, [place("ct-bad", status="REJECTED")]) == []
    assert derive({}, [place("ct-free", price=None)]) == []


def test_slots_are_independent_and_take_profit_supported():
    (taken,) = derive({}, [place("ct-t1", kind="PLACE_TP", price="120")])
    assert taken.kind == "PROTECTION_SET" and taken.facts["slot"] == "take_profit"
    drafts = derive(
        {"stop": BaselineSlot(D(100), "ct-s1")},
        [place("ct-t1", kind="PLACE_TP", price="120", at=70)],
    )
    assert len(drafts) == 1 and drafts[0].facts["slot"] == "take_profit"


def test_repeat_fold_is_stable_and_eligible_is_caller_policy():
    facts = [place("ct-s1"), place("ct-s2", price="110", at=70)]
    first = derive({}, facts)
    second = derive({}, facts, eligible=False)
    assert [item.dedup_key for item in first] == [item.dedup_key for item in second]
    assert all(item.notify_eligible is False for item in second)
    assert [item.kind for item in first] == ["PROTECTION_SET", "PROTECTION_CHANGED"]


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError, match="symbol"):
        protection_events("", {}, [], recorded_at=RECORDED, notify_eligible=True)
    with pytest.raises(ValueError, match="recorded_at"):
        protection_events("BTCUSDT", {}, [], recorded_at=-1, notify_eligible=True)
    with pytest.raises(ValueError, match="protection kind"):
        derive({}, [ProtectionFact("ct-x", "MARKET_OPEN", "NEW", 60, D(1), None)])
    with pytest.raises(ValueError, match="protection time"):
        derive({}, [place("ct-x", at=-1)])
    with pytest.raises(ValueError, match="positive Decimal"):
        derive({}, [place("ct-x", price="0")])
    with pytest.raises(ValueError, match="baseline"):
        derive({"stop": BaselineSlot(D(100), "")}, [])
