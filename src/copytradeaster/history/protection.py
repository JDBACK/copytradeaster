"""Derive protection events from confirmed order observations and the journal.

Pure fold between storage and publication: the loader assembles protection
facts (stop/take-profit placements and their cancels) from durable order
observations plus the execution-journal payloads that carry the ordered
price levels; :func:`protection_events` folds them into immutable drafts.

State key is ``(leg, slot)`` where ``leg`` is a proven
:class:`~copytradeaster.domain.models.PosSide` (or ``None`` when unknown)
and ``slot`` is ``stop``/``take_profit``. Every leg converges independently:
replacing the LONG stop never touches the SHORT stop slot, and an unknown
leg is never defaulted to ``BOTH``.

Lifecycle sources (fail-closed):

* a placement is reconstructed from its CONFIRMED ``PLACE_STOP``/``PLACE_TP``
  journal command row (kind + payload carry slot, leg and price); the mutable
  observation row only confirms exchange state (``observed_at``/``status``).
  The journal and the observation must agree on the kind, otherwise the row
  is skipped rather than guessed.
* a cancel is reconstructed from its CONFIRMED ``CANCEL_ORDER`` command row
  and linked to its placement by account + client order id; leg, slot and
  price come from the linked placement payload, never from the mutable row
  alone. A cancel whose placement cannot be proven keeps ``leg=None`` and
  never claims a slot.
* an absent ``positionSide`` in a placement payload means one-way mode and
  is honestly ``BOTH`` (the exchange request defaults the same way, see
  ``trading.executor._payload_for``); only a present-but-unparsable value
  stays unknown.
* ``occurred_at`` is always the exchange ``observed_at``; the journal
  ``created_at`` travels as explicit evidence (``command_created_at`` with
  ``time_source="observation"``). A missing exchange timestamp is never
  masked with an invented execution time.

Honesty rules, mirroring the plan semantics:

* a placement at a new level is ``PROTECTION_SET`` only when no level is
  tracked for that ``(leg, slot)``; a different level is
  ``PROTECTION_CHANGED``.
* a technical replace (new order, then cancel of the old one) emits one
  change at placement time; the stale cancel of a non-active order is
  noise and emits nothing.
* a cancel of the tracked order without replacement is
  ``PROTECTION_UNCONFIRMED``; a cancel that cannot be attributed to a
  tracked ``(leg, slot)`` emits nothing rather than a guessed removal. A
  cancel proven by its placement link on an untracked key reports the
  removal of pre-window protection (seed, then unconfirm); an unknown-leg
  cancel never does.
* prices come from the journal payload of the proven command. A placement
  without a proven price emits nothing; failed placements are not news.
* events carry no ``trade_ref``: binding protection to a follower episode by
  leg + accepted interval stays best-effort and is unproven inside this
  fold, so every draft is an account-level fact with ``trade_ref=None``.
  ``notify_eligible`` is a caller policy, not derived here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.domain.models import PosSide
from copytradeaster.storage.events import EventDraft
from copytradeaster.storage.history import HistoryRepository, HistoryScope

_PLACE_KINDS = {"PLACE_STOP": "stop", "PLACE_TP": "take_profit"}
_TERMINAL_PLACE_STATUS = frozenset({"CANCELED", "REJECTED", "EXPIRED"})
_TIME_SOURCES = frozenset({"observation", "command"})

#: Baseline/fold state key: proven leg value (``None`` when unknown) + slot.
BaselineKey = tuple[str | None, str]


@dataclass(frozen=True, slots=True)
class BaselineSlot:
    """Last proven protection level before the folded window."""

    price: Decimal
    coid: str
    leg: PosSide | None = None
    command_key: str | None = None
    observed_at: int = 0


@dataclass(frozen=True, slots=True)
class ProtectionFact:
    """One confirmed placement or cancel with its proven journal context."""

    coid: str
    kind: str
    status: str
    observed_at: int
    price: Decimal | None
    slot: str | None
    leg: PosSide | None = None
    command_key: str | None = None
    command_created_at: str | None = None
    time_source: str = "observation"


async def load_protection_facts(
    history: HistoryRepository,
    commands,
    scope: HistoryScope,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> tuple[dict[BaselineKey, BaselineSlot], list[ProtectionFact]]:
    """Assemble the baseline and window facts for one symbol.

    ``commands`` is the ``CommandRepo``; only its ``get`` and
    ``by_client_order_id`` reads are used. Every price and leg comes from a
    stored journal payload, never from drum memory; the mutable observation
    rows only confirm exchange state and window membership.
    """
    baseline: dict[BaselineKey, BaselineSlot] = {}
    for row in await history.latest_protection_places(scope, symbol, start_ms):
        command_key = row["command_key"]
        command = await commands.get(command_key) if command_key else None
        if command is None or command["status"] != "CONFIRMED":
            continue
        slot = _PLACE_KINDS.get(str(command["kind"]))
        price = _payload_price(command["payload"])
        if slot is None or price is None:
            continue
        leg = _payload_leg(command["payload"])
        coid = str(row["client_order_id"])
        if await history.protection_canceled_before(scope, symbol, coid, start_ms):
            continue
        baseline[(_leg_key(leg), slot)] = BaselineSlot(
            price,
            coid,
            leg=leg,
            command_key=str(command_key),
            observed_at=int(row["observed_at"]),
        )
    facts: list[ProtectionFact] = []
    for row in await history.protection_observations(scope, symbol, start_ms, end_ms):
        kind = str(row["kind"])
        command_key = row["command_key"]
        command = await commands.get(command_key) if command_key else None
        if command is None or command["status"] != "CONFIRMED":
            continue
        created = command["created_at"]
        created_text = str(created) if created is not None else None
        proven_key = str(command_key) if command_key else None
        if kind in _PLACE_KINDS:
            slot = _PLACE_KINDS.get(str(command["kind"]))
            if slot is None or slot != _PLACE_KINDS[kind]:
                continue
            facts.append(
                ProtectionFact(
                    str(row["client_order_id"]),
                    kind,
                    str(row["status"]),
                    int(row["observed_at"]),
                    _payload_price(command["payload"]),
                    slot,
                    leg=_payload_leg(command["payload"]),
                    command_key=proven_key,
                    command_created_at=created_text,
                )
            )
            continue
        if kind != "CANCEL_ORDER":
            continue
        replaced = _payload_replaced(command["payload"]) or str(row["client_order_id"])
        if not replaced:
            continue
        placed = await commands.by_client_order_id(scope.account_id, replaced)
        slot = None
        price = None
        leg = None
        if (
            placed is not None
            and placed["status"] == "CONFIRMED"
            and str(placed["kind"]) in _PLACE_KINDS
        ):
            slot = _PLACE_KINDS[str(placed["kind"])]
            price = _payload_price(placed["payload"])
            leg = _payload_leg(placed["payload"])
        facts.append(
            ProtectionFact(
                replaced,
                kind,
                str(row["status"]),
                int(row["observed_at"]),
                price,
                slot,
                leg=leg,
                command_key=proven_key,
                command_created_at=created_text,
            )
        )
    return baseline, facts


def protection_events(
    symbol: str,
    baseline: Mapping[BaselineKey, BaselineSlot],
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
    state: dict[BaselineKey, BaselineSlot] = {}
    for key, held in baseline.items():
        leg_key, slot = _require_baseline_key(key)
        _require_slot(slot)
        _require_level(held.price)
        if not held.coid:
            raise ValueError("baseline protection order required")
        if held.leg is not None and held.leg.value != leg_key:
            raise ValueError("baseline leg disagrees with its key")
        if held.command_key is not None and not held.command_key:
            raise ValueError("baseline protection command required")
        if type(held.observed_at) is not int or held.observed_at < 0:
            raise ValueError("baseline protection time must be a nonnegative integer")
        state[(leg_key, slot)] = held
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
            key = (_leg_key(fact.leg), slot)
            current = state.get(key)
            if current is None:
                drafts.append(
                    _draft(
                        "PROTECTION_SET",
                        symbol,
                        fact,
                        {
                            "symbol": symbol,
                            "slot": slot,
                            "new_price": _money(fact.price),
                            "leg": _leg_key(fact.leg),
                            "command_key": fact.command_key,
                            "command_created_at": fact.command_created_at,
                            "time_source": fact.time_source,
                        },
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
                            "leg": _leg_key(fact.leg),
                            "command_key": fact.command_key,
                            "command_created_at": fact.command_created_at,
                            "time_source": fact.time_source,
                        },
                        recorded_at,
                        notify_eligible,
                    )
                )
            state[key] = BaselineSlot(
                fact.price,
                fact.coid,
                leg=fact.leg,
                command_key=fact.command_key,
                observed_at=fact.observed_at,
            )
            continue
        slot = fact.slot
        leg = fact.leg
        if slot is None or leg is None:
            continue
        key = (leg.value, slot)
        current = state.get(key)
        if current is None:
            if fact.price is None:
                continue
            current = BaselineSlot(fact.price, fact.coid, leg=leg, observed_at=fact.observed_at)
            state[key] = current
        if current.coid != fact.coid:
            continue
        drafts.append(
            _draft(
                "PROTECTION_UNCONFIRMED",
                symbol,
                fact,
                {
                    "symbol": symbol,
                    "slot": slot,
                    "last_price": _money(current.price),
                    "leg": leg.value,
                    "command_key": fact.command_key,
                    "command_created_at": fact.command_created_at,
                    "time_source": fact.time_source,
                },
                recorded_at,
                notify_eligible,
            )
        )
        del state[key]
    return drafts


def _slot_of(kind: str) -> str:
    slot = _PLACE_KINDS.get(kind)
    if slot is None:
        raise ValueError("unknown protection kind")
    return slot


def _leg_key(leg: PosSide | None) -> str | None:
    return leg.value if leg is not None else None


def _require_baseline_key(key: object) -> BaselineKey:
    if not isinstance(key, tuple) or len(key) != 2:
        raise ValueError("baseline protection key must be (leg, slot)")
    leg_key, slot = key
    if leg_key is not None:
        if not isinstance(leg_key, str) or not leg_key:
            raise ValueError("baseline leg must be a proven position side")
        try:
            PosSide(leg_key)
        except ValueError:
            raise ValueError("baseline leg must be a proven position side") from None
    if not isinstance(slot, str):
        raise ValueError("unknown protection slot")
    return leg_key, slot


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
    if fact.leg is not None and not isinstance(fact.leg, PosSide):
        raise ValueError("protection leg must be a proven position side")
    if fact.command_key is not None and not fact.command_key:
        raise ValueError("protection command required")
    if fact.command_created_at is not None and not isinstance(fact.command_created_at, str):
        raise ValueError("protection command time must be text")
    if fact.time_source not in _TIME_SOURCES:
        raise ValueError("protection time source must be explicit")
    if fact.kind in _PLACE_KINDS:
        _slot_of(fact.kind)
        if fact.price is not None:
            _require_level(fact.price)
        if fact.slot is not None and fact.slot != _PLACE_KINDS[fact.kind]:
            raise ValueError("protection slot disagrees with its kind")
    else:
        if fact.slot is not None:
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


def _payload_leg(payload: object) -> PosSide | None:
    """Proven leg from a placement payload.

    An absent ``positionSide`` means one-way mode and is honestly ``BOTH``.
    Only a present-but-unparsable value stays unknown (``None``) so the
    fold never defaults an unknown leg to ``BOTH``.
    """
    if not isinstance(payload, str):
        return PosSide.BOTH
    try:
        data = json.loads(payload)
    except ValueError:
        return PosSide.BOTH
    if not isinstance(data, dict):
        return PosSide.BOTH
    raw = data.get("positionSide")
    if raw is None:
        return PosSide.BOTH
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return PosSide(raw.strip().upper())
    except ValueError:
        return None


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
