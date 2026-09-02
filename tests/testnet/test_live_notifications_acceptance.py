"""Signed Live Acceptance Gate for Stage 3 Part 2 (P2.3 — Notifications).

Production-path verification on the real Aster Futures Testnet and the real
Telegram Bot API:

1. The Mini App HTTP boundary (production aiohttp + build_stage2_composition
   application with its control audit) reads and toggles notification
   settings, accepting only a real boolean for ``enabled``.
2. With notifications disabled, a real master order still copies to the
   follower through the production copy engine (trading is unaffected), and
   NO durable user delivery row is enqueued for the owner.
3. After re-enabling through the same production HTTP path, a real
   subsequent trade event creates a durable SQLite outbox row for the owner
   and the production TelegramOutboxWorker delivers it via the real
   Telegram Bot API (row reaches terminal SENT).
4. Cleanup: master and follower are flattened, the original toggle value is
   restored, so re-runs are repeatable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from decimal import Decimal as D

import pytest
from aiogram import Bot
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import (
    PosSide,
    RiskPolicy,
    Side,
)
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.notifications.worker import TelegramOutboxWorker
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import SettingsRepo, TelegramUserRepo

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
# The real production pair: Telegram user 8264892202 owns the Builder
# follower account ``tg-8264892202`` (the same account the P2.2 owner
# acceptance ran on).  Its chat_id is a real private chat that the
# configured bot can deliver to, so outbox delivery is provable end to end.
TEST_USER_ID = 8264892202
FOLLOWER_ACCOUNT = "tg-8264892202"


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


async def _user_outbox_rows(db: Database, after_id: int = 0) -> list[dict]:
    rows = await db.fetchall(
        "SELECT id, telegram_user_id, chat_id, status, kind, text FROM notification_outbox "
        "WHERE telegram_user_id=? AND id>? ORDER BY id",
        (TEST_USER_ID, after_id),
    )
    return [dict(row) for row in rows]


@pytest.fixture
async def live_app_stack():
    settings = Settings(miniapp_dev_unsigned=True, miniapp_dev_user_id=TEST_USER_ID)
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    if settings.telegram_bot_token is None:
        pytest.skip("real Telegram bot token required for outbox delivery (.botkey)")

    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()
    # Production composition: the same durable NotificationRouter that
    # Stage2Process wires between the engine and the Telegram outbox.
    router = NotificationRouter(db, admin_ids=settings.telegram_admin_ids)
    app = App(settings, notifier=router, db=db)
    await app.credentials.load()
    if "main" not in app.credentials.account_ids() or (
        FOLLOWER_ACCOUNT not in app.credentials.account_ids()
    ):
        await db.close()
        pytest.skip(f"need main + {FOLLOWER_ACCOUNT} imported in wallet.txt")

    await app.register_follower(FOLLOWER_ACCOUNT)
    info = await app.master_client.exchange_info(force=True)
    app._filters_cache.update(info.tradable_filters())  # noqa: SLF001
    await app.leverage.refresh_catalog()
    await app.leverage.prepare_account(FOLLOWER_ACCOUNT, [SYMBOL])

    settings_repo = SettingsRepo(app.db)
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.set_flag("ready", "true")
    await settings_repo.set_flag("heartbeat_at", str(int(time.time() * 1000)))
    await settings_repo.upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
        FOLLOWER_ACCOUNT,
    )

    users = TelegramUserRepo(app.db)
    owner_row = await users.by_account(FOLLOWER_ACCOUNT)
    if owner_row is None or int(owner_row["telegram_user_id"]) != TEST_USER_ID:
        await db.close()
        pytest.skip(f"user {TEST_USER_ID} must own {FOLLOWER_ACCOUNT} for delivery routing")
    original_enabled = bool(owner_row["notifications_enabled"])

    # Production application layer (shared use cases + control audit) behind
    # the production Mini App aiohttp transport, dev-unsigned testnet mode.
    composition = build_stage2_composition(app)
    aio_app = create_miniapp_app(settings, composition.application)
    test_client = TestClient(TestServer(aio_app))
    await test_client.start_server()

    # Real Telegram Bot API sender — the production worker with the real bot.
    bot = Bot(settings.telegram_token_value())
    worker = TelegramOutboxWorker(
        bot=bot,
        db=app.db,
        concurrency=1,
        poll_interval=0.2,
        per_chat_interval=0.0,
        max_attempts=3,
    )

    hedge_master = await app.master_client.get_position_mode("main")
    hedge_follower = await app.master_client.get_position_mode(FOLLOWER_ACCOUNT)
    await _flatten(app.master_client, "main", hedge_master, "pre-m")
    await _flatten(app.master_client, FOLLOWER_ACCOUNT, hedge_follower, "pre-f")

    # Journal cleanup keeps the copy engine deterministic for this follower.
    await app.db.execute_write(
        "DELETE FROM execution_commands WHERE account_id=?", (FOLLOWER_ACCOUNT,)
    )
    baseline_leverage = app.leverage.confirmed(FOLLOWER_ACCOUNT, SYMBOL)
    outbox_baseline_row = await app.db.fetchone(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM notification_outbox"
    )
    outbox_baseline_max = int(outbox_baseline_row["max_id"])

    yield {
        "app": app,
        "client": app.master_client,
        "http_client": test_client,
        "worker": worker,
        "users": users,
        "original_enabled": original_enabled,
        "hedge_master": hedge_master,
        "hedge_follower": hedge_follower,
        "outbox_baseline_max": int(outbox_baseline_max),
    }

    # Cleanup: flat accounts and the original toggle for repeatable re-runs.
    await users.set_notifications_enabled(TEST_USER_ID, original_enabled)
    if baseline_leverage is not None:
        with contextlib.suppress(Exception):
            await app.master_client.set_leverage(FOLLOWER_ACCOUNT, SYMBOL, int(baseline_leverage))
    with contextlib.suppress(Exception):
        await _flatten(app.master_client, "main", hedge_master, "post-m")
    with contextlib.suppress(Exception):
        await _flatten(app.master_client, FOLLOWER_ACCOUNT, hedge_follower, "post-f")
    await test_client.close()
    with contextlib.suppress(Exception):
        await bot.session.close()
    await app.http.aclose()
    await db.close()


@pytest.mark.asyncio
async def test_live_production_notifications_acceptance_gate(live_app_stack):
    app: App = live_app_stack["app"]
    client: AsterClient = live_app_stack["client"]
    http_client: TestClient = live_app_stack["http_client"]
    worker: TelegramOutboxWorker = live_app_stack["worker"]
    users: TelegramUserRepo = live_app_stack["users"]
    outbox_baseline_max: int = live_app_stack["outbox_baseline_max"]
    hedge_master = live_app_stack["hedge_master"]
    auth_headers = {"X-Dev-User-Id": str(TEST_USER_ID), "Content-Type": "application/json"}

    async def read_settings() -> dict:
        resp = await http_client.post("/miniapp/api/notifications", headers=auth_headers)
        assert resp.status == 200
        return await resp.json()

    async def set_enabled(enabled: bool) -> dict:
        resp = await http_client.post(
            "/miniapp/api/notifications/enabled",
            headers=auth_headers,
            json={"enabled": enabled},
        )
        assert resp.status == 200, await resp.text()
        return await resp.json()

    # Phase 0: production read path returns the safe DTO surface only.
    initial = await read_settings()
    assert initial["enabled"] is live_app_stack["original_enabled"]
    for forbidden in ("chat_id", "account_id", "payload", "last_error", "event_key", "text"):
        assert forbidden not in initial

    # Phase 1: strict boolean boundary on the real transport.
    bad = await http_client.post(
        "/miniapp/api/notifications/enabled",
        headers=auth_headers,
        json={"enabled": "false"},
    )
    assert bad.status == 400
    assert (await bad.json())["error"] == "invalid_input"

    # Phase 2: disable through the production application path (control audit).
    disabled = await set_enabled(False)
    assert disabled["enabled"] is False
    row = await users.by_account(FOLLOWER_ACCOUNT)
    assert bool(row["notifications_enabled"]) is False

    # Phase 3: real master open + Stop; production copy engine copies it while
    # notifications are disabled, and no user delivery row is enqueued.
    risks_init = await client.position_risk("main", SYMBOL)
    ref_mark = (
        risks_init[0].mark_price if risks_init and risks_init[0].mark_price > 0 else D("60000")
    )
    stop_price = (ref_mark * D("0.95")).quantize(D("0.1"))
    ts = int(time.time() * 1000)
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=D("0.002"),
            position_side=PosSide.LONG if hedge_master else PosSide.BOTH,
            new_client_order_id=f"live-notif-m-open-{ts}"[:36],
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
            new_client_order_id=f"live-notif-m-stop-{ts}"[:36],
        ),
    )
    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower(FOLLOWER_ACCOUNT, reason="test_live_notifications_disabled")

    follower_risks = await client.position_risk(FOLLOWER_ACCOUNT, SYMBOL)
    copied = [p for p in follower_risks if p.position_amt != 0]
    assert copied, "copy engine must trade while notifications are disabled"
    new_rows_disabled = await _user_outbox_rows(app.db, outbox_baseline_max)
    assert new_rows_disabled == [], "disabled notifications must not enqueue user delivery rows"

    # Phase 4: re-enable through the same production HTTP path.
    enabled = await set_enabled(True)
    assert enabled["enabled"] is True
    row = await users.by_account(FOLLOWER_ACCOUNT)
    assert bool(row["notifications_enabled"]) is True

    # Phase 5: real subsequent trade event -> durable outbox row -> real
    # Telegram Bot API delivery by the production worker (terminal SENT).
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
            new_client_order_id=f"live-notif-m-close-{ts}"[:36],
        ),
    )
    await app.projector.rebuild_full(client, "main")
    await app.reconcile_follower(FOLLOWER_ACCOUNT, reason="test_live_notifications_enabled")

    deadline = time.monotonic() + 15
    delivered: dict | None = None
    while time.monotonic() < deadline:
        pending_rows = [
            r
            for r in await _user_outbox_rows(app.db, outbox_baseline_max)
            if r["status"] in {"PENDING", "SENDING", "SENT"}
        ]
        if pending_rows:
            delivered = pending_rows[0]
            break
        await asyncio.sleep(0.3)
    assert delivered is not None, "enabled notifications must enqueue a durable outbox row"

    # Drive the real worker: process_once performs a real Bot API send.
    for _ in range(30):
        await worker.process_once()
        fresh = await _user_outbox_rows(app.db, outbox_baseline_max)
        current = next(r for r in fresh if r["id"] == delivered["id"])
        if current["status"] == "SENT":
            break
        assert current["status"] in {"PENDING", "SENDING"}, current
        await asyncio.sleep(0.5)
    final_rows = await _user_outbox_rows(app.db, outbox_baseline_max)
    final = next(r for r in final_rows if r["id"] == delivered["id"])
    assert final["status"] == "SENT", f"real Bot API delivery failed: {final}"

    # Phase 6: the settings read reflects the confirmed state and delivery.
    after = await read_settings()
    assert after["enabled"] is True
    assert after["last_delivery_at"], "last delivery timestamp must be visible"

    # Cleanup: flatten both accounts; the toggle is restored by the fixture.
    await _flatten(client, "main", hedge_master, "final-m")
    await _flatten(client, FOLLOWER_ACCOUNT, live_app_stack["hedge_follower"], "final-f")
