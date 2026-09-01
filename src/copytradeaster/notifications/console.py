"""Stage-1 notifications: timestamped console lines (logging INFO)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from copytradeaster.logging_utils import get_logger
from copytradeaster.notifications.models import NotificationEvent

log = get_logger("copytradeaster.notify")


async def notify(text: str) -> None:
    line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {text}"
    print(line, flush=True)
    log.info(text)


class ConsoleNotifier:
    """Always-on local sink for structured events."""

    def __init__(self, sink: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._sink = sink

    async def emit(self, event: NotificationEvent) -> None:
        account = "" if event.account_id is None else f" [{event.account_id}]"
        line = f"{event.severity} {event.kind}{account}: {event.text}"
        if self._sink is None:
            await notify(line)
        else:
            await self._sink(line)
