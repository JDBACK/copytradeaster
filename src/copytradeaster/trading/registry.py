"""In-process follower runtime registry used by engine and control surfaces.

The registry publishes copy-on-write snapshots.  A coordinator generation can
therefore keep one immutable tuple across every await while onboarding safely
publishes a new, initially-paused follower for the next generation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from copytradeaster.trading.coordinator import FollowerRuntime


class FollowerRegistry:
    """Small single-process registry; it never stores credential material."""

    def __init__(self, runtimes: Iterable[FollowerRuntime] = ()) -> None:
        self._mutation_lock = asyncio.Lock()
        self._by_id: dict[str, FollowerRuntime] = {}
        self._snapshot: tuple[FollowerRuntime, ...] = ()
        self._publish(runtimes)

    async def load_at_boot(self, runtimes: Iterable[FollowerRuntime]) -> None:
        """Replace the initial contents before engine update tasks are admitted."""
        async with self._mutation_lock:
            self._publish(runtimes)

    async def locked_snapshot(self) -> tuple[FollowerRuntime, ...]:
        """Return one immutable snapshot while serializing with publication.

        ``snapshot()`` is intentionally lock-free for the hot fan-out path: the
        tuple is copy-on-write and assignment is atomic on the event loop.  This
        method is the public lifecycle boundary for callers that want an
        explicitly serialized read before a subsequent CAS publication.
        The lock is never held across caller awaits.
        """
        async with self._mutation_lock:
            return self._snapshot

    async def publish_new(self, runtime: FollowerRuntime) -> None:
        """Publish a follower that is not currently registered."""
        async with self._mutation_lock:
            if runtime.account_id in self._by_id:
                raise ValueError(f"follower {runtime.account_id!r} is already registered")
            updated = dict(self._by_id)
            updated[runtime.account_id] = runtime
            self._publish(updated.values())

    async def publish_replacement(
        self,
        runtime: FollowerRuntime,
        *,
        expected: FollowerRuntime,
    ) -> FollowerRuntime:
        """Atomically replace ``expected`` with ``runtime``.

        The application lifecycle owns pause/DB/cache/flatness policy and holds
        the account locks before calling this CAS primitive.  The expected
        object prevents a stale lifecycle from overwriting a newer replacement.
        """
        async with self._mutation_lock:
            current = self._by_id.get(runtime.account_id)
            if current is None:
                raise KeyError(f"follower {runtime.account_id!r} is not registered")
            if current is not expected:
                raise RuntimeError(f"follower {runtime.account_id!r} changed during lifecycle")
            updated = dict(self._by_id)
            updated[runtime.account_id] = runtime
            self._publish(updated.values())
            return current

    async def unpublish(
        self,
        account_id: str,
        *,
        expected: FollowerRuntime,
    ) -> FollowerRuntime:
        """Atomically remove ``expected`` after application-level safety checks."""
        async with self._mutation_lock:
            current = self._by_id.get(account_id)
            if current is None:
                raise KeyError(f"follower {account_id!r} is not registered")
            if current is not expected:
                raise RuntimeError(f"follower {account_id!r} changed during lifecycle")
            updated = dict(self._by_id)
            del updated[account_id]
            self._publish(updated.values())
            return current

    async def add_or_replace(self, runtime: FollowerRuntime) -> None:
        """Backward-compatible hot-add name; replacement requires a lifecycle."""
        await self.publish_new(runtime)

    async def remove(self, account_id: str) -> FollowerRuntime | None:
        """Reject the old unsafe removal API.

        Removal needs an expected runtime and application-level flat/order/
        in-flight checks, so it cannot be made safe from this collection alone.
        """
        del account_id
        raise RuntimeError("follower removal requires App.follower_lifecycle()")

    def get(self, account_id: str) -> FollowerRuntime | None:
        return self._by_id.get(account_id)

    def snapshot(self) -> tuple[FollowerRuntime, ...]:
        return self._snapshot

    def __len__(self) -> int:
        return len(self._snapshot)

    def _publish(self, runtimes: Iterable[FollowerRuntime]) -> None:
        materialized = tuple(runtimes)
        by_id = {runtime.account_id: runtime for runtime in materialized}
        if len(by_id) != len(materialized):
            raise ValueError("duplicate follower account_id in registry")
        ordered = tuple(by_id[account_id] for account_id in sorted(by_id))
        self._by_id = by_id
        self._snapshot = ordered


__all__ = ["FollowerRegistry"]
