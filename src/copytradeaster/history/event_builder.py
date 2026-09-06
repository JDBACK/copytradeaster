"""Derive immutable user events from confirmed episode transitions.

Pure layer between projection and storage: compares previously published
episodes with a fresh projection and emits drafts for genuinely new exchange
facts — openings, increases, partial reductions and closes. A repeated
projection of the same facts yields no drafts, so replays and restarts never
duplicate feed rows; the repository dedup key makes that guarantee durable.

Honesty rules, mirroring the plan semantics:

* an event fires only on an observed allocation or a proven episode
  boundary. A confirmed close command alone is not a close event; the
  episode closure is.
* unknown order linkage stays ``unknown``; nothing is attributed to copy.
* prices come from the referenced fills. A missing fill aborts the whole
  batch instead of emitting a priceless event.
* placeholder (partial-history) episodes emit no event until their first
  entry allocation is observed.
* ``notify_eligible`` is a caller policy, not derived here: live capture
  marks fresh windows eligible, historical backfill does not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from copytradeaster.aster.models import UserTradeDTO
from copytradeaster.history.models import Episode
from copytradeaster.storage.events import EventDraft

_ENTRY_ROLES = frozenset({"entry", "unknown"})


def episode_events(
    old: Mapping[str, Episode],
    new: Sequence[Episode],
    fills: Mapping[int, UserTradeDTO],
    *,
    notify_eligible: bool,
    recorded_at: int,
) -> list[EventDraft]:
    """Diff published episodes against a fresh projection.

    ``old`` maps public refs to the previously published state (empty on
    first publication). ``fills`` must contain every allocated trade id.
    ``recorded_at`` stamps every draft with the local publication time.
    """
    if type(recorded_at) is not int or recorded_at < 0:
        raise ValueError("recorded_at must be a nonnegative integer")
    drafts: list[EventDraft] = []
    for episode in new:
        previous = old.get(episode.public_ref)
        if previous is not None and (
            previous.symbol != episode.symbol or previous.position_side != episode.position_side
        ):
            raise ValueError("episode identity changed scope")
        old_allocs = _alloc_set(previous.allocations) if previous is not None else set()
        added = [alloc for alloc in episode.allocations if _alloc_key(alloc) not in old_allocs]
        had_entry = any(role in _ENTRY_ROLES for _, role, _ in old_allocs)
        added_entries = [alloc for alloc in added if alloc.role in _ENTRY_ROLES]
        added_exits = [alloc for alloc in added if alloc.role == "exit"]
        closed_at = episode.closed_at
        closed_now = closed_at is not None and (previous is None or previous.closed_at is None)
        if added_entries and not had_entry:
            occurred = episode.opened_at
            if occurred is None:
                occurred = min(alloc.occurred_at for alloc in added_entries)
            drafts.append(
                EventDraft(
                    kind="POSITION_OPENED",
                    trade_ref=episode.public_ref,
                    occurred_at=occurred,
                    facts={
                        "symbol": episode.symbol,
                        "direction": episode.direction,
                        "side": episode.position_side.value,
                        "quality": episode.quality,
                        "source": episode.source,
                        "entry_qty": _money(episode.entry_qty),
                        "entry_price": _maybe_money(episode.average_entry),
                        "remaining_qty": _maybe_money(episode.remaining_qty),
                    },
                    dedup_key=f"evt:open:{episode.public_ref}",
                    notify_eligible=notify_eligible,
                    recorded_at=recorded_at,
                )
            )
        if not closed_now:
            if had_entry:
                for alloc in added_entries:
                    fill = _require_fill(fills, alloc.trade_id, episode.symbol)
                    drafts.append(
                        EventDraft(
                            kind="POSITION_INCREASED",
                            trade_ref=episode.public_ref,
                            occurred_at=alloc.occurred_at,
                            facts={
                                "symbol": episode.symbol,
                                "direction": episode.direction,
                                "side": episode.position_side.value,
                                "quality": episode.quality,
                                "source": episode.source,
                                "added_qty": _money(alloc.quantity),
                                "price": _money(fill.price),
                                "remaining_qty": _maybe_money(episode.remaining_qty),
                                "entry_qty": _money(episode.entry_qty),
                            },
                            dedup_key=(
                                f"evt:entry:{episode.public_ref}:"
                                f"{alloc.trade_id}:{alloc.role}:{_money(alloc.quantity)}"
                            ),
                            notify_eligible=notify_eligible,
                            recorded_at=recorded_at,
                        )
                    )
            for alloc in added_exits:
                fill = _require_fill(fills, alloc.trade_id, episode.symbol)
                drafts.append(
                    EventDraft(
                        kind="POSITION_REDUCED",
                        trade_ref=episode.public_ref,
                        occurred_at=alloc.occurred_at,
                        facts={
                            "symbol": episode.symbol,
                            "direction": episode.direction,
                            "side": episode.position_side.value,
                            "quality": episode.quality,
                            "source": episode.source,
                            "closed_qty": _money(alloc.quantity),
                            "close_price": _money(fill.price),
                            "remaining_qty": _maybe_money(episode.remaining_qty),
                            "realized_gross": _money(episode.realized_pnl_gross),
                        },
                        dedup_key=(
                            f"evt:exit:{episode.public_ref}:"
                            f"{alloc.trade_id}:{alloc.role}:{_money(alloc.quantity)}"
                        ),
                        notify_eligible=notify_eligible,
                        recorded_at=recorded_at,
                    )
                )
            for alloc in added:
                _require_fill(fills, alloc.trade_id, episode.symbol)
        if closed_now and closed_at is not None:
            duration = None
            if episode.opened_at is not None:
                duration = closed_at - episode.opened_at
            drafts.append(
                EventDraft(
                    kind="POSITION_CLOSED",
                    trade_ref=episode.public_ref,
                    occurred_at=closed_at,
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
                    dedup_key=f"evt:close:{episode.public_ref}",
                    notify_eligible=notify_eligible,
                    recorded_at=recorded_at,
                )
            )
    return drafts


def _alloc_key(alloc) -> tuple[int, str, str]:
    return (alloc.trade_id, alloc.role, _money(alloc.quantity))


def _alloc_set(allocations) -> set[tuple[int, str, str]]:
    return {_alloc_key(alloc) for alloc in allocations}


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
