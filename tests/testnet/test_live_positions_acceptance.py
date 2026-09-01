"""Signed Live Acceptance Gate for Stage 3 Part 2 (P2.1 — read-only Positions).

Reviewer verification gate:
1. Master opens real BTCUSDT LONG + Stop on Aster Testnet.
2. Production copy engine (Projector -> Coordinator -> Planner -> Executor -> journal)
   reconciles and copies position + Stop to follower copyc1.
3. Production application/runtime (AppStage2RuntimeAdapter + UserApplicationService)
   serves POST /miniapp/api/positions.
4. PositionSummaryDTO is verified against signed follower positionRisk and openOrders.
5. Master moves Stop and places TP; engine copies and positions DTO reflects updated levels.
6. Master does partial close; engine copies and positions DTO reflects decreased qty.
7. Stop is cancelled on follower; positions DTO flags MISSING_STOP warning and
   risk_to_stop_usdt=None.
8. Master and follower are flattened cleanly for complete idempotency.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from decimal import Decimal as D

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.app import App
from copytradeaster.application.adapters import AppStage2RuntimeAdapter
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, RiskPolicy, Side
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.storage.repositories import (
    SettingsRepo,
    TelegramUserRepo,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
TEST_USER_ID = 900000001


def _unknown_outcome(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, AsterError):
        return False
    if exc.code in {-1000, -1006, -1007} or exc.http_status in {503, 504}:
        return True
    message = exc.msg.lower()
    return exc.code == 0 and ("timeout" in message or "timed out" in message)


async def _place(client: AsterClient, account_id: str, req: PlaceOrderRequest):
    for attempt in range(3):
        try:
            return await client.place_order(account_id, req)
        except Exception as exc:  # noqa: BLE001
            if not _unknown_outcome(exc) or attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
            found = await client.query_order(
                account_id, req.symbol, orig_client_order_id=req.new_client_order_id
            )
            if found is not None:
                return found
    raise RuntimeError("unreachable")


async def _flatten(client: AsterClient, account_id: str, hedge: bool, prefix: str) -> None:
    ts = int(time.time() * 1000)
    for order in await client.open_orders(account_id, symbol=SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order(
                account_id, SYMBOL, orig_client_order_id=order.client_order_id
            )
    for index, p in enumerate(await client.position_risk(account_id, SYMBOL)):
        if p.position_amt == 0:
            continue
        with contextlib.suppress(Exception):
            await _place(
                client,
                account_id,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL if p.position_amt > 0 else Side.BUY,
                    type="MARKET",
                    quantity=abs(p.position_amt),
                    reduce_only=not hedge,
                    position_side=p.position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"{prefix}-{ts}-{index}"[:36],
                ),
            )


@pytest.fixture
async def live_app_stack():
    settings = Settings(miniapp_dev_unsigned=True, miniapp_dev_user_id=TEST_USER_ID)
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")

    app = App(settings)
    await app.db.connect()
    await app.db.migrate()
    await app.credentials.load()
    if "main" not in app.credentials.account_ids() or "copyc1" not in app.credentials.account_ids():
        await app.db.close()
        pytest.skip("need main + copyc1 imported in wallet.txt")

    # Register copyc1 in FollowerRegistry
    await app.register_follower("copyc1")

    # Refresh filters & candidate leverage catalog
    info = await app.master_client.exchange_info(force=True)
    app._filters_cache.update(info.tradable_filters())  # noqa: SLF001
    await app.leverage.refresh_catalog()
    await app.leverage.prepare_account("copyc1", [SYMBOL])

    settings_repo = SettingsRepo(app.db)
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.set_flag("ready", "true")
    await settings_repo.set_flag("heartbeat_at", str(int(time.time() * 1000)))
    await settings_repo.upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
        "copyc1",
    )

    # Set up telegram user
    tg_users = TelegramUserRepo(app.db)
    await tg_users.upsert(
        telegram_user_id=TEST_USER_ID,
        chat_id=TEST_USER_ID,
        account_id="copyc1",
        role="ADMIN",
        access_enabled=True,
    )

    runtime_adapter = AppStage2RuntimeAdapter(app)
    app_service = UserApplicationService(
        settings=settings_repo,
        runtime=runtime_adapter,
        admin_ids=(TEST_USER_ID,),
    )

    aio_app = create_miniapp_app(settings, app_service)
    test_client = TestClient(TestServer(aio_app))
    await test_client.start_server()

    hedge_master = await app.master_client.get_position_mode("main")
    hedge_follower = await app.master_client.get_position_mode("copyc1")
    await _flatten(app.master_client, "main", hedge_master, "pre-m")
    await _flatten(app.master_client, "copyc1", hedge_follower, "pre-f")

    yield {
        "app": app,
        "client": app.master_client,
        "http_client": test_client,
        "hedge_master": hedge_master,
        "hedge_follower": hedge_follower,
    }

    # Post cleanup
    with contextlib.suppress(Exception):
        await _flatten(app.master_client, "main", hedge_master, "post-m")
    with contextlib.suppress(Exception):
        await _flatten(app.master_client, "copyc1", hedge_follower, "post-f")
    await test_client.close()
    await app.http.aclose()
    await app.db.close()


@pytest.mark.asyncio
async def test_live_production_positions_acceptance_gate(live_app_stack):
    app: App = live_app_stack["app"]
    client: AsterClient = live_app_stack["client"]
    http_client: TestClient = live_app_stack["http_client"]
    hedge_master = live_app_stack["hedge_master"]
    auth_headers = {"X-Dev-User-Id": str(TEST_USER_ID), "Content-Type": "application/json"}

    # Phase 0: Initial verification - empty positions
    resp0 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    assert resp0.status == 200
    data0 = await resp0.json()
    assert data0["count"] == 0
    assert len(data0["positions"]) == 0

    # Phase 1: Master opens BTCUSDT LONG + Stop Market on Aster Testnet
    risks_init = await client.position_risk("main", SYMBOL)
    ref_mark = (
        risks_init[0].mark_price if risks_init and risks_init[0].mark_price > 0 else D("60000")
    )
    stop_price = (ref_mark * D("0.95")).quantize(D("0.1"))
    order_qty = D("0.002")

    ts = int(time.time() * 1000)
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=order_qty,
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            new_client_order_id=f"live-m-open-{ts}"[:36],
        ),
    )
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            stop_price=stop_price,
            close_position=True,
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            new_client_order_id=f"live-m-stop-{ts}"[:36],
        ),
    )

    # Phase 2: Feed master state to Projector and reconcile through Coordinator
    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower("copyc1", reason="test_live_open")

    # Phase 3: Query Mini App positions endpoint through production Application
    resp1 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    assert resp1.status == 200
    data1 = await resp1.json()
    assert data1["count"] == 1
    assert data1["is_stale"] is False
    assert data1["snapshot_captured_at"] is not None

    pos_dto = data1["positions"][0]
    assert pos_dto["symbol"] == SYMBOL
    assert pos_dto["side"] == "LONG"
    assert D(str(pos_dto["qty"])) > 0
    assert pos_dto["protection_state"] == "protected"
    assert pos_dto["stop_price"] is not None
    assert D(str(pos_dto["stop_price"])) == stop_price
    assert pos_dto["risk_to_stop_usdt"] is not None
    assert D(str(pos_dto["risk_to_stop_usdt"])) > 0

    # Phase 4: Compare DTO against signed follower positionRisk and openOrders
    risks = await client.position_risk("copyc1", SYMBOL)
    matching_risk = next(r for r in risks if r.position_amt != 0)
    assert abs(matching_risk.position_amt) == D(str(pos_dto["qty"]))

    open_orders = await client.open_orders("copyc1", symbol=SYMBOL)
    stop_order = next(o for o in open_orders if o.type == "STOP_MARKET")
    assert stop_order.stop_price == stop_price

    # Phase 5: Master replaces Stop and adds TP -> Engine copies to follower
    new_stop_price = (ref_mark * D("0.96")).quantize(D("0.1"))
    new_tp_price = (ref_mark * D("1.05")).quantize(D("0.1"))

    for o in await client.open_orders("main", symbol=SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=o.client_order_id)

    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            stop_price=new_stop_price,
            close_position=True,
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            new_client_order_id=f"live-m-stop2-{ts}"[:36],
        ),
    )
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            stop_price=new_tp_price,
            close_position=True,
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            new_client_order_id=f"live-m-tp2-{ts}"[:36],
        ),
    )

    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower("copyc1", reason="test_live_stop_move")

    resp2 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    data2 = await resp2.json()
    pos_dto2 = data2["positions"][0]
    assert D(str(pos_dto2["stop_price"])) == new_stop_price
    assert pos_dto2["take_profit_price"] is not None
    assert D(str(pos_dto2["take_profit_price"])) == new_tp_price
    assert pos_dto2["protection_state"] == "protected"

    # Phase 6: Master does partial close -> Engine scales down follower
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=D("0.001"),
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            reduce_only=not hedge_master,
            new_client_order_id=f"live-m-partial-{ts}"[:36],
        ),
    )
    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower("copyc1", reason="test_live_partial")

    resp3 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    data3 = await resp3.json()
    pos_dto3 = data3["positions"][0]
    assert D(str(pos_dto3["qty"])) < D(str(pos_dto["qty"]))

    # Phase 7: Cancel Stop order on follower -> Verify protection warning in API
    for o in await client.open_orders("copyc1", symbol=SYMBOL):
        if o.type == "STOP_MARKET":
            await client.cancel_order("copyc1", SYMBOL, orig_client_order_id=o.client_order_id)

    resp4 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    data4 = await resp4.json()
    pos_dto4 = data4["positions"][0]
    assert pos_dto4["protection_state"] == "missing_stop"
    assert pos_dto4["risk_to_stop_usdt"] is None
    assert pos_dto4["stop_price"] is None

    # Phase 8: Full clean flat audit
    await _flatten(client, "main", hedge_master, "final-m")
    await _flatten(client, "copyc1", live_app_stack["hedge_follower"], "final-f")

    resp5 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    data5 = await resp5.json()
    assert data5["count"] == 0
    assert len(data5["positions"]) == 0
