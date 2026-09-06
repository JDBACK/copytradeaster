"""C4 review repros: leg-aware protection lifecycle over durable storage.

Each leg converges independently from CONFIRMED journal commands plus
exchange observations; the mutable observation row only confirms current
state. Unknown-leg cancels never claim a slot.
"""

from __future__ import annotations

import json

from copytradeaster.aster.models import OrderDTO
from copytradeaster.domain.models import PosSide
from copytradeaster.history.protection import load_protection_facts, protection_events
from copytradeaster.storage.repositories import CommandRepo
from tests.unit.aster.test_trading_api import ORDER_RESPONSE

SYMBOL = "BTCUSDT"
RECORDED = 5000
FIRSTS = (("LONG", 11, "90", "ct-l1"), ("SHORT", 21, "110", "ct-s1"))
SECONDS = (("LONG", 13, "100", "ct-l2"), ("SHORT", 23, "120", "ct-s2"))


async def place_command(commands, key, coid, kind, *, price, leg=None):
    payload = {"symbol": SYMBOL, "stopPrice": price}
    if leg is not None:
        payload["positionSide"] = leg
    await commands.insert(key, "a", None, kind, json.dumps(payload), 1, coid)
    await commands.set_status(key, "CONFIRMED")


async def cancel_command(commands, key, replaced, *, status="CONFIRMED"):
    await commands.insert(
        key,
        "a",
        None,
        "CANCEL_ORDER",
        json.dumps({"symbol": SYMBOL, "origClientOrderId": replaced}),
        1,
        None,
    )
    await commands.set_status(key, status)


async def observe(history, scope, order_id, coid, *, kind, key, at, status="NEW"):
    order = OrderDTO.model_validate(
        {**ORDER_RESPONSE, "orderId": order_id, "clientOrderId": coid, "status": status}
    )
    await history.observe_order(scope, order, kind=kind, command_key=key, observed_at=at)


async def observe_cancel(history, scope, order_id, coid, *, key, at):
    await observe(
        history, scope, order_id, coid, kind="CANCEL_ORDER", key=key, at=at, status="CANCELED"
    )


def drafts_for(symbol, baseline, facts):
    return protection_events(symbol, baseline, facts, recorded_at=RECORDED, notify_eligible=True)


async def test_long_stop_90_and_short_stop_110_yield_two_sets(storage):
    db, history, scope = storage
    commands = CommandRepo(db)
    await place_command(commands, "k-long", "ct-long", "PLACE_STOP", price="90", leg="LONG")
    await observe(history, scope, 11, "ct-long", kind="PLACE_STOP", key="k-long", at=60)
    await place_command(commands, "k-short", "ct-short", "PLACE_STOP", price="110", leg="SHORT")
    await observe(history, scope, 12, "ct-short", kind="PLACE_STOP", key="k-short", at=61)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 0, 100)
    assert baseline == {}
    assert [(fact.leg, str(fact.price)) for fact in facts] == [
        (PosSide.LONG, "90"),
        (PosSide.SHORT, "110"),
    ]
    drafts = drafts_for(SYMBOL, baseline, facts)
    assert [item.kind for item in drafts] == ["PROTECTION_SET", "PROTECTION_SET"]
    assert [(item.facts["leg"], item.facts["new_price"]) for item in drafts] == [
        ("LONG", "90"),
        ("SHORT", "110"),
    ]
    assert all(item.trade_ref is None for item in drafts)


async def test_replace_then_cancel_on_both_legs(storage):
    """Sequential production loads: SET, CHANGED, stale-cancel silent, UNCONFIRMED."""
    db, history, scope = storage
    commands = CommandRepo(db)
    for leg, order_id, price, coid in FIRSTS:
        await place_command(commands, f"k-{coid}", coid, "PLACE_STOP", price=price, leg=leg)
        await observe(history, scope, order_id, coid, kind="PLACE_STOP", key=f"k-{coid}", at=60)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 0, 65)
    assert baseline == {}
    first = drafts_for(SYMBOL, baseline, facts)
    assert [(item.kind, item.facts["leg"], item.facts["new_price"]) for item in first] == [
        ("PROTECTION_SET", "LONG", "90"),
        ("PROTECTION_SET", "SHORT", "110"),
    ]
    for leg, order_id, price, coid in SECONDS:
        await place_command(commands, f"k-{coid}", coid, "PLACE_STOP", price=price, leg=leg)
        await observe(history, scope, order_id, coid, kind="PLACE_STOP", key=f"k-{coid}", at=70)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 65, 75)
    assert set(baseline) == {("LONG", "stop"), ("SHORT", "stop")}
    second = drafts_for(SYMBOL, baseline, facts)
    assert [
        (item.kind, item.facts["leg"], item.facts["old_price"], item.facts["new_price"])
        for item in second
    ] == [
        ("PROTECTION_CHANGED", "LONG", "90", "100"),
        ("PROTECTION_CHANGED", "SHORT", "110", "120"),
    ]
    for _, order_id, _, coid in FIRSTS:
        await cancel_command(commands, f"k-x-{coid}", coid)
        await observe_cancel(history, scope, order_id, coid, key=f"k-x-{coid}", at=80)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 75, 85)
    assert [fact.coid for fact in facts] == ["ct-l1", "ct-s1"]
    assert drafts_for(SYMBOL, baseline, facts) == []
    for _, order_id, _, coid in SECONDS:
        await cancel_command(commands, f"k-x-{coid}", coid)
        await observe_cancel(history, scope, order_id, coid, key=f"k-x-{coid}", at=90)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 85, 95)
    last = drafts_for(SYMBOL, baseline, facts)
    assert [(item.kind, item.facts["leg"], item.facts["last_price"]) for item in last] == [
        ("PROTECTION_UNCONFIRMED", "LONG", "100"),
        ("PROTECTION_UNCONFIRMED", "SHORT", "120"),
    ]


