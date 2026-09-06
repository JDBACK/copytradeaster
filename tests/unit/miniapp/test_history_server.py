"""Transport tests for the S1.4 history Mini App endpoints."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import (
    ActivityFeedPage,
    EpisodeSource,
    HistoryQuality,
    PositionSide,
    TradeDetailDTO,
    TradeEventItem,
    TradeEventsPage,
    TradeListItem,
    TradeListPage,
    TradeState,
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


def _item() -> TradeListItem:
    return TradeListItem(
        trade_ref="ref-1",
        symbol="BTCUSDT",
        quantity_asset="BTC",
        side=PositionSide.LONG,
        state=TradeState.CLOSED,
        source=EpisodeSource.COPY,
        opened_at=AT,
        closed_at=AT,
        executed_qty=Decimal("2"),
        realized_pnl_gross_usdt=Decimal("60"),
        net_pnl_usdt=None,
        quality=HistoryQuality.COMPLETE,
    )


@dataclass
class FakeHistoryApplication:
    calls: list = field(default_factory=list)
    fail_with: Exception | None = None

    async def trades(self, user_id, **kwargs):
        self.calls.append(("trades", user_id, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        return TradeListPage(items=(_item(),), next_cursor=None, as_of=AT)

    async def trade(self, user_id, trade_ref):
        self.calls.append(("trade", user_id, trade_ref))
        if self.fail_with is not None:
            raise self.fail_with
        return TradeDetailDTO(
            trade=_item(),
            entry_qty=Decimal("2"),
            exit_qty=Decimal("2"),
            remaining_qty=Decimal("0"),
            average_entry=Decimal("100"),
            average_exit=Decimal("140"),
            duration_ms=3600000,
            revision=1,
            position_side="BOTH",
        )

    async def trade_events(self, user_id, trade_ref, **kwargs):
        self.calls.append(("trade_events", user_id, trade_ref, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        return TradeEventsPage(
            fills=(),
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
        )

    async def activity_feed(self, user_id, **kwargs):
        self.calls.append(("activity_feed", user_id, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        return ActivityFeedPage(items=(), next_cursor=None, high_water=0, pending=())


def _settings() -> Settings:
    return Settings(encryption_key="00" * 32, telegram_bot_token=BOT_TOKEN, miniapp_enabled=True)


@pytest.fixture
def fake_app() -> FakeHistoryApplication:
    return FakeHistoryApplication()


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


async def test_history_routes_require_auth(client):
    for path in ("/miniapp/api/trades", "/miniapp/api/trade", "/miniapp/api/trade/events"):
        resp = await client.post(path, json={})
        assert resp.status == 401
    resp = await client.post("/miniapp/api/activity/feed", json={})
    assert resp.status == 401


async def test_trades_passes_filters_and_serializes_money(client, fake_app):
    resp = await client.post(
        "/miniapp/api/trades",
        json={"period": "30d", "limit": 20, "symbol": "BTCUSDT"},
        headers=_auth(),
    )
    assert resp.status == 200
    assert fake_app.calls[0][2] == {
        "period": "30d",
        "cursor": None,
        "limit": 20,
        "symbol": "BTCUSDT",
    }
    body = await resp.json()
    (item,) = body["items"]
    assert item["executed_qty"] == "2" and item["realized_pnl_gross_usdt"] == "60"
    assert item["net_pnl_usdt"] is None and item["side"] == "LONG"
    assert item["opened_at"] == "2026-09-06T12:00:00+00:00"


@pytest.mark.parametrize(
    "payload",
    [{"period": "1y"}, {"cursor": 5}, {"limit": "20"}, {"symbol": ""}],
)
async def test_trades_rejects_invalid_bodies(client, fake_app, payload):
    resp = await client.post("/miniapp/api/trades", json=payload, headers=_auth())
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_input"
    assert fake_app.calls == []


async def test_trade_requires_ref_and_maps_unavailable(client, fake_app):
    resp = await client.post("/miniapp/api/trade", json={}, headers=_auth())
    assert resp.status == 400
    fake_app.fail_with = TradeUnavailableError("trade is unavailable")
    resp = await client.post("/miniapp/api/trade", json={"trade_ref": "x"}, headers=_auth())
    assert resp.status == 400
    assert (await resp.json())["error"] == "trade_unavailable"


async def test_trade_events_validates_cursor_and_limit(client, fake_app):
    resp = await client.post(
        "/miniapp/api/trade/events", json={"trade_ref": "ref-1"}, headers=_auth()
    )
    assert resp.status == 200
    assert fake_app.calls[0][3] == {
        "cursor": 0,
        "limit": None,
        "fills_cursor": None,
        "events_cursor": None,
        "fills_limit": None,
        "events_limit": None,
    }
    resp = await client.post(
        "/miniapp/api/trade/events", json={"trade_ref": "ref-1", "cursor": "7"}, headers=_auth()
    )
    assert resp.status == 200
    assert fake_app.calls[-1][3]["cursor"] == 7
    resp = await client.post(
        "/miniapp/api/trade/events",
        json={"trade_ref": "ref-1", "fills_cursor": "abc", "events_cursor": "9"},
        headers=_auth(),
    )
    assert resp.status == 200
    assert fake_app.calls[-1][3]["fills_cursor"] == "abc"
    assert fake_app.calls[-1][3]["events_cursor"] == 9
    resp = await client.post(
        "/miniapp/api/trade/events", json={"trade_ref": "ref-1", "cursor": "7x"}, headers=_auth()
    )
    assert resp.status == 400
    resp = await client.post("/miniapp/api/trade/events", json={}, headers=_auth())
    assert resp.status == 400


async def test_activity_feed_validates_filter_and_cursor(client, fake_app):
    resp = await client.post(
        "/miniapp/api/activity/feed", json={"filter": "trade"}, headers=_auth()
    )
    assert resp.status == 200
    assert fake_app.calls[0][2] == {"filter": "trade", "cursor": 0, "limit": None}
    resp = await client.post(
        "/miniapp/api/activity/feed", json={"filter": "bogus"}, headers=_auth()
    )
    assert resp.status == 400
    assert fake_app.calls[-1][2]["filter"] == "trade"


async def test_risk_preview_requires_auth_and_serializes_steps(client, fake_app):
    from copytradeaster.application.models import (
        Confirmation,
        ResumeContextDTO,
        RiskPreviewDTO,
        RiskPreviewStep,
        SnapshotFreshness,
    )

    resp = await client.post("/miniapp/api/risk/preview", json={})
    assert resp.status == 401

    async def fake_preview(user_id):
        assert user_id == 777
        return RiskPreviewDTO(
            available_usdt=Decimal("1240"),
            steps=(RiskPreviewStep(Decimal("0.02"), Decimal("24.80")),),
            snapshot_captured_at=AT,
            freshness=SnapshotFreshness.FRESH,
        )

    async def fake_begin(user_id, action):
        assert user_id == 777
        return Confirmation(
            token="tok",
            action=action,
            account_id="h1",
            expires_at=AT,
            resume_context=ResumeContextDTO(
                master_in_position=True,
                snapshot_freshness=SnapshotFreshness.FRESH,
                permission_ok=None,
            ),
        )

    fake_app.risk_preview = fake_preview  # type: ignore[method-assign]
    fake_app.begin_confirmation = fake_begin  # type: ignore[method-assign]

    resp = await client.post("/miniapp/api/risk/preview", json={}, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert body["available_usdt"] == "1240" and body["freshness"] == "fresh"
    (step,) = body["steps"]
    assert step == {"risk_percent": "0.02", "budget_usdt": "24.80"}

    resp = await client.post(
        "/miniapp/api/confirmation/begin", json={"action": "resume"}, headers=_auth()
    )
    assert resp.status == 200
    begun = await resp.json()
    assert begun["resume_context"] == {
        "master_in_position": True,
        "snapshot_freshness": "fresh",
        "permission_ok": None,
    }
