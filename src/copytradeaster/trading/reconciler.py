"""Reconciler: full actual-vs-target sweep per follower (not just changed symbols).

Triggers: startup, post-command debounce, master reconnect, periodic sweep,
manual CLI. Invariant: reconcile() on a consistent state creates 0 commands.

Production snapshot policy: the sweep path passes an explicit symbol scope
(whitelist + active master episodes + open copy-trade episodes) so each
follower reads per-symbol weight-1 openOrders instead of the weight-40
all-symbol read. ``snapshot_scope=None`` keeps the authoritative all-symbol
fetch for startup/boundary/manual passes, which is also the bounded fallback
healing any symbol a scoped sweep could not see. An explicitly passed scope
that normalizes to empty falls back to all-symbol: a sweep must never read
nothing.

The scope is driven through the coordinator's ``snapshot_symbols`` switch
owned by the (wave-E frozen) coordinator module: it is assigned for the
duration of the follower loop only and restored afterwards, so the configured
static value always wins outside a sweep.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from copytradeaster.logging_utils import get_logger
from copytradeaster.master.projector import MasterDelta, MasterState
from copytradeaster.trading.coordinator import Coordinator

log = get_logger(__name__)
# Bounded-parallel fan-out for the per-follower recompute. 16 is sized below
# the order-rate windows and the shared HTTP pool: high enough that a 100+
# follower sweep fits inside the 5s master-freshness window, low enough that
# the shared RateLimits limiter (not a local lock) stays the backpressure
# point instead of connection exhaustion.
RECOMPUTE_CONCURRENCY = 16
_SAFE_FRAGMENT = re.compile(r"[^A-Za-z0-9_.:-]+")


def _safe_fragment(value: object, fallback: str = "unknown") -> str:
    """Bounded identifier for user-facing keys; details stay in local logs."""
    text = _SAFE_FRAGMENT.sub("_", str(value or ""))[:80].strip("_")
    return text or fallback


def _error_category(exc: BaseException) -> str:
    """Secret-safe failure class: exchange code or exception type name only."""
    code = getattr(exc, "code", None)
    if code is not None:
        return f"aster_{_safe_fragment(code, 'error')}"
    return _safe_fragment(type(exc).__name__, "error")


def build_snapshot_scope(
    *,
    whitelist: Iterable[str],
    master_symbols: Iterable[str],
    episode_symbols: Iterable[str],
) -> set[str]:
    """Union the cheap scope sources into normalized symbols (may be empty)."""
    scope: set[str] = set()
    for raw in list(whitelist) + list(master_symbols) + list(episode_symbols):
        symbol = str(raw or "").strip().upper()
        if symbol:
            scope.add(symbol)
    return scope


@dataclass(slots=True)
class ReconcileReport:
    per_account: dict[str, int] = field(default_factory=dict)
    # Per-follower sweep failures vanished silently (no journal row, no flag).
    # Every -1 in per_account carries its secret-safe category here so the
    # sweep path can raise the degraded flag with account + cause.
    failures: dict[str, str] = field(default_factory=dict)


class Reconciler:
    def __init__(self, coordinator: Coordinator, *, metrics=None) -> None:
        self._coordinator = coordinator
        # EngineMetrics-shaped recorder (duck-typed); None keeps unit doubles cheap.
        self._metrics = metrics

    async def reconcile(
        self,
        master_state: MasterState,
        account_ids: list[str] | None = None,
        reason: str = "manual",
        *,
        snapshot_scope: Collection[str] | None = None,
    ) -> ReconcileReport:
        """Sweep every requested follower against the full master state."""
        started = time.monotonic()
        report = ReconcileReport()
        followers = [
            fr
            for fr in self._coordinator.followers
            if account_ids is None or fr.account_id in account_ids
        ]
        delta = MasterDelta(
            changed_symbols=frozenset(master_state.positions.keys()) | {"*"},
            state=master_state,
        )
        coordinator = self._coordinator
        previous_scope = getattr(coordinator, "_snapshot_symbols", None)
        if snapshot_scope is None:
            scoped: set[str] | None = None
        else:
            scoped = {
                str(symbol).strip().upper() for symbol in snapshot_scope if str(symbol).strip()
            } or None
        if scoped is not None:
            log.debug("scoped follower snapshot (%s): %s", reason, sorted(scoped))
        coordinator._snapshot_symbols = scoped  # noqa: SLF001 - wave-E owns coordinator.py
        # Safety: concurrency here is I/O overlap only. Per-account state is
        # serialized by fr.lock inside _recompute_follower, SQLite writes by
        # the store write_lock, and exchange backpressure by the shared
        # RateLimits limiter — so followers cannot interleave on one account,
        # on the journal, or on the wire.
        try:
            before = await self._command_counts()
            semaphore = asyncio.Semaphore(RECOMPUTE_CONCURRENCY)

            async def _run(fr) -> tuple[str, str | None]:
                follower_started = time.monotonic()
                try:
                    async with semaphore:
                        await self._coordinator.recompute_follower(fr, delta)
                except Exception as exc:  # noqa: BLE001 - one account must not stop sweep
                    category = _error_category(exc)
                    log.error("reconcile failed for %s: %s", fr.account_id, exc)
                    return fr.account_id, category
                finally:
                    metrics = self._metrics
                    if metrics is not None:
                        metrics.observe_follower(time.monotonic() - follower_started)
                return fr.account_id, None

            # asyncio.gather preserves input order, so results map back onto
            # followers in order and the report shape is exactly as before.
            outcomes = await asyncio.gather(*(_run(fr) for fr in followers))
            after = await self._command_counts()
            for account_id, category in outcomes:
                if category is not None:
                    report.per_account[account_id] = -1
                    report.failures[account_id] = category
                else:
                    report.per_account[account_id] = max(
                        after.get(account_id, 0) - before.get(account_id, 0), 0
                    )
        finally:
            coordinator._snapshot_symbols = previous_scope  # noqa: SLF001 - restore static value
        metrics = self._metrics
        if metrics is not None:
            metrics.observe_reconcile(time.monotonic() - started)
        log.info("reconciliation (%s): %s", reason, report.per_account)
        return report

    async def _command_counts(self) -> Counter:
        """One bounded journal scan counting commands per account.

        Hoisted out of the per-follower loop: the old code scanned
        recent(10_000) twice per follower (203 scans for 103 followers);
        the sweep now reads once before and once after the fan-out.
        """
        repo = self._coordinator.commands
        rows = await repo.recent(10_000)
        counts: Counter = Counter()
        for row in rows:
            counts[str(row["account_id"])] += 1
        return counts

    async def _command_count(self, account_id: str) -> int:
        repo = self._coordinator.commands
        rows = await repo.recent(10_000)
        return sum(1 for r in rows if r["account_id"] == account_id)
