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
from decimal import ROUND_CEILING
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
    SyncBasisRepo,
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
    # Fail-fast ambient guard: leftover positions/orders from earlier suite
    # tests must not silently turn Phase 3 into count==0.
    for _ in range(3):
        m_pos = [
            x for x in await app.master_client.position_risk("main", SYMBOL) if x.position_amt != 0
        ]
        c_pos = [
            x
            for x in await app.master_client.position_risk("copyc1", SYMBOL)
            if x.position_amt != 0
        ]
        m_ord = await app.master_client.open_orders("main", symbol=SYMBOL)
        c_ord = await app.master_client.open_orders("copyc1", symbol=SYMBOL)
        if not m_pos and not c_pos and not m_ord and not c_ord:
            break
        await _flatten(app.master_client, "main", hedge_master, "pre-m2")
        await _flatten(app.master_client, "copyc1", hedge_follower, "pre-f2")
    else:
        pytest.fail("ambient residue: main/copyc1 not flat before acceptance")
    # Episode isolation on the shared prod DB: direct exchange flattens above
    # bypass the engine, so a zero_target marker stranded by an earlier suite
    # test would INTENTIONAL_FLAT-skip our fresh entry (proven live). The
    # engine recreates basis rows on the next execution.
    basis_repo = SyncBasisRepo(app.db)
    for _side in ("LONG", "SHORT", "BOTH"):
        await basis_repo.clear("copyc1", SYMBOL, _side)

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
    # Do not poison later suite tests: drop our episode basis markers too.
    with contextlib.suppress(Exception):
        _br = SyncBasisRepo(app.db)
        for _s in ("LONG", "SHORT", "BOTH"):
            await _br.clear("copyc1", SYMBOL, _s)
    # Restore baseline policy: the test calibrates copyc1 risk up for sizing.
    with contextlib.suppress(Exception):
        await SettingsRepo(app.db).upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
            "copyc1",
        )
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
    # Stabilizer (review corrective): follower entry sizing depends on copyc1
    # equity x risk% / stop distance, NOT on master qty. Calibrate risk% from
    # the measured balance so the follower opens >= 3 lot steps: with a
    # single step, the Phase 6 halving floors back to the same step and the
    # strict-decrease assert flakes.
    step_size = app._filters()[SYMBOL].step_size  # noqa: SLF001
    copy_balances = await client.balance("copyc1")
    copy_usdt = next((b for b in copy_balances if b.asset == "USDT"), None)
    live_distance = ref_mark - stop_price
    if copy_usdt is None or copy_usdt.available_balance <= 0 or live_distance <= 0:
        pytest.skip("cannot calibrate follower sizing: no balance or bad distance")
    risk_raw = live_distance * (4 * step_size) * D("1.001") / copy_usdt.available_balance
    if risk_raw > D("0.10"):
        pytest.skip("copyc1 equity too small for stable multi-step sizing")
    risk_pct = (risk_raw * 10000).to_integral_value(rounding=ROUND_CEILING) / 10000
    await SettingsRepo(app.db).upsert_policy(
        RiskPolicy(risk_percent=risk_pct, max_leverage=20, max_entry_deviation_pct=D("1")),
        "copyc1",
    )
    # Test-only skip spy (instance-level, cf. P2.7 place_order spy): the
    # reconciler swallows per-account failures and skips journal nothing,
    # so record skip reasons for the failure dump below.
    skip_reasons: list = []
    coordinator = app.reconciler._coordinator  # noqa: SLF001
    orig_skip = coordinator._skip_record

    async def _spy_skip(key, reason):  # noqa: ANN001, ANN202
        skip_reasons.append((str(key.symbol), str(reason)))
        return await orig_skip(key, reason)

    coordinator._skip_record = _spy_skip  # type: ignore[method-assign]

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

    # Master leg must exist before projecting: fail fast instead of a
    # confusing count==0 later.
    main_pos = [x for x in await client.position_risk("main", SYMBOL) if x.position_amt != 0]
    assert main_pos, "master leg did not open"
    # Phase 2: Feed master state to Projector and reconcile through Coordinator
    # The reconciler swallows per-account exceptions into the report (-1) and
    # legit skips journal nothing: both would surface later as a misleading
    # count==0. Retry boundedly (reconcile is idempotent; production sweeps
    # every 15s anyway), then fail with the report attached.
    entry_report = None
    for _ in range(3):
        await app.projector.rebuild_full(client, "main")
        entry_report = await app.reconcile_follower("copyc1", reason="test_live_open")
        if (entry_report.per_account.get("copyc1") or 0) > 0:
            break
    else:
        lev = app.leverage.confirmed("copyc1", SYMBOL)
        lev_ready = app.leverage.is_ready("copyc1", SYMBOL)
        pytest.fail(f"entry no-cmds: {entry_report} skips={skip_reasons} lev={lev}/{lev_ready}")

    # Phase 3: Query Mini App positions endpoint through production Application
    resp1 = await http_client.post(
        "/miniapp/api/positions", headers=auth_headers, json={"refresh": True}
    )
    assert resp1.status == 200
    data1 = await resp1.json()
    if data1["count"] != 1:
        basis = await app.db.fetchall(
            "SELECT symbol, pos_side, master_ref_abs,"
            + "issued_target, zero_target FROM sync_basis WHERE account_id=?",
            ("copyc1",),
        )
        atrades = await app.db.fetchall(
            "SELECT trade_id, symbol, pos_side, status FROM copy_trades WHERE closed_at IS NULL",
        )
        c_risks = await client.position_risk("copyc1", SYMBOL)
        c_open = await client.open_orders("copyc1", symbol=SYMBOL)
        m_risks = await client.position_risk("main", SYMBOL)
        pytest.fail(
            f"follower leg missing: copyc1 risks={c_risks}"
            + f" orders={c_open} main={m_risks}"
            + f" basis={basis} active_trades={atrades} skips={skip_reasons}",
        )
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
    # Sizing invariant: calibration above must hold, otherwise Phase 6
    # strict-decrease would be meaningless.
    assert D(str(pos_dto["qty"])) >= 3 * step_size, "sizing calibration failed"

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
