"""Derive protection events from confirmed order observations and the journal.

Pure fold between storage and publication: the loader assembles protection
facts (stop/take-profit placements and their cancels) from durable order
observations plus the execution-journal payloads that carry the ordered
price levels; :func:`protection_events` folds them into immutable drafts.

Honesty rules, mirroring the plan semantics:

* a placement at a new level is ``PROTECTION_SET`` only when no level is
  tracked for that slot; a different level is ``PROTECTION_CHANGED``.
* a technical replace (new order, then cancel of the old one) emits one
  change at placement time; the stale cancel of a non-active order is
  noise and emits nothing.
* a cancel of the tracked order without replacement is
  ``PROTECTION_UNCONFIRMED``; a cancel that cannot be attributed to a
  tracked slot emits nothing rather than a guessed removal.
* prices come from the journal payload of the proven command. A placement
  without a proven price emits nothing; failed placements are not news.
* events carry no ``trade_ref``: the journal trade id is master-side and
  cannot be joined to a follower episode ref. ``notify_eligible`` is a
  caller policy, not derived here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.storage.events import EventDraft
from copytradeaster.storage.history import HistoryRepository, HistoryScope

_PLACE_KINDS = {"PLACE_STOP": "stop", "PLACE_TP": "take_profit"}
_TERMINAL_PLACE_STATUS = frozenset({"CANCELED", "REJECTED", "EXPIRED"})


@dataclass(frozen=True, slots=True)
class BaselineSlot:
    """Last proven protection level before the folded window."""

    price: Decimal
    coid: str


@dataclass(frozen=True, slots=True)
class ProtectionFact:
    """One confirmed placement or cancel with its proven journal context."""

    coid: str
    kind: str
    status: str
    observed_at: int
    price: Decimal | None
    slot: str | None


async def load_protection_facts(
    history: HistoryRepository,
    commands,
    scope: HistoryScope,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> tuple[dict[str, BaselineSlot], list[ProtectionFact]]:
    """Assemble the baseline and window facts for one symbol.

    ``commands`` is the ``CommandRepo``; only its ``get`` and
    ``by_client_order_id`` reads are used. Every price comes from a stored
    journal payload, never from drum memory.
    """
    baseline: dict[str, BaselineSlot] = {}
    for row in await history.latest_protection_places(scope, symbol, start_ms):
        slot = _PLACE_KINDS.get(str(row["kind"]))
        command = await commands.get(row["command_key"]) if row["command_key"] else None
        if command is None or command["status"] != "CONFIRMED":
            continue
        price = _payload_price(command["payload"])
        if slot is None or price is None:
            continue
        coid = str(row["client_order_id"])
        if await history.protection_canceled_before(scope, symbol, coid, start_ms):
            continue
        baseline[slot] = BaselineSlot(price, coid)
    facts: list[ProtectionFact] = []
    for row in await history.protection_observations(scope, symbol, start_ms, end_ms):
        kind = str(row["kind"])
        command = await commands.get(row["command_key"]) if row["command_key"] else None
        if command is None or command["status"] != "CONFIRMED":
            continue
        payload = command["payload"]
        if kind in _PLACE_KINDS:
            facts.append(
                ProtectionFact(
                    str(row["client_order_id"]),
                    kind,
                    str(row["status"]),
                    int(row["observed_at"]),
                    _payload_price(payload),
                    _PLACE_KINDS[kind],
                )
            )
            continue
        replaced = _payload_replaced(payload)
        slot = None
        price = None
        if replaced:
            placed = await commands.by_client_order_id(scope.account_id, replaced)
            if placed is not None and placed["status"] == "CONFIRMED":
                slot = _PLACE_KINDS.get(str(placed["kind"]))
                price = _payload_price(placed["payload"])
        facts.append(
            ProtectionFact(
                str(row["client_order_id"]),
                kind,
                str(row["status"]),
                int(row["observed_at"]),
                price,
                slot,
            )
        )
    return baseline, facts


def protection_events(
    symbol: str,
    baseline: Mapping[str, BaselineSlot],
    facts: Sequence[ProtectionFact],
    *,
    recorded_at: int,
    notify_eligible: bool,
) -> list[EventDraft]:
    """Fold protection facts into drafts; a repeat fold is state-stable."""
    if not symbol:
        raise ValueError("protection symbol required")
    if type(recorded_at) is not int or recorded_at < 0:
        raise ValueError("recorded_at must be a nonnegative integer")
    state = dict(baseline)
    for slot, held in state.items():
        _require_slot(slot)
        _require_level(held.price)
        if not held.coid:
            raise ValueError("baseline protection order required")
    seen: set[str] = set()
    drafts: list[EventDraft] = []
    for fact in sorted(facts, key=lambda item: (item.observed_at, item.coid)):
        _require_fact(symbol, fact)
        if fact.kind in _PLACE_KINDS:
            if fact.status in _TERMINAL_PLACE_STATUS or fact.price is None:
                continue
            if fact.coid in seen:
                continue
            seen.add(fact.coid)
            slot = _PLACE_KINDS[fact.kind]
            current = state.get(slot)
            if current is None:
                drafts.append(
                    _draft(
                        "PROTECTION_SET",
                        symbol,
                        fact,
                        {"symbol": symbol, "slot": slot, "new_price": _money(fact.price)},
                        recorded_at,
                        notify_eligible,
                    )
                )
            elif current.price != fact.price:
                drafts.append(
                    _draft(
                        "PROTECTION_CHANGED",
                        symbol,
                        fact,
                        {
                            "symbol": symbol,
                            "slot": slot,
                            "old_price": _money(current.price),
                            "new_price": _money(fact.price),
                        },
                        recorded_at,
                        notify_eligible,
                    )
                )
            state[slot] = BaselineSlot(fact.price, fact.coid)
            continue
        slot = fact.slot
        if slot is None:
            for tracked, held in state.items():
                if held.coid == fact.coid:
                    slot = tracked
                    break
        current = state.get(slot) if slot is not None else None
        if current is None:
            if slot is None or fact.price is None:
                continue
            current = BaselineSlot(fact.price, fact.coid)
            state[slot] = current
        if current.coid != fact.coid:
            continue
        drafts.append(
            _draft(
                "PROTECTION_UNCONFIRMED",
                symbol,
                fact,
                {"symbol": symbol, "slot": slot, "last_price": _money(current.price)},
                recorded_at,
                notify_eligible,
            )
        )
        del state[slot]
    return drafts


def _slot_of(kind: str) -> str:
    slot = _PLACE_KINDS.get(kind)
    if slot is None:
        raise ValueError("unknown protection kind")
    return slot


def _require_slot(slot: str) -> None:
    if slot not in ("stop", "take_profit"):
        raise ValueError("unknown protection slot")


def _require_level(price: Decimal | None) -> Decimal:
    if price is None or not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise ValueError("protection price must be a positive Decimal")
    return price


def _require_fact(symbol: str, fact: ProtectionFact) -> None:
    _ = symbol
    if fact.kind not in (*_PLACE_KINDS, "CANCEL_ORDER"):
        raise ValueError("unknown protection kind")
    if not fact.coid:
        raise ValueError("protection order required")
    if type(fact.observed_at) is not int or fact.observed_at < 0:
        raise ValueError("protection time must be a nonnegative integer")
    if fact.kind in _PLACE_KINDS:
        _slot_of(fact.kind)
        if fact.price is not None:
            _require_level(fact.price)
    elif fact.slot is not None:
        _require_slot(fact.slot)
        if fact.price is not None:
            _require_level(fact.price)


def _draft(kind, symbol, fact, facts, recorded_at, notify_eligible) -> EventDraft:
    return EventDraft(
        kind=kind,
        trade_ref=None,
        occurred_at=fact.observed_at,
        recorded_at=recorded_at,
        facts=facts,
        dedup_key=f"evt:prot:{kind}:{symbol}:{fact.coid}",
        notify_eligible=notify_eligible,
    )


def _payload_price(payload: object) -> Decimal | None:
    if not isinstance(payload, str):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _decimal(data.get("stopPrice"))


def _payload_replaced(payload: object) -> str | None:
    if not isinstance(payload, str):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    replaced = data.get("origClientOrderId")
    return replaced if isinstance(replaced, str) and replaced else None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        price = Decimal(str(value))
    except ArithmeticError:
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price


def _money(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text
