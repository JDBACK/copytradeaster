"""C7 stable-snapshot feed regression (plan §23.8 R9).

The first page pins a high water; later pages keep ``seq <= high_water``
with a ``(occurred_at, seq)`` keyset newest first, so rows recorded between
pages never interleave. ``latest_seq`` is the live max for the banner.
Cursor-shaped checks run over HTTP to prove the route round-trips v1 and
maps bad cursors to 400, never 500.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import ValidationError
from copytradeaster.application.service import UserApplicationService
from copytradeaster.config import Settings
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    SettingsRepo,
    TelegramUserRepo,
)

BOT_TOKEN = "123456:TEST-TOKEN"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
USER_ID = 7
OTHER_ID = 8
USER_ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_ADDRESS = "0x2222222222222222222222222222222222222222"


class FakeHistoryRuntime:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.testnet = True
        self.cfg = SimpleNamespace(reconcile_interval_s=15, fee_buffer_pct="0.001")
        self.snapshot: dict[str, Any] | None = {"positions": {}, "available_usdt": "1240"}
        self.snapshot_at: datetime | None = NOW
        self.assets: dict[str, str | None] = {"BTCUSDT": "BTC"}

    async def cached_snapshot(self, account_id: str) -> Any:
        del account_id
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def refresh_snapshot(self, account_id: str) -> Any:
        del account_id
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def symbol_assets(self, account_id: str, symbols: list[str]) -> dict[str, str | None]:
        del account_id
        return {symbol: self.assets.get(symbol) for symbol in symbols}


def _make_init_data(token: str, user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF" + "0" * 10,
        "user": f'{{"id":{user_id},"first_name":"T"}}',
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


def _auth(user_id: int = USER_ID) -> dict[str, str]:
    return {"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, user_id)}


async def _record(db: Database, scope: HistoryScope, kind: str, occurred_at: int, tag: str) -> None:
    await EventRepository(db).record(
        scope,
        kind=kind,
        occurred_at=occurred_at,
        recorded_at=occurred_at + 1,
        facts={"symbol": "BTCUSDT"},
        dedup_key=f"evt:{tag}",
    )


@pytest.fixture
async def feed_env(tmp_path: Any) -> Any:
    db = Database(tmp_path / "feed_c7.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("h1", "follower", "h1", USER_ADDRESS)
    await AccountRepo(db).upsert("h2", "follower", "h2", OTHER_ADDRESS)
    await TelegramUserRepo(db).upsert(USER_ID, 700, account_id="h1")
    await TelegramUserRepo(db).upsert(OTHER_ID, 800, account_id="h2")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=D("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        "h1",
    )
    await SettingsRepo(db).set_flag("ready", "true")
    await SettingsRepo(db).set_flag("allow_new_entries", "true")
    runtime = FakeHistoryRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        testnet_check=True,
    )
    settings = Settings(
        encryption_key="00" * 32, telegram_bot_token=BOT_TOKEN, miniapp_enabled=True
    )
    app = create_miniapp_app(settings, service)  # type: ignore[arg-type]
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield db, service, client
    await client.close()
    await db.close()


async def test_feed_first_page_pins_high_water_newest_first(feed_env: Any) -> None:
    db, service, _ = feed_env
    scope = HistoryScope("h1", "testnet")
    for index, kind in enumerate(["POSITION_OPENED", "PROTECTION_SET", "RISK_CHANGED"]):
        await _record(db, scope, kind, index, f"pin:{index}")
    page = await service.activity_feed(USER_ID, limit=2)
    assert [item.kind for item in page.items] == ["RISK_CHANGED", "PROTECTION_SET"]
    assert page.high_water == 3 and page.latest_seq == 3
    assert page.next_cursor is not None and page.next_cursor.startswith("v1:")
    # Empty foreign account pins zero without leaking rows.
    empty = await service.activity_feed(OTHER_ID)
    assert empty.items == () and empty.high_water == 0 and empty.latest_seq == 0
    assert empty.next_cursor is None


async def test_feed_second_page_excludes_between_inserts(feed_env: Any) -> None:
    db, service, _ = feed_env
    scope = HistoryScope("h1", "testnet")
    for index, kind in enumerate(
        ["POSITION_OPENED", "PROTECTION_SET", "RISK_CHANGED", "COPY_PAUSED"]
    ):
        await _record(db, scope, kind, index, f"stable:{index}")
    first = await service.activity_feed(USER_ID, limit=2)
    assert [item.kind for item in first.items] == ["COPY_PAUSED", "RISK_CHANGED"]
    # Two rows land after the pin, above the water.
    await _record(db, scope, "POSITION_OPENED", 10, "stable:new1")
    await _record(db, scope, "COPY_RESUMED", 11, "stable:new2")
    second = await service.activity_feed(USER_ID, cursor=first.next_cursor, limit=2)
    assert [item.kind for item in second.items] == ["PROTECTION_SET", "POSITION_OPENED"]
    assert second.high_water == 4 and second.next_cursor is None
    # The banner sees past the pin while the page stays stable.
    assert second.latest_seq == 6
    assert first.high_water == 4


async def test_feed_late_backfill_invisible_until_refresh(feed_env: Any) -> None:
    db, service, _ = feed_env
    scope = HistoryScope("h1", "testnet")
    await _record(db, scope, "POSITION_OPENED", 10, "backfill:old")
    await _record(db, scope, "COPY_PAUSED", 30, "backfill:new")
    first = await service.activity_feed(USER_ID, limit=1)
    assert [item.kind for item in first.items] == ["COPY_PAUSED"]
    # Late arrival with an older timestamp but a fresh seq sits above water.
    await _record(db, scope, "RISK_CHANGED", 20, "backfill:late")
    second = await service.activity_feed(USER_ID, cursor=first.next_cursor, limit=2)
    assert [item.kind for item in second.items] == ["POSITION_OPENED"]
    assert second.high_water == 2 and second.latest_seq == 3
    # A refresh re-pins and slots the backfill by occurred_at.
    fresh = await service.activity_feed(USER_ID, limit=10)
    assert [item.kind for item in fresh.items] == [
        "COPY_PAUSED",
        "RISK_CHANGED",
        "POSITION_OPENED",
    ]
    assert fresh.high_water == 3 and fresh.latest_seq == 3


async def test_feed_legacy_bare_int_is_first_page(feed_env: Any) -> None:
    db, service, _ = feed_env
    scope = HistoryScope("h1", "testnet")
    for index, kind in enumerate(["POSITION_OPENED", "COPY_PAUSED"]):
        await _record(db, scope, kind, index, f"legacy:{index}")
    for legacy in (0, 1, None, "1"):
        page = await service.activity_feed(USER_ID, cursor=legacy, limit=10)  # type: ignore[arg-type]
        assert [item.kind for item in page.items] == ["COPY_PAUSED", "POSITION_OPENED"]
        assert page.high_water == 2 and page.latest_seq == 2


@pytest.mark.parametrize("bad", ["bogus", "7x", "v1:", "v1:!!!", "v2:abc", "", 1.5, True, -1])
async def test_feed_invalid_cursor_fails_closed(feed_env: Any, bad: Any) -> None:
    _, service, _ = feed_env
    with pytest.raises(ValidationError):
        await service.activity_feed(USER_ID, cursor=bad)


async def test_feed_tampered_cursor_fails_closed(feed_env: Any) -> None:
    db, service, _ = feed_env
    scope = HistoryScope("h1", "testnet")
    for index, kind in enumerate(["POSITION_OPENED", "COPY_PAUSED"]):
        await _record(db, scope, kind, index, f"tamper:{index}")
    first = await service.activity_feed(USER_ID, limit=1)
    assert first.next_cursor is not None
    tampered = first.next_cursor[:-1] + ("A" if not first.next_cursor.endswith("A") else "B")
    with pytest.raises(ValidationError):
        await service.activity_feed(USER_ID, cursor=tampered)
    # A cursor from another filter or another account never crosses over.
    with pytest.raises(ValidationError):
        await service.activity_feed(USER_ID, cursor=first.next_cursor, filter="trade")
    with pytest.raises(ValidationError):
        await service.activity_feed(OTHER_ID, cursor=first.next_cursor)


async def test_feed_repository_validates_page_bounds(feed_env: Any) -> None:
    db, _, _ = feed_env
    scope = HistoryScope("h1", "testnet")
    events = EventRepository(db)
    with pytest.raises(ValueError):
        await events.list_feed_page(scope, high_water=-1)
    with pytest.raises(ValueError):
        await events.list_feed_page(scope, high_water=5, last_occurred_at=1, last_seq=9)
    with pytest.raises(ValueError):
        await events.list_feed_page(scope, last_occurred_at=1, last_seq=None)
    with pytest.raises(ValueError):
        await events.list_feed_page(scope, kinds=["NOPE"])


async def test_feed_http_round_trip_pins_snapshot(feed_env: Any) -> None:
    db, _, client = feed_env
    scope = HistoryScope("h1", "testnet")
    for index, kind in enumerate(
        ["POSITION_OPENED", "PROTECTION_SET", "RISK_CHANGED", "COPY_PAUSED"]
    ):
        await _record(db, scope, kind, index, f"http:{index}")
    resp = await client.post("/miniapp/api/activity/feed", json={"limit": 2}, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert [item["kind"] for item in body["items"]] == ["COPY_PAUSED", "RISK_CHANGED"]
    assert body["high_water"] == 4 and body["latest_seq"] == 4
    cursor = body["next_cursor"]
    assert isinstance(cursor, str) and cursor.startswith("v1:")
    await _record(db, scope, "POSITION_OPENED", 10, "http:new1")
    await _record(db, scope, "COPY_RESUMED", 11, "http:new2")
    resp = await client.post(
        "/miniapp/api/activity/feed", json={"cursor": cursor, "limit": 2}, headers=_auth()
    )
    assert resp.status == 200
    body = await resp.json()
    assert [item["kind"] for item in body["items"]] == ["PROTECTION_SET", "POSITION_OPENED"]
    assert body["high_water"] == 4 and body["latest_seq"] == 6
    assert body["next_cursor"] is None
    # A refresh without the cursor re-pins onto the new rows.
    resp = await client.post("/miniapp/api/activity/feed", json={"limit": 2}, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert [item["kind"] for item in body["items"]] == ["COPY_RESUMED", "POSITION_OPENED"]
    assert body["high_water"] == 6


async def test_feed_http_invalid_cursor_is_400(feed_env: Any) -> None:
    db, _, client = feed_env
    scope = HistoryScope("h1", "testnet")
    await _record(db, scope, "POSITION_OPENED", 0, "http:bad:0")
    await _record(db, scope, "COPY_PAUSED", 1, "http:bad:1")
    resp = await client.post("/miniapp/api/activity/feed", json={"limit": 1}, headers=_auth())
    assert resp.status == 200
    cursor = (await resp.json())["next_cursor"]
    assert isinstance(cursor, str) and cursor.startswith("v1:")
    tampered = cursor[:-1] + ("A" if not cursor.endswith("A") else "B")
    for bad in ["bogus", "7x", "v1:!!!", tampered, "v2:abc"]:
        resp = await client.post(
            "/miniapp/api/activity/feed", json={"cursor": bad}, headers=_auth()
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_input"
    # Pinned filter travels with the cursor; switching it is a 400.
    resp = await client.post(
        "/miniapp/api/activity/feed",
        json={"cursor": cursor, "filter": "trade"},
        headers=_auth(),
    )
    assert resp.status == 400
    # Legacy bare ints still open a first page.
    for legacy in (0, 2, "0"):
        resp = await client.post(
            "/miniapp/api/activity/feed", json={"cursor": legacy}, headers=_auth()
        )
        assert resp.status == 200


async def test_feed_concurrent_sibling_scope_stays_isolated(feed_env: Any) -> None:
    db, service, _ = feed_env
    await asyncio.gather(
        _record(db, HistoryScope("h1", "testnet"), "POSITION_OPENED", 0, "iso:h1"),
        _record(db, HistoryScope("h2", "testnet"), "COPY_PAUSED", 0, "iso:h2"),
    )
    mine = await service.activity_feed(USER_ID)
    theirs = await service.activity_feed(OTHER_ID)
    assert [item.kind for item in mine.items] == ["POSITION_OPENED"]
    assert [item.kind for item in theirs.items] == ["COPY_PAUSED"]
