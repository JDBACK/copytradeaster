"""Capture N seconds of the master user-data stream to a replay fixture.

Usage: python scripts/capture_stream.py SECONDS [outfile]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from copytradeaster.aster.rate_limits import RateLimits  # noqa: E402
from copytradeaster.aster.trading_api import AsterClient  # noqa: E402
from copytradeaster.aster.user_stream import UserStream  # noqa: E402
from copytradeaster.config import Settings  # noqa: E402
from copytradeaster.credentials.service import SqliteCredentials  # noqa: E402
from copytradeaster.storage.database import Database  # noqa: E402
from copytradeaster.storage.repositories import NonceStore  # noqa: E402


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    out = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else Path("tests/replay/fixtures/captured_stream.json")
    )

    cfg = Settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    creds = SqliteCredentials(db, cfg.encryption_key_bytes())
    await creds.load()
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    client = AsterClient(
        http=http,
        base_url=cfg.rest_base_url,
        limiter=RateLimits(
            cfg.weight_limit_per_min, cfg.orders_limit_per_min, cfg.orders_limit_per_10s
        ),
        identities=creds,
        keys=creds,
        nonces=NonceStore(db),
    )
    events: list[dict] = []

    async def on_event(event):
        events.append({"received_at": event.received_at, "raw": event.raw})

    stop = asyncio.Event()
    stream = UserStream(
        client=client,
        account_id="main",
        ws_base=cfg.ws_user_base,
        keepalive_min=cfg.listenkey_keepalive_min,
        on_event=on_event,
        on_reconnect=lambda: asyncio.sleep(0),
    )
    task = asyncio.create_task(stream.run(stop))
    print(f"capturing {seconds}s of master stream -> {out}")
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"captured_at": time.time(), "count": len(events), "events": events}
    out.write_text(json.dumps(payload, indent=2))
    print(f"captured {len(events)} events")
    await http.aclose()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
