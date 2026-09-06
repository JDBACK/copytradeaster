"""Protection derivation: confirmed placements/cancels become stable drafts.

State key is (leg, slot): each leg converges independently, an unknown leg
never claims a slot, and provenance (command key, journal time, time source)
travels with every draft.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.history.protection import (
    BaselineSlot,
    ProtectionFact,
    protection_events,
)

RECORDED = 5000


def place(
    coid,
    kind="PLACE_STOP",
    price="100",
    at=60,
    status="NEW",
    leg=PosSide.BOTH,
    command="cmd-place",
):
    slot = {"PLACE_STOP": "stop", "PLACE_TP": "take_profit"}[kind]
    return ProtectionFact(
        coid,
        kind,
        status,
        at,
        D(price) if price else None,
        slot,
        leg=leg,
        command_key=command,
        command_created_at="2026-09-01T00:00:00+00:00",
    )


def cancel(
    coid,
    slot="stop",
    price="100",
    at=70,
    leg=PosSide.BOTH,
    command="cmd-cancel",
):
    return ProtectionFact(
        coid,
        "CANCEL_ORDER",
        "CANCELED",
        at,
        D(price) if price else None,
        slot,
        leg=leg,
        command_key=command,
        command_created_at="2026-09-01T00:00:01+00:00",
    )


def slot(price, coid, leg=PosSide.BOTH, command="cmd-place"):
    name = "stop"
    return (leg.value, name), BaselineSlot(
        D(price),
        coid,
        leg=leg,
        command_key=command,
        observed_at=10,
    )


def derive(baseline, facts, eligible=True):
    return protection_events(
        "BTCUSDT", dict(baseline), facts, recorded_at=RECORDED, notify_eligible=eligible
    )


def test_first_placement_sets_level_with_stable_key():
    (draft,) = derive({}, [place("ct-s1")])
    assert draft.kind == "PROTECTION_SET" and draft.trade_ref is None
    assert draft.occurred_at == 60 and draft.recorded_at == RECORDED
    assert draft.notify_eligible is True
    assert draft.dedup_key == "evt:prot:PROTECTION_SET:BTCUSDT:ct-s1"
    assert draft.facts == {
        "symbol": "BTCUSDT",
        "slot": "stop",
        "new_price": "100",
        "leg": "BOTH",
        "command_key": "cmd-place",
        "command_created_at": "2026-09-01T00:00:00+00:00",
        "time_source": "observation",
    }


def test_same_price_replacement_is_silent_but_price_move_changes():
    first = derive({}, [place("ct-s1")])
    key, held = slot("100", "ct-s1")
    again = derive({key: held}, [place("ct-s2", at=70)])
    assert again == []
    (changed,) = derive({key: held}, [place("ct-s2", price="110", at=70)])
    assert changed.kind == "PROTECTION_CHANGED"
    assert changed.facts["old_price"] == "100" and changed.facts["new_price"] == "110"
    assert changed.facts["leg"] == "BOTH"
    assert first[0].dedup_key != changed.dedup_key


def test_replace_pair_emits_single_change_and_stale_cancel_is_silent():
    key, held = slot("100", "ct-s1")
    (changed,) = derive({key: held}, [place("ct-s2", price="110", at=70)])
    assert changed.kind == "PROTECTION_CHANGED"
    done_key, done = slot("110", "ct-s2")
    after = derive({done_key: done}, [cancel("ct-s1", at=80)])
    assert after == []


def test_cancel_of_tracked_order_without_replacement_unconfirms():
    key, held = slot("100", "ct-s1")
    (draft,) = derive({key: held}, [cancel("ct-s1", at=80)])
    assert draft.kind == "PROTECTION_UNCONFIRMED"
    assert draft.facts["last_price"] == "100" and draft.occurred_at == 80
    assert draft.facts["leg"] == "BOTH"


def test_linked_cancel_seeds_baseline_from_replaced_command():
    (draft,) = derive({}, [cancel("ct-old", price="90", at=80)])
    assert draft.kind == "PROTECTION_UNCONFIRMED"
    assert draft.facts["last_price"] == "90"


def test_unattributed_cancel_and_failed_placement_emit_nothing():
    ghost = ProtectionFact("ct-ghost", "CANCEL_ORDER", "CANCELED", 70, None, None)
    assert derive({}, [ghost]) == []
    assert derive({}, [place("ct-bad", status="REJECTED")]) == []
    assert derive({}, [place("ct-free", price=None)]) == []


def test_unknown_leg_cancel_never_claims_tracked_slot():
    key, held = slot("100", "ct-s1")
    ghost = ProtectionFact("ct-ghost", "CANCEL_ORDER", "CANCELED", 80, D(100), "stop")
    assert ghost.leg is None
    assert derive({key: held}, [ghost]) == []
    # The tracked slot survives: a later price move still diffs against it.
    (changed,) = derive({key: held}, [place("ct-s2", price="110", at=90)])
    assert changed.kind == "PROTECTION_CHANGED"
    assert changed.facts["old_price"] == "100"


def test_legs_converge_independently():
    drafts = derive(
        {},
        [
            place("ct-long", price="90", at=60, leg=PosSide.LONG),
            place("ct-short", price="110", at=61, leg=PosSide.SHORT),
        ],
    )
    assert [item.kind for item in drafts] == ["PROTECTION_SET", "PROTECTION_SET"]
    assert [item.facts["leg"] for item in drafts] == ["LONG", "SHORT"]
    assert [item.facts["new_price"] for item in drafts] == ["90", "110"]
    long_key, long_held = slot("90", "ct-long", leg=PosSide.LONG)
    short_key, short_held = slot("110", "ct-short", leg=PosSide.SHORT)
    baseline = {long_key: long_held, short_key: short_held}
    (changed,) = derive(baseline, [place("ct-long2", price="95", at=70, leg=PosSide.LONG)])
    assert changed.kind == "PROTECTION_CHANGED"
    assert changed.facts == {
        "symbol": "BTCUSDT",
        "slot": "stop",
        "old_price": "90",
        "new_price": "95",
        "leg": "LONG",
        "command_key": "cmd-place",
        "command_created_at": "2026-09-01T00:00:00+00:00",
        "time_source": "observation",
    }


def test_slots_are_independent_and_take_profit_supported():
    (taken,) = derive({}, [place("ct-t1", kind="PLACE_TP", price="120")])
    assert taken.kind == "PROTECTION_SET" and taken.facts["slot"] == "take_profit"
    key, held = slot("100", "ct-s1")
    drafts = derive(
        {key: held},
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
        derive({("BOTH", "stop"): BaselineSlot(D(100), "")}, [])
    with pytest.raises(ValueError, match=r"\(leg, slot\)"):
        derive({"stop": BaselineSlot(D(100), "ct-s1")}, [])
    with pytest.raises(ValueError, match="baseline leg"):
        derive(
            {("LONG", "stop"): BaselineSlot(D(100), "ct-s1", leg=PosSide.SHORT)},
            [],
        )
    with pytest.raises(ValueError, match="proven position side"):
        derive({}, [place("ct-x", leg="LONG")])
    with pytest.raises(ValueError, match="slot disagrees"):
        derive(
            {},
            [
                ProtectionFact(
                    "ct-x", "PLACE_STOP", "NEW", 60, D(100), "take_profit", leg=PosSide.BOTH
                )
            ],
        )
    with pytest.raises(ValueError, match="time source"):
        derive(
            {},
            [
                ProtectionFact(
                    "ct-x",
                    "PLACE_STOP",
                    "NEW",
                    60,
                    D(100),
                    "stop",
                    leg=PosSide.BOTH,
                    time_source="invented",
                )
            ],
        )