async def test_baseline_after_mutable_row_updated_by_cancel(storage):
    db, history, scope = storage
    commands = CommandRepo(db)
    await place_command(commands, "k-l1", "ct-l1", "PLACE_STOP", price="90", leg="LONG")
    await observe(history, scope, 11, "ct-l1", kind="PLACE_STOP", key="k-l1", at=10)
    await cancel_command(commands, "k-x-l1", "ct-l1")
    # The cancel mutates the same order row: kind flips to CANCEL_ORDER.
    await observe_cancel(history, scope, 11, "ct-l1", key="k-x-l1", at=20)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 30, 40)
    assert baseline == {} and facts == []
    assert drafts_for(SYMBOL, baseline, facts) == []
    assert await history.protection_canceled_before(scope, SYMBOL, "ct-l1", 30) is True


async def test_cancel_with_unknown_leg_claims_no_replacement(storage):
    db, history, scope = storage
    commands = CommandRepo(db)
    await place_command(commands, "k-s1", "ct-s1", "PLACE_STOP", price="100")
    await observe(history, scope, 11, "ct-s1", kind="PLACE_STOP", key="k-s1", at=10)
    await cancel_command(commands, "k-x-ghost", "ct-ghost")
    await observe_cancel(history, scope, 99, "ct-ghost", key="k-x-ghost", at=55)
    baseline, facts = await load_protection_facts(history, commands, scope, SYMBOL, 50, 60)
    assert list(baseline) == [("BOTH", "stop")]
    assert len(facts) == 1 and facts[0].leg is None and facts[0].slot is None
    assert drafts_for(SYMBOL, baseline, facts) == []


async def test_baseline_partitions_stop_by_command_leg(storage):
    db, history, scope = storage
    commands = CommandRepo(db)
    await place_command(commands, "k-long", "ct-long", "PLACE_STOP", price="90", leg="LONG")
    await observe(history, scope, 11, "ct-long", kind="PLACE_STOP", key="k-long", at=10)
    await place_command(commands, "k-short", "ct-short", "PLACE_STOP", price="110", leg="SHORT")
    await observe(history, scope, 12, "ct-short", kind="PLACE_STOP", key="k-short", at=20)
    await place_command(commands, "k-both", "ct-both", "PLACE_STOP", price="100")
    await observe(history, scope, 13, "ct-both", kind="PLACE_STOP", key="k-both", at=30)
    await place_command(commands, "k-long2", "ct-long2", "PLACE_STOP", price="95", leg="LONG")
    await observe(history, scope, 14, "ct-long2", kind="PLACE_STOP", key="k-long2", at=40)
    rows = await history.latest_protection_places(scope, SYMBOL, 100)
    assert {(row["leg"], row["client_order_id"]) for row in rows} == {
        ("LONG", "ct-long2"),
        ("SHORT", "ct-short"),
        ("BOTH", "ct-both"),
    }


async def test_confirmed_cancel_command_without_observation_excludes_baseline(storage):
    db, history, scope = storage
    commands = CommandRepo(db)
    await place_command(commands, "k-s1", "ct-s1", "PLACE_STOP", price="100")
    await observe(history, scope, 11, "ct-s1", kind="PLACE_STOP", key="k-s1", at=10)
    # Journal times are real wall-clock ISO: prove against a future horizon.
    horizon = 4_000_000_000_000
    assert await history.protection_canceled_before(scope, SYMBOL, "ct-s1", horizon) is False
    await cancel_command(commands, "k-x-draft", "ct-s1", status="PENDING")
    assert await history.protection_canceled_before(scope, SYMBOL, "ct-s1", horizon) is False
    await commands.set_status("k-x-draft", "CONFIRMED")
    # Exchange-unknown (-2011) confirm: proven cancel, no observation row.
    assert await history.protection_canceled_before(scope, SYMBOL, "ct-s1", horizon) is True
    baseline, _ = await load_protection_facts(history, commands, scope, SYMBOL, horizon, horizon)
    assert baseline == {}
