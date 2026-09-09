"""Signed Live Acceptance Gate for Stage 3 Part 2 (P2.2 — read-only Activity).

Reviewer verification gate:
1. Master opens real BTCUSDT LONG + Stop on Aster Testnet.
2. Production copy engine (Projector -> Coordinator -> Planner -> Executor)
   reconciles and copies position + Stop to follower copyc1.
3. Production history capture imports the signed fills and projects immutable
   ``POSITION_*`` / ``PROTECTION_*`` user events.
4. Production application/runtime (AppStage2RuntimeAdapter + UserApplicationService)
   serves POST /miniapp/api/activity.
5. The Mini App activity payload is verified against the durable user-event feed:
   the safe DTO contains the expected category / title / status / symbol,
   trade vs system filters split the rows correctly, and no internal
   fields (command_key, account_id, client_order_id, payload, addresses)
   ever reach the wire.
6. Master and follower are flattened cleanly for repeatable re-runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import time
from decimal import Decimal as D
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.app import App
from copytradeaster.application.adapters import AppStage2RuntimeAdapter
from copytradeaster.application.models import RiskUpdateInput
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import (
    PosSide,
    RiskPolicy,
    Side,
)
from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.reader import HistoryReader, Window
from copytradeaster.history.reconcile import HistoryReconciliationError
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    SettingsRepo,
    TelegramUserRepo,
)
from tests.testnet.support.app_live import arm_manual_master_admission

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
TEST_USER_ID = 900000001


def _copy_sqlite(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


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


async def _reset_activity_history(db: Database, account_id: str) -> None:
    """Clear only the copied follower history inside the per-test database."""
    params = (account_id, "testnet")
    await db.execute_write(
        "DELETE FROM user_activity_events WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_episode_allocations WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_episode_resolutions WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_episode_aliases WHERE alias_ref IN "
        "(SELECT public_ref FROM follower_episode_refs WHERE account_id=? AND environment=?) "
        "OR target_ref IN "
        "(SELECT public_ref FROM follower_episode_refs WHERE account_id=? AND environment=?)",
        (*params, *params),
    )
    await db.execute_write(
        "DELETE FROM follower_episodes WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_episode_refs WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM history_projection_boundaries WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM history_capture_state WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM history_scope_state WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM history_sync_state WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM history_order_checks WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_order_observations WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_cash_flows WHERE account_id=? AND environment=?", params
    )
    await db.execute_write(
        "DELETE FROM follower_fills WHERE account_id=? AND environment=?", params
    )
    await db.execute_write("DELETE FROM order_links WHERE account_id=?", (account_id,))
    await db.execute_write("DELETE FROM execution_commands WHERE account_id=?", (account_id,))


async def _capture_until(
    app: App,
    client: AsterClient,
    *,
    start_ms: int,
    wanted: set[str],
    timeout_s: float = 30.0,
) -> None:
    """Import signed follower fills and wait until projection proves wanted events."""
    scope = HistoryScope("copyc1", "testnet")
    capture = HistoryCapture(
        app.history,
        app.episodes,
        events=app.user_events,
        commands=app.commands,
    )
    synchronizer = HistorySynchronizer(HistoryReader(client), app.history)
    deadline = time.monotonic() + timeout_s
    last_kinds: set[str] = set()
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await capture.capture_and_publish(
                scope,
                SYMBOL,
                Window(start_ms, int(time.time() * 1000)),
                snapshots=client,
                synchronizer=synchronizer,
                notify_eligible=True,
            )
            last_error = None
        except HistoryReconciliationError as exc:
            last_error = exc
        rows = await app.user_events.list_owned(scope, limit=100)
        last_kinds = {row.event.kind for row in rows}
        if wanted <= last_kinds:
            return
        await asyncio.sleep(2)
    raise AssertionError(
        f"history capture did not publish {sorted(wanted)}; "
        f"seen={sorted(last_kinds)} last_error={last_error}"
    )


@pytest.fixture
async def live_app_stack(tmp_path):
    base_settings = Settings(miniapp_dev_unsigned=True, miniapp_dev_user_id=TEST_USER_ID)
    if "testnet" not in base_settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {base_settings.rest_base_url!r}")
    if not base_settings.db_path.exists():
        pytest.skip(f"source database required for isolated copy: {base_settings.db_path}")
    db_copy = tmp_path / "live-activity.sqlite3"
    _copy_sqlite(base_settings.db_path, db_copy)
    settings = base_settings.model_copy(
        update={"db_path": db_copy, "log_path": tmp_path / "live-activity.log"}
    )
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()
    app = App(settings, db=db)
    await app.credentials.load()
    if "main" not in app.credentials.account_ids() or "copyc1" not in app.credentials.account_ids():
        await db.close()
        pytest.skip("need main + copyc1 imported in wallet.txt")

    await app.register_follower("copyc1")
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
    await _reset_activity_history(app.db, "copyc1")
    await arm_manual_master_admission(app, app.master_client)

    yield {
        "app": app,
        "application": app_service,
        "client": app.master_client,
        "http_client": test_client,
        "hedge_master": hedge_master,
        "hedge_follower": hedge_follower,
    }

    with contextlib.suppress(Exception):
        await _flatten(app.master_client, "main", hedge_master, "post-m")
    with contextlib.suppress(Exception):
        await _flatten(app.master_client, "copyc1", hedge_follower, "post-f")
    await test_client.close()
    await app.http.aclose()
    await db.close()


@pytest.mark.asyncio
async def test_live_production_activity_acceptance_gate(live_app_stack):
    app: App = live_app_stack["app"]
    application: UserApplicationService = live_app_stack["application"]
    client: AsterClient = live_app_stack["client"]
    http_client: TestClient = live_app_stack["http_client"]
    hedge_master = live_app_stack["hedge_master"]
    auth_headers = {"X-Dev-User-Id": str(TEST_USER_ID), "Content-Type": "application/json"}

    # Phase 0: Empty durable user-event feed (after isolated reset).
    resp0 = await http_client.post(
        "/miniapp/api/activity",
        headers=auth_headers,
        json={"filter": "all", "page": 0},
    )
    assert resp0.status == 200
    data0 = await resp0.json()
    assert data0["filter"] == "all"
    assert data0["page"] == 0
    assert isinstance(data0["items"], list)
    # No forbidden fields in any item.
    for item in data0["items"]:
        for forbidden in (
            "command_key",
            "client_order_id",
            "account_id",
            "payload",
            "address",
            "nonce",
            "private_key",
        ):
            assert forbidden not in item
        # Status is one of the three safe values.
        assert item["status"] in {"completed", "pending", "failed"}
        assert item["category"] in {"trade", "system"}

    # Phase 1: Master opens BTCUSDT LONG + Stop Market on Aster Testnet
    risks_init = await client.position_risk("main", SYMBOL)
    ref_mark = (
        risks_init[0].mark_price if risks_init and risks_init[0].mark_price > 0 else D("60000")
    )
    stop_price = (ref_mark * D("0.95")).quantize(D("0.1"))
    order_qty = D("0.002")

    ts = int(time.time() * 1000)
    capture_start = ts - 5_000

    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=order_qty,
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            new_client_order_id=f"live-act-m-open-{ts}"[:36],
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
            new_client_order_id=f"live-act-m-stop-{ts}"[:36],
        ),
    )

    # Phase 2: Feed master state to Projector and reconcile through Coordinator
    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower("copyc1", reason="test_live_activity_open")
    await _capture_until(
        app,
        client,
        start_ms=capture_start,
        wanted={"POSITION_OPENED", "PROTECTION_SET"},
    )

    # Phase 2b: use the current public application path for a system event.
    # Activity is intentionally no longer a view over execution_commands;
    # RISK_CHANGED is an immutable, user-facing event in the same feed.
    updated_risk = await application.update_risk(
        TEST_USER_ID, RiskUpdateInput(risk_percent=D("0.02"))
    )
    assert updated_risk.risk_percent == D("0.02")

    # Phase 3: Query Mini App activity endpoint through production Application
    resp1 = await http_client.post(
        "/miniapp/api/activity",
        headers=auth_headers,
        json={"filter": "all", "page": 0},
    )
    assert resp1.status == 200
    data1 = await resp1.json()
    items = data1["items"]
    assert items, "expected user events after the open + stop cycle"

    titles = [item["title"] for item in items]
    # Trade timeline: the proven fill and protective order. System timeline:
    # the public risk update from Phase 2b.
    assert any(t.startswith("Открыта BTCUSDT") for t in titles), titles
    assert any(t.startswith("Стоп BTCUSDT установлен") for t in titles), titles
    assert any(t.startswith("Риск на сделку") for t in titles), titles

    # Phase 4: Trade/system filters split cleanly.
    # Both branches are asserted on a NON-EMPTY result: an empty page proves
    # nothing about the predicate, and an empty `system` page in particular is
    # exactly what the post-LIMIT filtering bug produced.
    trade_resp = await http_client.post(
        "/miniapp/api/activity",
        headers=auth_headers,
        json={"filter": "trade", "page": 0},
    )
    assert trade_resp.status == 200
    trade_data = await trade_resp.json()
    assert trade_data["filter"] == "trade"
    assert trade_data["items"], "trade filter must not be empty after the open + stop cycle"
    assert all(item["category"] == "trade" for item in trade_data["items"])
    assert all(item["symbol"] == SYMBOL for item in trade_data["items"])
    assert any(t.startswith("Открыта BTCUSDT") for t in (i["title"] for i in trade_data["items"]))
    assert any(
        t.startswith("Стоп BTCUSDT установлен") for t in (i["title"] for i in trade_data["items"])
    )

    system_resp = await http_client.post(
        "/miniapp/api/activity",
        headers=auth_headers,
        json={"filter": "system", "page": 0},
    )
    assert system_resp.status == 200
    system_data = await system_resp.json()
    assert system_data["filter"] == "system"
    assert system_data["items"], "system filter must return the real RISK_CHANGED row"
    assert all(item["category"] == "system" for item in system_data["items"])
    system_titles = [item["title"] for item in system_data["items"]]
    assert any(t.startswith("Риск на сделку") for t in system_titles), system_titles
    assert all(item["symbol"] is None for item in system_data["items"])
    assert all(
        item["status"] in {"completed", "pending", "failed"} for item in system_data["items"]
    )

    # trade + system must not overlap, and both come from the same event feed.
    trade_titles = [item["title"] for item in trade_data["items"]]
    assert not (set(trade_titles) & set(system_titles)), "categories must partition the journal"

    # Phase 5: Each item maps to a safe DTO surface, no internal codes leak.
    for item in items:
        assert item["status"] in {"completed", "pending", "failed"}
        assert item["remediation"] is None
        for forbidden in (
            "command_key",
            "client_order_id",
            "account_id",
            "payload",
            "address",
            "nonce",
            "private_key",
            "MARKET_OPEN",
            "PLACE_STOP",
            "MARKET_CLOSE",
            "RISK_CHANGED",
        ):
            assert forbidden not in (item["title"] or "")
            assert forbidden not in (item.get("remediation") or "")

    # Phase 6: master closes fully; the engine converges the follower flat and
    # history publishes a POSITION_CLOSED event.
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=order_qty,
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            reduce_only=not hedge_master,
            new_client_order_id=f"live-act-m-close-{ts}"[:36],
        ),
    )
    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower("copyc1", reason="test_live_activity_close")
    await _capture_until(
        app,
        client,
        start_ms=capture_start,
        wanted={"POSITION_CLOSED"},
    )

    resp2 = await http_client.post(
        "/miniapp/api/activity",
        headers=auth_headers,
        json={"filter": "trade", "page": 0},
    )
    data2 = await resp2.json()
    trade_titles = [item["title"] for item in data2["items"]]
    assert any(t.startswith("Закрыта BTCUSDT") for t in trade_titles), trade_titles

    # Phase 7: Cleanup flat.  After flatten the activity DTO stays consistent:
    # the same safe shape, same no-leak guarantees.
    await _flatten(client, "main", hedge_master, "final-m")
    await _flatten(client, "copyc1", live_app_stack["hedge_follower"], "final-f")

    resp3 = await http_client.post(
        "/miniapp/api/activity",
        headers=auth_headers,
        json={"filter": "all", "page": 0},
    )
    data3 = await resp3.json()
    for item in data3["items"]:
        for forbidden in (
            "command_key",
            "client_order_id",
            "account_id",
            "payload",
            "address",
            "nonce",
            "private_key",
        ):
            assert forbidden not in item
        assert item["status"] in {"completed", "pending", "failed"}
        assert item["category"] in {"trade", "system"}
