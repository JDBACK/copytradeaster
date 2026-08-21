"""Reconciler: full actual-vs-target sweep per follower (not just changed symbols).

Triggers: startup, post-command debounce, master reconnect, periodic sweep,
manual CLI. Invariant: reconcile() on a consistent state creates 0 commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from copytradeaster.logging_utils import get_logger
from copytradeaster.master.projector import MasterDelta, MasterState
from copytradeaster.trading.coordinator import Coordinator

log = get_logger(__name__)


@dataclass(slots=True)
class ReconcileReport:
    per_account: dict[str, int] = field(default_factory=dict)


class Reconciler:
    def __init__(self, coordinator: Coordinator) -> None:
        self._coordinator = coordinator

    async def reconcile(
        self,
        master_state: MasterState,
        account_ids: list[str] | None = None,
        reason: str = "manual",
    ) -> ReconcileReport:
        """Sweep every requested follower against the full master state."""
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
        for fr in followers:
            before = await self._command_count(fr.account_id)
            try:
                await self._coordinator.recompute_follower(fr, delta)
            except Exception as exc:  # noqa: BLE001 - one account must not stop sweep
                log.error("reconcile failed for %s: %s", fr.account_id, exc)
                report.per_account[fr.account_id] = -1
                continue
            after = await self._command_count(fr.account_id)
            report.per_account[fr.account_id] = max(after - before, 0)
        log.info("reconciliation (%s): %s", reason, report.per_account)
        return report

    async def _command_count(self, account_id: str) -> int:
        repo = self._coordinator.commands
        rows = await repo.recent(10_000)
        return sum(1 for r in rows if r["account_id"] == account_id)
