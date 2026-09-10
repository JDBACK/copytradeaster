"""Live acceptance: the shipped Mini App live channel against the running bot.

jsdom proves the client logic, not the shipped transport. This drives the REAL
/miniapp/api/live WebSocket of a running instance the way the bundle does
(real session, single-use ticket) and measures what the merge-review
correctives claimed:

  * the channel opens with a hello revision baseline and then keeps revisions
    strictly increasing,
  * mark-driven invalidations are coalesced (one publication per cadence
    window), never one per symbol frame,
  * a client that stops reading for a while catches up with a bounded burst
    instead of receiving a fenced backlog,
  * a live ticket is single-use.

The local session comes from a locally minted Telegram stub
(data/preview-init.js, git-ignored, a session credential): mint it with
.venv/bin/python data/make_preview_init.py before running. The test talks only
to the local instance and never sends trading commands.

Run: CT_TESTNET=1 pytest tests/testnet/test_live_channel_cadence_acceptance.py -v

MINIAPP_LOCAL_URL overrides the local base URL (default http://127.0.0.1:8080).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]

ROOT = Path(__file__).resolve().parents[2]
INIT_STUB = ROOT / "data" / "preview-init.js"
BASE_URL = os.environ.get("MINIAPP_LOCAL_URL", "http://127.0.0.1:8080").rstrip("/")
INIT_DATA_HEADER = "X-Telegram-Init-Data"
READ_WINDOW_S = 20.0
SLOW_WINDOW_S = 12.0
COALESCED_MAX_PER_S = 2.0


def _init_data() -> str:
    if not INIT_STUB.exists():
        pytest.skip("data/preview-init.js is missing; run data/make_preview_init.py first")
    text = INIT_STUB.read_text(encoding="utf-8")
    match = re.search(r"initData:\s*[\"']([^\"']+)[\"']", text)
    if match is None:
        pytest.skip("data/preview-init.js has no initData field")
    return match.group(1)


@pytest.fixture
async def live_instance():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(15.0)) as client:
        try:
            probe = await client.get("/miniapp/")
        except httpx.HTTPError:
            pytest.skip(f"no Mini App instance on {BASE_URL}")
        if probe.status_code != 200:
            pytest.skip(f"Mini App instance is not serving on {BASE_URL}")
        headers = {INIT_DATA_HEADER: _init_data()}
        session = await client.post("/miniapp/api/session", headers=headers, json={})
        if session.status_code != 200:
            pytest.fail(
                f"the minted session was rejected ({session.status_code}); "
                "re-mint data/preview-init.js"
            )
        yield client, headers


async def _ticket(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post("/miniapp/api/live-ticket", headers=headers, json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("ticket"), body
    return str(body["ticket"])


def _ws_url() -> str:
    if BASE_URL.startswith("https://"):
        return "wss://" + BASE_URL[len("https://") :]
    return "ws://" + BASE_URL[len("http://") :]


async def test_live_channel_coalesces_and_recovers(live_instance):
    client, headers = live_instance
    ticket = await _ticket(client, headers)
    url = _ws_url()

    async with connect(f"{url}/miniapp/api/live?ticket={ticket}") as socket:
        hello = json.loads(await asyncio.wait_for(socket.recv(), timeout=10.0))
        assert hello["type"] == "hello", hello
        assert isinstance(hello["revision"], int)

        started = time.monotonic()
        revisions: list[int] = []
        sources: list[str] = []
        while time.monotonic() - started < READ_WINDOW_S:
            remaining = READ_WINDOW_S - (time.monotonic() - started)
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            except TimeoutError:
                break
            event = json.loads(raw)
            if event.get("type") != "update":
                continue
            revisions.append(int(event["revision"]))
            sources.append(str(event.get("source") or ""))

        assert revisions == sorted(revisions), revisions
        assert len(set(revisions)) == len(revisions), revisions
        rate = len(revisions) / READ_WINDOW_S
        assert rate <= COALESCED_MAX_PER_S, (
            f"{len(revisions)} invalidations in {READ_WINDOW_S:g}s: not coalesced"
        )

        positions = await client.post("/miniapp/api/positions", headers=headers, json={})
        assert positions.status_code == 200, positions.text
        held = bool((positions.json().get("data") or {}).get("positions"))
        if held:
            # With an open position the shared mark feed must invalidate the
            # surface, once per cadence window rather than once per frame.
            assert "mark-price" in sources, sources

        # A reader that stops consuming must not be fenced by a backlog: the
        # hub keeps a bounded queue and the session catches up in a burst.
        before_pause = revisions[-1] if revisions else int(hello["revision"])
        await asyncio.sleep(SLOW_WINDOW_S)
        burst: list[int] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=0.5)
            except TimeoutError:
                break
            event = json.loads(raw)
            if event.get("type") == "update":
                burst.append(int(event["revision"]))
        assert len(burst) <= 12, f"slow client received a {len(burst)}-event backlog"
        assert burst == sorted(burst), burst
        if burst:
            assert burst[-1] >= before_pause, (before_pause, burst)
        # The channel survived the slow window: prove it with a round trip
        # rather than trusting a stale connection handle.
        waiter = await asyncio.wait_for(socket.ping(), timeout=5.0)
        await asyncio.wait_for(waiter, timeout=5.0)

    # A consumed ticket cannot open a second channel.
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(10.0)) as probe:
        fresh = await probe.post("/miniapp/api/live-ticket", headers=headers, json={})
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["ticket"] != ticket
        with pytest.raises(Exception) as refused:
            async with connect(f"{url}/miniapp/api/live?ticket={ticket}"):
                pass
        assert "handshake" in str(refused.value).lower() or "401" in str(refused.value)
