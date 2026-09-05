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

Snapshot scope is call-local, so overlapping sweeps cannot change each
other's reads. Reports count evaluated actions, including retries/blocks;
zero means an evaluated empty plan, not an unchanged journal row count.
"""

from __future__ import annotations

import asyncio
import re
import time
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
    authoritative: bool = True
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
        report = ReconcileReport(authoritative=not snapshot_scope)
        followers = [
            fr
            for fr in self._coordinator.followers
            if account_ids is None or fr.account_id in account_ids
        ]
        delta = MasterDelta(
            changed_symbols=frozenset(master_state.positions.keys()) | {"*"},
            state=master_state,
        )
        scoped = (
            {str(s).strip().upper() for s in snapshot_scope if str(s).strip()}
            if snapshot_scope
            else None
        )
        semaphore = asyncio.Semaphore(RECOMPUTE_CONCURRENCY)

        async def _run(fr) -> tuple[str, int, str | None]:
            follower_started = time.monotonic()
            try:
                async with semaphore:
                    count = await self._coordinator.recompute_follower(
                        fr, delta, snapshot_scope=scoped
                    )
                if count is None or count < 0:
                    return fr.account_id, -1, "generation-not-evaluated"
                return fr.account_id, count, None
            except Exception as exc:
                category = _error_category(exc)
                log.error("reconcile failed for %s: %s", fr.account_id, category)
                return fr.account_id, -1, category
            finally:
                if self._metrics is not None:
                    self._metrics.observe_follower(time.monotonic() - follower_started)

        for account_id, count, category in await asyncio.gather(*(_run(fr) for fr in followers)):
            report.per_account[account_id] = count
            if category is not None:
                report.failures[account_id] = category
        metrics = self._metrics
        if metrics is not None:
            metrics.observe_reconcile(time.monotonic() - started)
        log.info("reconciliation (%s): %s", reason, report.per_account)
        return report
