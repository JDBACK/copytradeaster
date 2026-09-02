"""Signed Live Acceptance Gate for Stage 3 Part 2 (P2.4 — Risk on Stop).

Production-path verification on the real Aster Futures Testnet:

1. Point-in-time ``sqlite3.backup`` copy of the working DB; the live test
   boots App + control-audited application + production aiohttp transport on
   the copy only.  The working DB is fingerprinted before/after.
2. ``POST /miniapp/api/risk`` reads the authoritative backend-owned policy;
   ``POST /miniapp/api/risk/percent`` sets risk A (1%) then risk B (3%).
3. For each risk value a real master BTCUSDT LONG + STOP_MARKET is opened,
   the production copy engine (Projector -> reconcile_follower -> journal ->
   REAL follower ``tg-8264892202``) copies it, and the signed follower
   ``positionRisk`` qty is read back.
4. Each realized qty is checked against its own Decimal sizing expectation:
   realized_loss = qty * stop_distance * (1 + fee_buffer) must sit within one
   lot step of the configured risk budget (existing RiskEngine semantics,
   precision/lot rounding and live balance accounted for); the B/A realized
   ratio must track the 3x risk change.
5. A forged HTTP body with ``max_leverage``/``max_entry_deviation_pct`` is
   rejected with 400 ``invalid_input``.
6. Best-effort cleanup flattens both accounts; the working DB is proven
   unchanged.

No FakeAster/MockTransport is used anywhere in this scenario.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sqlite3
import time
from decimal import Decimal as D
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, RiskPolicy, Side
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.risk.engine import entry_quantity
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import SettingsRepo, TelegramUserRepo

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
# Production pair: Telegram user 8264892202 owns follower ``tg-8264892202``
# (the same real pair the P2.3 notifications gate proved delivery on).
TEST_USER_ID = 8264892202
FOLLOWER_ACCOUNT = "tg-8264892202"
RISK_A = D("0.01")
RISK_B = D("0.03")


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


def _copy_sqlite(src: Path, dst: Path) -> None:
    """Consistent online copy; safe while the source is in WAL use."""
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _fingerprint_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        tables = [
            "copy_settings",
            "telegram_users",
            "accounts",
            "execution_commands",
            "notification_outbox",
        ]
        digest = hashlib.sha256()
        for table in tables:
            try:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
            except Exception:  # noqa: BLE001
                continue
            digest.update(repr(sorted(map(repr, rows))).encode())
        return digest.hexdigest()
    finally:
        conn.close()


async def _follower_qty(client: AsterClient) -> D:
    risks = await client.position_risk(FOLLOWER_ACCOUNT, SYMBOL)
    total = sum((abs(p.position_amt) for p in risks), D(0))
    return total


async def _available_usdt(client: AsterClient, account_id: str) -> D:
    for b in await client.balance(account_id):
        if b.asset == "USDT":
            return D(b.available_balance)
    return D(0)


@pytest.fixture
async def live_app_stack(tmp_path):
    base_settings = Settings(miniapp_dev_unsigned=True, miniapp_dev_user_id=TEST_USER_ID)
    if "testnet" not in base_settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {base_settings.rest_base_url!r}")
    if not base_settings.db_path.exists():
        pytest.skip(f"source database required for the isolated copy: {base_settings.db_path}")

    prod_fingerprint = _fingerprint_db(base_settings.db_path)
    prod_settings_row = (
        sqlite3.connect(base_settings.db_path)
        .execute("SELECT account_id, risk_percent FROM copy_settings ORDER BY account_id")
        .fetchall()
    )

    # Storage isolation: point-in-time copy; every mutation lands there.
    db_copy = tmp_path / "live-risk.sqlite3"
    _copy_sqlite(base_settings.db_path, db_copy)
    settings = base_settings.model_copy(
        update={"db_path": db_copy, "log_path": tmp_path / "live-risk.log"}
    )

    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()
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

    users = TelegramUserRepo(app.db)
    owner_row = await users.by_account(FOLLOWER_ACCOUNT)
    if owner_row is None or int(owner_row["telegram_user_id"]) != TEST_USER_ID:
        await db.close()
        pytest.skip(f"user {TEST_USER_ID} must own {FOLLOWER_ACCOUNT}")

    composition = build_stage2_composition(app)
    aio_app = create_miniapp_app(settings, composition.application)
    test_client = TestClient(TestServer(aio_app))
    await test_client.start_server()

    hedge_master = await app.master_client.get_position_mode("main")
    hedge_follower = await app.master_client.get_position_mode(FOLLOWER_ACCOUNT)
    await _flatten(app.master_client, "main", hedge_master, "pre-m")
    await _flatten(app.master_client, FOLLOWER_ACCOUNT, hedge_follower, "pre-f")
    await app.db.execute_write(
        "DELETE FROM execution_commands WHERE account_id=?", (FOLLOWER_ACCOUNT,)
    )

    yield {
        "app": app,
        "client": app.master_client,
        "http_client": test_client,
        "hedge_master": hedge_master,
        "hedge_follower": hedge_follower,
        "prod_fingerprint": prod_fingerprint,
        "prod_settings_row": prod_settings_row,
        "base_settings": base_settings,
    }

    with contextlib.suppress(Exception):
        await _flatten(app.master_client, "main", hedge_master, "post-m")
    with contextlib.suppress(Exception):
        await _flatten(app.master_client, FOLLOWER_ACCOUNT, hedge_follower, "post-f")
    await test_client.close()
    await app.http.aclose()
    await db.close()


@pytest.mark.asyncio
async def test_live_production_risk_acceptance_gate(live_app_stack):
    app: App = live_app_stack["app"]
    client: AsterClient = live_app_stack["client"]
    http_client: TestClient = live_app_stack["http_client"]
    hedge_master = live_app_stack["hedge_master"]
    auth_headers = {"X-Dev-User-Id": str(TEST_USER_ID), "Content-Type": "application/json"}

    async def read_risk() -> dict:
        resp = await http_client.post("/miniapp/api/risk", headers=auth_headers, json={})
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def set_risk(value: str) -> dict:
        resp = await http_client.post(
            "/miniapp/api/risk/percent",
            headers=auth_headers,
            json={"risk_percent": value},
        )
        assert resp.status == 200, await resp.text()
        return await resp.json()

    # Phase 0: authoritative backend-owned policy over production HTTP.
    policy = await read_risk()
    assert policy == {
        "risk_percent": policy["risk_percent"],
        "min": "0.005",
        "max": "0.05",
        "step": "0.005",
        "presets": ["0.01", "0.02", "0.03", "0.05"],
        "risk_base": "available_balance",
    }
    for forbidden in ("max_leverage", "max_entry_deviation_pct", "account_id", "enabled"):
        assert forbidden not in policy

    filters = app._filters_cache.get(SYMBOL)  # noqa: SLF001
    assert filters is not None, "exchange filters required for sizing math"
    fee_buffer = app.cfg.fee_buffer_pct

    async def run_leg(risk: D, tag: str) -> dict:
        saved = await set_risk(str(risk))
        assert D(saved["risk_percent"]) == risk
        # Real master LONG + STOP_MARKET with a known stop distance.
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
                new_client_order_id=f"live-risk-{tag}-o-{ts}"[:36],
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
                new_client_order_id=f"live-risk-{tag}-s-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(client, "main")
        equity_before = await _available_usdt(client, FOLLOWER_ACCOUNT)
        await app.reconcile_follower(FOLLOWER_ACCOUNT, reason=f"test_live_risk_{tag}")
        # Production copy settles on the exchange; poll the signed state.
        actual = D(0)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            actual = await _follower_qty(client)
            if actual > 0:
                break
            await asyncio.sleep(2)
        assert actual > 0, f"copy engine must open the follower leg {tag}"
        # Actual fill/stop read back from the signed exchange state.
        risks = await client.position_risk(FOLLOWER_ACCOUNT, SYMBOL)
        live = [p for p in risks if p.position_amt != 0][0]
        entry = abs(D(live.entry_price)) if D(live.entry_price) > 0 else ref_mark
        distance = abs(entry - stop_price)
        assert distance > 0
        equity = await _available_usdt(client, FOLLOWER_ACCOUNT)
        equity = equity_before if equity_before > 0 else equity
        expected_decision = entry_quantity(
            RiskPolicy(
                risk_percent=risk,
                max_leverage=20,
                max_entry_deviation_pct=D("1"),
            ),
            equity,
            entry,
            stop_price,
            filters,
            fee_buffer,
        )
        print(
            f"\nleg={tag} risk={risk} equity={equity} entry={entry} "
            f"stop={stop_price} actual={actual} expected={expected_decision.qty}"
        )
        return {
            "risk": risk,
            "equity": equity,
            "entry": entry,
            "stop": stop_price,
            "distance": distance,
            "actual": actual,
            "expected": expected_decision.qty,
        }

    # Phase 1: risk A through the real Mini App endpoint + real copy.
    leg_a = await run_leg(RISK_A, "a")
    assert leg_a["expected"] is not None, "sizing must admit leg A"
    assert leg_a["actual"] == leg_a["expected"], (
        f"leg A must match Decimal sizing: actual={leg_a['actual']} expected={leg_a['expected']}"
    )
    await _flatten(client, "main", hedge_master, "mid-m")
    await _flatten(client, FOLLOWER_ACCOUNT, live_app_stack["hedge_follower"], "mid-f")

    # Phase 2: risk B through the same production path + equivalent trade.
    leg_b = await run_leg(RISK_B, "b")
    assert leg_b["expected"] is not None, "sizing must admit leg B"
    assert leg_b["actual"] == leg_b["expected"], (
        f"leg B must match Decimal sizing: actual={leg_b['actual']} expected={leg_b['expected']}"
    )

    # Phase 3: each leg's realized stop-loss tracks its own configured risk
    # within one lot step (precision/lot rounding accounted for).
    for leg in (leg_a, leg_b):
        realized = leg["actual"] * leg["distance"] * (D(1) + fee_buffer) / leg["equity"]
        step_loss = filters.step_size * leg["distance"] * (D(1) + fee_buffer) / leg["equity"]
        assert abs(realized - leg["risk"]) <= step_loss + D("0.000000001"), (
            f"realized stop-loss {realized} must track configured {leg['risk']}"
        )
    # The follower size follows the risk change (nominal 3x for 1% -> 3%).
    ratio = leg_b["actual"] / leg_a["actual"]
    assert D("2.0") <= ratio <= D("4.0"), f"risk change must move size, ratio={ratio}"

    # Phase 4: forged legacy fields are rejected on the live transport.
    forged = await http_client.post(
        "/miniapp/api/risk/percent",
        headers=auth_headers,
        json={"risk_percent": "0.02", "max_leverage": 50},
    )
    assert forged.status == 400
    assert (await forged.json())["error"] == "invalid_input"
    forged2 = await http_client.post(
        "/miniapp/api/risk/percent",
        headers=auth_headers,
        json={"risk_percent": "0.02", "max_entry_deviation_pct": "0.5"},
    )
    assert forged2.status == 400

    # Phase 5: cleanup + working-DB invariance.
    await _flatten(client, "main", hedge_master, "final-m")
    await _flatten(client, FOLLOWER_ACCOUNT, live_app_stack["hedge_follower"], "final-f")
    base_settings: Settings = live_app_stack["base_settings"]
    assert _fingerprint_db(base_settings.db_path) == live_app_stack["prod_fingerprint"]
    current_settings = (
        sqlite3.connect(base_settings.db_path)
        .execute("SELECT account_id, risk_percent FROM copy_settings ORDER BY account_id")
        .fetchall()
    )
    assert [tuple(r) for r in current_settings] == [
        tuple(r) for r in live_app_stack["prod_settings_row"]
    ]
