"""Process-local follower read model shared by engine and presentation.

Trading still reads Aster directly. This store only publishes already-normalized
``AccountState`` snapshots and presentation-only mark updates to Mini App readers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from copytradeaster.domain.models import AccountState, PositionState, PosSide
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)

ACCOUNT_RESOURCES = frozenset({"dashboard", "positions", "status"})
QUOTE_RESOURCES = frozenset({"dashboard", "positions"})


@dataclass(frozen=True, slots=True)
class SnapshotPublication:
    account_id: str
    state: AccountState
    captured_at: datetime
    published_at: datetime
    revision: int
    resources: frozenset[str]
    source: str


SnapshotListener = Callable[[SnapshotPublication], None]


class FollowerSnapshotStore:
    """Latest-wins in-memory state with a monotonic revision per account.

    All mutation runs on the owning asyncio event-loop thread. Reads are
    lock-free and publications are immutable records; listeners must remain
    synchronous/non-blocking and isolate their own delivery failures.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: dict[str, SnapshotPublication] = {}
        self._listeners: set[SnapshotListener] = set()

    def get(self, account_id: str) -> SnapshotPublication | None:
        return self._entries.get(account_id)

    def publish(
        self,
        state: AccountState,
        *,
        captured_at: datetime | None = None,
        source: str = "rest",
        resources: frozenset[str] = ACCOUNT_RESOURCES,
    ) -> SnapshotPublication:
        now = self._clock()
        previous = self._entries.get(state.account_id)
        record = SnapshotPublication(
            account_id=state.account_id,
            state=state,
            captured_at=captured_at or now,
            published_at=now,
            revision=1 if previous is None else previous.revision + 1,
            resources=resources,
            source=source,
        )
        self._entries[state.account_id] = record
        self._notify(record)
        return record

    def apply_mark_prices(
        self, prices: Mapping[str, Decimal], *, published_at: datetime | None = None
    ) -> tuple[SnapshotPublication, ...]:
        """Apply public quotes to display fields without refreshing REST age."""
        normalized = {
            str(symbol).strip().upper(): price
            for symbol, price in prices.items()
            if isinstance(price, Decimal) and price.is_finite() and price > 0
        }
        if not normalized:
            return ()
        now = published_at or self._clock()
        changed: list[SnapshotPublication] = []
        for account_id, previous in tuple(self._entries.items()):
            positions = dict(previous.state.positions)
            equity_delta = Decimal(0)
            any_changed = False
            for key, position in previous.state.positions.items():
                mark = normalized.get(key.symbol.upper())
                if mark is None or mark == position.mark_price:
                    continue
                unrealized = _unrealized(position, mark)
                equity_delta += unrealized - position.unrealized_profit
                positions[key] = replace(
                    position,
                    mark_price=mark,
                    unrealized_profit=unrealized,
                )
                any_changed = True
            if not any_changed:
                continue
            equity = previous.state.equity_usdt
            state = replace(
                previous.state,
                positions=positions,
                equity_usdt=None if equity is None else equity + equity_delta,
            )
            record = SnapshotPublication(
                account_id=account_id,
                state=state,
                captured_at=previous.captured_at,
                published_at=now,
                revision=previous.revision + 1,
                resources=QUOTE_RESOURCES,
                source="mark-price",
            )
            self._entries[account_id] = record
            self._notify(record)
            changed.append(record)
        return tuple(changed)

    def republish(
        self,
        account_id: str,
        *,
        resources: frozenset[str],
        source: str,
    ) -> SnapshotPublication | None:
        """Emit a presentation boundary without pretending REST got fresher.

        Dashboard/Status also depend on process-wide application state such as
        READY/degraded, which isn't part of ``AccountState``. Re-publish the
        latest immutable follower state while preserving its original capture
        time so live clients can cheaply recompute those DTOs from current
        application flags without another exchange request.
        """
        previous = self._entries.get(account_id)
        if previous is None:
            return None
        now = self._clock()
        record = SnapshotPublication(
            account_id=account_id,
            state=previous.state,
            captured_at=previous.captured_at,
            published_at=now,
            revision=previous.revision + 1,
            resources=resources,
            source=source,
        )
        self._entries[account_id] = record
        self._notify(record)
        return record

    def discard(self, account_id: str) -> None:
        self._entries.pop(account_id, None)

    def subscribe(self, listener: SnapshotListener) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def _notify(self, record: SnapshotPublication) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - presentation cannot break trading reads
                log.exception("follower snapshot listener failed account_id=%s", record.account_id)


def _unrealized(position: PositionState, mark: Decimal) -> Decimal:
    quantity = position.qty
    if position.key.pos_side == PosSide.SHORT:
        quantity = -abs(quantity)
    elif position.key.pos_side == PosSide.LONG:
        quantity = abs(quantity)
    return quantity * (mark - position.entry_price)


__all__ = [
    "ACCOUNT_RESOURCES",
    "QUOTE_RESOURCES",
    "FollowerSnapshotStore",
    "SnapshotPublication",
]
