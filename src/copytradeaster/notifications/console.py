"""Stage-1 notifications: timestamped console lines (logging INFO)."""

from __future__ import annotations

from datetime import UTC, datetime

from copytradeaster.logging_utils import get_logger

log = get_logger("copytradeaster.notify")


async def notify(text: str) -> None:
    line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {text}"
    print(line, flush=True)
    log.info(text)
