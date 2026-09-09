"""Transport tests for C2 split pagination on trade/events."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import (
    FillFacts,
    HistoryPageChangedError,
    TradeEventItem,
    TradeEventsPage,
    TradeSplitError,
    TradeUnavailableError,
)
from copytradeaster.config import Settings
from copytradeaster.miniapp.server import create_miniapp_app

BOT_TOKEN = "123456:TEST-TOKEN"
AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


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


def _fill() -> FillFacts:
    return FillFacts(
        trade_id="2",
        order_id="2",
        side="SELL",
        quantity="3",
        price="110",
        realized_pnl="20",
        executed_at=AT,
        allocated_quantity="2",
        role="exit",
        allocated_realized_pnl="20",
        position_side="BOTH",
    )


@dataclass
class FakeC2Application:
    calls: list = field(default_factory=list)
    fail_with: Exception | None = None
    page: TradeEventsPage | None = None

    async def trade_events(self, user_id, trade_ref, **kwargs):
        self.calls.append(("trade_events", user_id, trade_ref, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        if self.page is not None:
            return self.page
        return TradeEventsPage(
            fills=(_fill(),),
            events=(
                TradeEventItem(
                    event_ref="e1",
                    kind="POSITION_CLOSED",
                    trade_ref="ref-1",
                    occurred_at=AT,
                    facts={"symbol": "BTCUSDT"},
                ),
            ),
            next_cursor=None,
            fills_cursor=None,
            events_cursor=None,
            next_fills_cursor=None,
            next_events_cursor=None,
        )


def _settings() -> Settings:
    return Settings(encryption_key="00" * 32, telegram_bot_token=BOT_TOKEN, miniapp_enabled=True)


@pytest.fixture
def fake_app() -> FakeC2Application:
    return FakeC2Application()


@pytest.fixture
async def client(fake_app):
    app = create_miniapp_app(_settings(), fake_app)  # type: ignore[arg-type]
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


def _auth() -> dict:
    return {"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, 777)}


async def test_split_cursors_pass_through_and_serialize(client, fake_app):
    resp = await client.post(
        "/miniapp/api/trade/events",
        json={
            "trade_ref": "ref-1",
            "fills_cursor": "ZmlsbHM",
            "events_cursor": "9",
            "fills_limit": 10,
            "events_limit": 5,
        },
        headers=_auth(),
    )
    assert resp.status == 200
    assert fake_app.calls[0][3] == {
        "cursor": 0,
        "limit": None,
        "fills_cursor": "ZmlsbHM",
        "events_cursor": 9,
        "fills_limit": 10,
        "events_limit": 5,
    }
    body = await resp.json()
    assert body["fills"][0]["allocated_quantity"] == "2"
    assert body["fills"][0]["role"] == "exit"
    assert body["fills_cursor"] is None and body["next_fills_cursor"] is None


async def test_legacy_cursor_still_pages_events(client, fake_app):
    fake_app.page = TradeEventsPage(
        fills=(),
        events=(),
        next_cursor="42",
        fills_cursor=None,
        events_cursor="42",
        next_fills_cursor=None,
        next_events_cursor="42",
    )
    resp = await client.post(
        "/miniapp/api/trade/events", json={"trade_ref": "ref-1", "cursor": "42"}, headers=_auth()
    )
    assert resp.status == 200
    assert fake_app.calls[0][3]["cursor"] == 42
    assert fake_app.calls[0][3]["events_cursor"] is None
    body = await resp.json()
    assert body["next_cursor"] == "42" and body["events_cursor"] == "42"


async def test_history_page_changed_maps_to_typed_400(client, fake_app):
    fake_app.fail_with = HistoryPageChangedError("trade history changed under this page")
    resp = await client.post(
        "/miniapp/api/trade/events",
        json={"trade_ref": "ref-1", "fills_cursor": "ZmlsbHM"},
        headers=_auth(),
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "history_page_changed"


async def test_split_and_unavailable_map_to_typed_400(client, fake_app):
    fake_app.fail_with = TradeSplitError("trade split into several episodes")
    resp = await client.post(
        "/miniapp/api/trade/events", json={"trade_ref": "old-split"}, headers=_auth()
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "trade_split"
    fake_app.fail_with = TradeUnavailableError("trade is unavailable")
    resp = await client.post(
        "/miniapp/api/trade/events", json={"trade_ref": "nope"}, headers=_auth()
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "trade_unavailable"


async def test_invalid_split_params_rejected_before_application(client, fake_app):
    # The transport rejects malformed types; range checks live in the
    # service (covered at the service level), the fake never sees these.
    for payload in (
        {"trade_ref": "ref-1", "fills_cursor": ""},
        {"trade_ref": "ref-1", "fills_cursor": 7},
        {"trade_ref": "ref-1", "events_cursor": "7x"},
        {"trade_ref": "ref-1", "events_cursor": 2.5},
        {"trade_ref": "ref-1", "fills_limit": "10"},
        {"trade_ref": "ref-1", "events_limit": True},
    ):
        resp = await client.post("/miniapp/api/trade/events", json=payload, headers=_auth())
        assert resp.status == 400
    assert fake_app.calls == []
