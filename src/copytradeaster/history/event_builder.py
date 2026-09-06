"""Derive immutable user events from confirmed episode transitions.

Pure layer between projection and storage: translates each uncovered
:class:`EpisodeTransition` of a :class:`ProjectionResult` into one draft.
One batch and sequential publications of the same fills yield identical
semantic histories because every draft describes its own transition
(its quantity, price, closed share and remainder) instead of episode
totals, which keep moving as later fills arrive:

* the first entry transition gives ``POSITION_OPENED`` with its own
  qty/price; later entries give ``POSITION_INCREASED``;
* a partial exit gives ``POSITION_REDUCED`` carrying its own exit gross
  and the remainder after it;
* a zeroing exit gives ``POSITION_CLOSED`` carrying the episode totals.

Dedup identity (first-wins on ``UNIQUE(account, environment, dedup_key)``):

* per-transition events:
  ``hist1:fill:{account_id}:{environment}:{symbol}:{trade_id}:{role}:{seq}``
  where ``seq`` is the occurrence index within that fill and role
  (normally 0; a flip fill yields one ``exit`` and one ``entry``).
  The key carries no polling time, batch size, Decimal formatting or
  projection UUIDs — only stable fill/role facts in full scope.
* corrections: ``hist1:corrected:{account_id}:{environment}:{symbol}:{trade_ref}``,
  one per episode lineage.

Coverage comes from the previously published episodes: a transition whose
``(trade_id, role, seq)`` is already allocated there emits nothing, so a
re-published projection — including a costs finalization that only bumps
the detail revision — emits zero new drafts. Stored rows are never
rewritten. Transitions of an episode whose stored events hold only
pre-C3 ``evt:*`` rows (and no modern ``hist1:fill:`` row) are re-derived
from scratch — the superseded interpretation is excluded from this path —
and each superseded lineage is linked with a silent ``HISTORY_CORRECTED``
event for presenters to exclude.

Honesty rules, mirroring the plan semantics:

* an event fires only on an observed entry/exit transition. A confirmed
  close command alone is not a close event; the zeroing transition is.
* unknown order linkage stays ``unknown``; nothing is attributed to copy.
* transitions carry their own prices, but every uncovered transition
  still requires its fill in ``fills``: a missing fill aborts the whole
  batch instead of emitting an unproven event as final.
* placeholder (partial-history) episodes emit no event until their first
  entry transition is observed; ``unknown``-role allocations never count
  as entries.
* eligibility is per event: the caller ``notify_eligible`` live-edge gate
  AND a fact age within ``LIVE_ELIGIBLE_MS`` of ``recorded_at`` AND a fact
  time at or after ``rollout_floor_ms`` (None means no floor). A week-old
  fill at a fresh frontier is recorded but never pushes. Corrections are
  always silent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal

from copytradeaster.aster.models import UserTradeDTO
from copytradeaster.history.models import (
    LIVE_ELIGIBLE_MS,
    Episode,
    EpisodeTransition,
    FillAllocation,
    ProjectionResult,
)
from copytradeaster.storage.events import EventDraft, StoredEvent

_TRANSITION_KEY_PREFIX = "hist1:fill:"
_CORRECTION_KEY_PREFIX = "hist1:corrected:"
_LEGACY_KEY_PREFIX = "evt:"
_POSITION_KINDS = frozenset(
    {"POSITION_OPENED", "POSITION_INCREASED", "POSITION_REDUCED", "POSITION_CLOSED"}
)
_CORRECTION_REASON = "legacy evt:* interpretation superseded by deterministic transition events"


def episode_events(
    previous: Mapping[str, Episode],
    result: ProjectionResult,
    fills: Mapping[int, UserTradeDTO],
    existing: Sequence[StoredEvent],
    *,
    account_id: str,
    environment: str,
    notify_eligible: bool,
    recorded_at: int,
    rollout_floor_ms: int | None = None,
) -> list[EventDraft]:
    """Translate uncovered transitions into stable drafts.

    ``previous`` maps public refs to the previously published episodes
    (empty on first publication). ``existing`` holds the stored event rows
    for the episodes in ``result`` — used only to detect superseded
    ``evt:*`` lineage and to skip already-linked corrections. ``fills``
    must contain every allocated trade id. ``recorded_at`` stamps every
    draft with the local publication time.
    """
    if type(recorded_at) is not int or recorded_at < 0:
        raise ValueError("recorded_at must be a nonnegative integer")
    if not account_id or not environment:
        raise ValueError("event scope requires account_id and environment")
    if rollout_floor_ms is not None and (type(rollout_floor_ms) is not int or rollout_floor_ms < 0):
        raise ValueError("rollout_floor_ms must be None or a nonnegative integer")
    episodes = {episode.public_ref: episode for episode in result.episodes}
    for transition in result.transitions:
        if transition.ref not in episodes:
            raise ValueError("transition without a projected episode")
    for stored in existing:
        if stored.scope.account_id != account_id or stored.scope.environment != environment:
            raise ValueError("stored event scope mismatch")
    legacy_by_trade = _legacy_by_trade(existing)
    modern_by_trade = _modern_by_trade(existing)
    corrected_trades = _corrected_trades(existing)
    indexed = _index_transitions(result.transitions)
    drafts: list[EventDraft] = []
    for episode in result.episodes:
        ref = episode.public_ref
        stored = previous.get(ref)
        if stored is not None and (
            stored.symbol != episode.symbol or stored.position_side != episode.position_side
        ):
            raise ValueError("episode identity changed scope")
        superseded = legacy_by_trade.get(ref, ())
        forced = bool(superseded) and ref not in modern_by_trade
        covered: set[tuple[int, str, int]] = set()
        had_entry = False
        if not forced and stored is not None:
            covered = _allocation_identities(stored.allocations)
            had_entry = any(alloc.role == "entry" for alloc in stored.allocations)
        entries_emitted = 0
        for transition, seq in indexed.get(ref, ()):
            if (transition.trade_id, transition.role, seq) in covered:
                if transition.role == "entry":
                    entries_emitted += 1
                continue
            _require_fill(fills, transition.trade_id, transition.symbol)
            eligible = _live_eligible(
                notify_eligible, transition.occurred_at, recorded_at, rollout_floor_ms
            )
            if transition.role == "entry":
                drafts.append(
                    _entry_draft(
                        episode,
                        transition,
                        seq,
                        opened=not had_entry and entries_emitted == 0,
                        eligible=eligible,
                        recorded_at=recorded_at,
                        account_id=account_id,
                        environment=environment,
                    )
                )
                entries_emitted += 1
            elif transition.remainder == 0:
                drafts.append(
                    _close_draft(
                        episode,
                        transition,
                        seq,
                        eligible=eligible,
                        recorded_at=recorded_at,
                        account_id=account_id,
                        environment=environment,
                    )
                )
            else:
                drafts.append(
                    _reduced_draft(
                        episode,
                        transition,
                        seq,
                        eligible=eligible,
                        recorded_at=recorded_at,
                        account_id=account_id,
                        environment=environment,
                    )
                )
        if superseded and ref not in corrected_trades:
            drafts.append(
                _correction_draft(
                    episode,
                    superseded,
                    recorded_at=recorded_at,
                    account_id=account_id,
                    environment=environment,
                )
            )
    return drafts


def _live_eligible(
    gate: bool, occurred_at: int, recorded_at: int, rollout_floor_ms: int | None
) -> bool:
    """Per-event push eligibility: live gate, rollout floor and fact age."""
    if not gate:
        return False
    if rollout_floor_ms is not None and occurred_at < rollout_floor_ms:
        return False
    age = recorded_at - occurred_at
    return 0 <= age <= LIVE_ELIGIBLE_MS


def _transition_key(
    account_id: str, environment: str, transition: EpisodeTransition, seq: int
) -> str:
    return (
        f"{_TRANSITION_KEY_PREFIX}{account_id}:{environment}:{transition.symbol}:"
        f"{transition.trade_id}:{transition.role}:{seq}"
    )


def _correction_key(account_id: str, environment: str, symbol: str, trade_ref: str) -> str:
    return f"{_CORRECTION_KEY_PREFIX}{account_id}:{environment}:{symbol}:{trade_ref}"


def _legacy_by_trade(existing: Sequence[StoredEvent]) -> dict[str, list[StoredEvent]]:
    """Pre-C3 ``evt:*`` position rows grouped by episode ref, oldest first."""
    grouped: dict[str, list[StoredEvent]] = {}
    for stored in sorted(existing, key=lambda item: item.seq):
        event = stored.event
        if (
            event.trade_ref is None
            or event.kind not in _POSITION_KINDS
            or not event.dedup_key.startswith(_LEGACY_KEY_PREFIX)
        ):
            continue
        grouped.setdefault(event.trade_ref, []).append(stored)
    return grouped


def _modern_by_trade(existing: Sequence[StoredEvent]) -> set[str]:
    """Episode refs holding at least one deterministic transition event."""
    return {
        stored.event.trade_ref
        for stored in existing
        if stored.event.trade_ref is not None
        and stored.event.dedup_key.startswith(_TRANSITION_KEY_PREFIX)
    }


def _corrected_trades(existing: Sequence[StoredEvent]) -> set[str]:
    """Episode refs whose superseded lineage is already linked."""
    return {
        stored.event.trade_ref
        for stored in existing
        if stored.event.trade_ref is not None
        and stored.event.dedup_key.startswith(_CORRECTION_KEY_PREFIX)
    }


def _index_transitions(
    transitions: Sequence[EpisodeTransition],
) -> dict[str, list[tuple[EpisodeTransition, int]]]:
    """Group transitions by episode ref, tagging the seq within fill/role."""
    counters: dict[tuple[str, int, str], int] = {}
    indexed: dict[str, list[tuple[EpisodeTransition, int]]] = {}
    for transition in transitions:
        marker = (transition.ref, transition.trade_id, transition.role)
        seq = counters.get(marker, 0)
        counters[marker] = seq + 1
        indexed.setdefault(transition.ref, []).append((transition, seq))
    return indexed


def _allocation_identities(
    allocations: Sequence[FillAllocation],
) -> set[tuple[int, str, int]]:
    """Published ``(trade_id, role, seq)`` identities in allocation order."""
    counters: dict[tuple[int, str], int] = {}
    covered: set[tuple[int, str, int]] = set()
    for alloc in allocations:
        marker = (alloc.trade_id, alloc.role)
        seq = counters.get(marker, 0)
        counters[marker] = seq + 1
        covered.add((alloc.trade_id, alloc.role, seq))
    return covered


def _entry_draft(
    episode: Episode,
    transition: EpisodeTransition,
    seq: int,
    *,
    opened: bool,
    eligible: bool,
    recorded_at: int,
    account_id: str,
    environment: str,
) -> EventDraft:
    if opened:
        return EventDraft(
            kind="POSITION_OPENED",
            trade_ref=episode.public_ref,
            occurred_at=transition.occurred_at,
            recorded_at=recorded_at,
            facts={
                "symbol": episode.symbol,
                "direction": episode.direction,
                "side": episode.position_side.value,
                "quality": episode.quality,
                "source": episode.source,
                "entry_qty": _money(transition.qty),
                "entry_price": _money(transition.price),
                "remaining_qty": _maybe_money(transition.remainder),
            },
            dedup_key=_transition_key(account_id, environment, transition, seq),
            notify_eligible=eligible,
        )
    return EventDraft(
        kind="POSITION_INCREASED",
        trade_ref=episode.public_ref,
        occurred_at=transition.occurred_at,
        recorded_at=recorded_at,
        facts={
            "symbol": episode.symbol,
            "direction": episode.direction,
            "side": episode.position_side.value,
            "quality": episode.quality,
            "source": episode.source,
            "added_qty": _money(transition.qty),
            "price": _money(transition.price),
            "remaining_qty": _maybe_money(transition.remainder),
            "entry_qty": _money(episode.entry_qty),
        },
        dedup_key=_transition_key(account_id, environment, transition, seq),
        notify_eligible=eligible,
    )


def _reduced_draft(
    episode: Episode,
    transition: EpisodeTransition,
    seq: int,
    *,
    eligible: bool,
    recorded_at: int,
    account_id: str,
    environment: str,
) -> EventDraft:
    return EventDraft(
        kind="POSITION_REDUCED",
        trade_ref=episode.public_ref,
        occurred_at=transition.occurred_at,
        recorded_at=recorded_at,
        facts={
            "symbol": episode.symbol,
            "direction": episode.direction,
            "side": episode.position_side.value,
            "quality": episode.quality,
            "source": episode.source,
            "closed_qty": _money(transition.qty),
            "close_price": _money(transition.price),
            "remaining_qty": _maybe_money(transition.remainder),
            "realized_gross": _money(transition.closed_gross),
        },
        dedup_key=_transition_key(account_id, environment, transition, seq),
        notify_eligible=eligible,
    )


def _close_draft(
    episode: Episode,
    transition: EpisodeTransition,
    seq: int,
    *,
    eligible: bool,
    recorded_at: int,
    account_id: str,
    environment: str,
) -> EventDraft:
    occurred = episode.closed_at if episode.closed_at is not None else transition.occurred_at
    duration = None
    if episode.opened_at is not None:
        duration = occurred - episode.opened_at
    return EventDraft(
        kind="POSITION_CLOSED",
        trade_ref=episode.public_ref,
        occurred_at=occurred,
        recorded_at=recorded_at,
        facts={
            "symbol": episode.symbol,
            "direction": episode.direction,
            "side": episode.position_side.value,
            "quality": episode.quality,
            "source": episode.source,
            "entry_qty": _money(episode.entry_qty),
            "entry_price": _maybe_money(episode.average_entry),
            "exit_qty": _money(episode.exit_qty),
            "exit_price": _maybe_money(episode.average_exit),
            "realized_gross": _money(episode.realized_pnl_gross),
            "duration_ms": duration,
        },
        dedup_key=_transition_key(account_id, environment, transition, seq),
        notify_eligible=eligible,
    )


def _correction_draft(
    episode: Episode,
    superseded: Sequence[StoredEvent],
    *,
    recorded_at: int,
    account_id: str,
    environment: str,
) -> EventDraft:
    occurred = max(stored.event.occurred_at for stored in superseded)
    refs = sorted({stored.event.event_ref for stored in superseded})
    return EventDraft(
        kind="HISTORY_CORRECTED",
        trade_ref=episode.public_ref,
        occurred_at=occurred,
        recorded_at=recorded_at,
        facts={
            "symbol": episode.symbol,
            "reason": _CORRECTION_REASON,
            "superseded_event_refs": json.dumps(refs, separators=(",", ":")),
            "superseded_count": len(refs),
        },
        dedup_key=_correction_key(account_id, environment, episode.symbol, episode.public_ref),
        notify_eligible=False,
    )


def _require_fill(fills: Mapping[int, UserTradeDTO], trade_id: int, symbol: str) -> UserTradeDTO:
    fill = fills.get(trade_id)
    if fill is None or fill.symbol != symbol:
        raise ValueError("allocated fill is not a proven fact")
    return fill


def _money(value: Decimal) -> str:
    text = format(value, "f")
    return "0" if value == 0 else text


def _maybe_money(value: Decimal | None) -> str | None:
    return _money(value) if value is not None else None
