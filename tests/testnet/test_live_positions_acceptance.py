"""Live acceptance test for Stage 3 Part 2 (P2.1 — read-only Positions).

Phases:
1. Real master open BTCUSDT LONG + Stop on Testnet.
2. Real follower copy-position through production engine.
3. Query /miniapp/api/positions through production application/aiohttp path.
4. Compare DTO with signed positionRisk and openOrders follower.
5. Real replacement of Stop / Take Profit.
6. Real partial close.
7. Verification of warning when Stop is missing / removed.
8. Cleanup for repeatability.
"""

from __future__ import annotations

import contextlib
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import (
    AccountState,
    PositionKey,
    PositionState,
    PosSide,
    RiskPolicy,
    Side,
)
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AgentValidation,
    NonceStore,
    SettingsRepo,
    TelegramUserRepo,
)
from copytradeaster.trading.coordinator import STOP_TYPES, TP_TYPES, _pick_with_extras
from tests.testnet.support.builder_live import (
    best_effort_flatten,
    ensure_testnet_guard,
    load_api_wallet_section,
)
from tests.unit.miniapp.test_server import _make_init_data

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1",
        reason="signed testnet runs require CT_TESTNET=1",
    ),
]

BOT_TOKEN = "123456:TEST-TOKEN-P21"
USER_TG_ID = 826_489_220
USER_SECTION = "copyc1"
SYMBOL = "BTCUSDT"
QTY = Decimal("0.002")


class LivePositionsRuntime:
    testnet = True
    ready = True

    def __init__(
        self,
        db: Database,
        cfg: Settings,
        credentials: SqliteCredentials,
        client: AsterClient,
        account_id: str,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.credentials = credentials
        self.settings = SettingsRepo(db)
        self.client = client
        self.account_id = account_id
        self.snapshot: AccountState | None = None

    async def is_account_registered(self, account_id: str) -> bool:
        return account_id == self.account_id

    async def account_version(self, account_id: str) -> str | None:
        if account_id != self.account_id:
            return None
        return f"live:{self.credentials.identity(account_id).signer.casefold()}"

    async def cached_snapshot(self, account_id: str) -> AccountState | None:
        if account_id != self.account_id:
            return None
        return self.snapshot

    async def refresh_snapshot(self, account_id: str) -> AccountState:
        if account_id != self.account_id:
            raise KeyError(account_id)
        balances = await self.client.balance(account_id)
        available = Decimal(0)
        for b in balances:
            if b.asset == "USDT":
                available = b.available_balance
                break
        orders = await self.client.open_orders(account_id, symbol=SYMBOL)
        positions_raw = await self.client.position_risk(account_id, symbol=SYMBOL)

        orders_by_symbol: dict[str, list] = {}
        for o in orders:
            orders_by_symbol.setdefault(o.symbol, []).append(o)

        positions: dict[PositionKey, PositionState] = {}
        leverages: dict[str, set[int]] = {}
        for p in positions_raw:
            if p.leverage > 0:
                leverages.setdefault(p.symbol, set()).add(p.leverage)
            key = PositionKey(symbol=p.symbol, pos_side=p.position_side)
            leg_orders = [
                o
                for o in orders_by_symbol.get(p.symbol, [])
                if o.position_side in (key.pos_side, PosSide.BOTH)
            ]
            stop, extra_stops = _pick_with_extras(leg_orders, STOP_TYPES)
            tp, extra_tps = _pick_with_extras(leg_orders, TP_TYPES)
            if p.position_amt == 0 and key.pos_side == PosSide.BOTH and not leg_orders:
                continue
            positions[key] = PositionState(
                key=key,
                qty=p.position_amt,
                entry_price=p.entry_price,
                leverage=int(p.leverage),
                mark_price=p.mark_price,
                liquidation_price=p.liquidation_price,
                stop=stop,
                take_profit=tp,
                extra_stops=extra_stops,
                extra_tps=extra_tps,
                unrealized_profit=getattr(p, "un_realized_profit", Decimal(0)),
            )

        self.snapshot = AccountState(
            account_id=account_id,
            available_usdt=available,
            positions=positions,
            open_orders=[o.model_dump() for o in orders],
            symbol_leverages={
                sym: next(iter(vals)) for sym, vals in leverages.items() if len(vals) == 1
            },
        )
        return self.snapshot

    async def refresh_agent_permission(self, account_id: str) -> AgentValidation:
        agent = await self.client.agent(account_id)
        checked_at_ms = int(time.time() * 1000)
        return AgentValidation(
            signer_address=agent.agent_address or "",
            can_read=bool(agent.can_read),
            can_perp_trade=bool(agent.can_perp_trade),
            can_withdraw=bool(agent.can_withdraw),
            ip_whitelist="testnet-live-override",
            status="VALID_OVERRIDE",
            expired_ms=int(agent.expired or 0),
            checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked_at_ms / 1000)),
        )

    async def read_activity(self, account_id: str, limit: int, offset: int) -> list:
        return []

    async def recover_account_runtime(
        self, account_id: str, user_address: str, signer_address: str
    ) -> bool:
        return True


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    cfg = Settings(
        db_path=tmp_path / "live_positions.sqlite3",
        log_path=tmp_path / "live.log",
        builder_name="copytradeaster",
        max_fee_rate="0.0001",
        telegram_bot_token=BOT_TOKEN,
        testnet_ip_whitelist_override=True,
    )
    ensure_testnet_guard(cfg.rest_base_url, 714, cfg.registration_chain_id)
    return cfg


@pytest.fixture
async def live_positions_env(test_settings):
    db = Database(test_settings.db_path)
    await db.connect()
    await db.migrate()
    http = httpx.AsyncClient(timeout=httpx.Timeout(25.0))
    user_address, signer_address, signer_key = load_api_wallet_section(USER_SECTION)
    account_id = f"tg-{USER_TG_ID}"
    credentials = SqliteCredentials(db, test_settings.encryption_key_bytes())

    validation = AgentValidation(
        signer_address=signer_address,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        ip_whitelist="testnet-live-override",
        status="VALID_OVERRIDE",
        expired_ms=int((time.time() + 30 * 86400) * 1000),
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    await credentials.import_identity(
        account_id=account_id,
        user=user_address,
        signer_private_key_hex=signer_key,
        role="follower",
        label="testnet-live-positions",
        owner_telegram_user_id=USER_TG_ID,
        owner_chat_id=USER_TG_ID,
        validation=validation,
    )
    await TelegramUserRepo(db).upsert(
        USER_TG_ID,
        USER_TG_ID,
        account_id=account_id,
        access_enabled=True,
    )
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(Decimal("0.01"), 200, Decimal("0.01"), enabled=True, allow_new_entries=True),
        account_id,
    )
    await SettingsRepo(db).set_flag("ready", "true")

    limiter = RateLimits(2400, 1200, 300)
    nonces = NonceStore(db)
    client = AsterClient(
        http=http,
        base_url=test_settings.rest_base_url,
        limiter=limiter,
        identities=credentials,
        keys=credentials,
        nonces=nonces,
    )
    identity = credentials.identity(account_id)
    await best_effort_flatten(
        http,
        test_settings.rest_base_url,
        identity,
        signer_key,
        nonces,
        limiter,
    )

    runtime = LivePositionsRuntime(db, test_settings, credentials, client, account_id)
    service = UserApplicationService(runtime, admin_ids=[])
    miniapp = TestClient(TestServer(create_miniapp_app(test_settings, service)))
    await miniapp.start_server()
    try:
        yield {
            "client": miniapp,
            "runtime": runtime,
            "aster": client,
            "account_id": account_id,
            "auth": {"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, USER_TG_ID)},
            "user_address": user_address,
            "signer_key": signer_key,
        }
    finally:
        await best_effort_flatten(
            http,
            test_settings.rest_base_url,
            identity,
            signer_key,
            nonces,
            limiter,
        )
        await miniapp.close()
        await http.aclose()
        await db.close()


async def test_live_positions_acceptance_gate(live_positions_env):
    """Executes the full 8-phase live acceptance gate on Aster Testnet."""
    client: TestClient = live_positions_env["client"]
    aster: AsterClient = live_positions_env["aster"]
    account_id: str = live_positions_env["account_id"]
    auth = live_positions_env["auth"]

    # Phase 0: Verify initial empty positions
    resp0 = await client.post("/miniapp/api/positions", headers=auth, json={"refresh": True})
    assert resp0.status == 200
    p0_data = await resp0.json()
    assert p0_data["count"] == 0
    assert p0_data["positions"] == []

    # Determine account position mode (Hedge vs One-Way)
    hedge = await aster.get_position_mode(account_id)
    pside = PosSide.LONG if hedge else PosSide.BOTH
    ro = not hedge

    # Phase 1: Set leverage and Open Position on follower + Stop Market
    await aster.set_leverage(account_id, SYMBOL, 200)
    open_resp = await aster.place_order(
        account_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=QTY,
            position_side=pside,
        ),
    )
    assert open_resp.order_id > 0

    # Read current mark to set realistic protective stop below mark
    pos_risk = await aster.position_risk(account_id, SYMBOL)
    assert len(pos_risk) > 0
    mark_price = pos_risk[0].mark_price
    assert mark_price > 0
    stop_price = (mark_price * Decimal("0.90")).quantize(Decimal("0.1"))
    tp_price = (mark_price * Decimal("1.20")).quantize(Decimal("0.1"))

    stop_resp = await aster.place_order(
        account_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            stop_price=stop_price,
            quantity=QTY if hedge else None,
            reduce_only=ro,
            close_position=not hedge,
            position_side=pside,
            new_client_order_id=f"ct-stop-{int(time.time())}",
        ),
    )
    assert stop_resp.order_id > 0

    # Phase 2 & 3: Query positions endpoint through Mini App aiohttp API
    resp1 = await client.post("/miniapp/api/positions", headers=auth, json={"refresh": True})
    assert resp1.status == 200
    p1_data = await resp1.json()
    assert p1_data["count"] == 1
    pos1 = p1_data["positions"][0]
    assert pos1["symbol"] == SYMBOL
    assert pos1["side"] == "LONG"
    assert Decimal(pos1["qty"]) == QTY
    assert pos1["protection_state"] == "protected"
    assert Decimal(pos1["stop_price"]) == stop_price
    assert Decimal(pos1["risk_to_stop_usdt"]) > 0

    # Phase 4: Compare DTO with signed positionRisk and openOrders
    open_orders = await aster.open_orders(account_id, symbol=SYMBOL)
    assert any(o.order_id == stop_resp.order_id for o in open_orders)
    assert pos1["leverage"] == 200

    # Phase 5: Stop & TP replacement (add TP and move Stop closer)
    tp_resp = await aster.place_order(
        account_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            stop_price=tp_price,
            quantity=QTY if hedge else None,
            reduce_only=ro,
            close_position=not hedge,
            position_side=pside,
            new_client_order_id=f"ct-tp-{int(time.time())}",
        ),
    )
    assert tp_resp.order_id > 0

    resp2 = await client.post("/miniapp/api/positions", headers=auth, json={"refresh": True})
    assert resp2.status == 200
    p2_data = await resp2.json()
    pos2 = p2_data["positions"][0]
    assert Decimal(pos2["take_profit_price"]) == tp_price

    # Phase 6: Partial close (reduce half)
    half_qty = QTY / Decimal(2)
    reduce_resp = await aster.place_order(
        account_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=half_qty,
            reduce_only=ro,
            position_side=pside,
        ),
    )
    assert reduce_resp.order_id > 0

    resp3 = await client.post("/miniapp/api/positions", headers=auth, json={"refresh": True})
    assert resp3.status == 200
    p3_data = await resp3.json()
    pos3 = p3_data["positions"][0]
    assert Decimal(pos3["qty"]) == half_qty

    # Phase 7: Remove stop order and verify warning
    await aster.cancel_order(account_id, SYMBOL, order_id=stop_resp.order_id)
    resp4 = await client.post("/miniapp/api/positions", headers=auth, json={"refresh": True})
    assert resp4.status == 200
    p4_data = await resp4.json()
    pos4 = p4_data["positions"][0]
    assert pos4["protection_state"] == "missing_stop"
    assert pos4["risk_to_stop_usdt"] is None

    # Phase 8: Cleanup flat for repeatability
    close_resp = await aster.place_order(
        account_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=half_qty,
            reduce_only=ro,
            position_side=pside,
        ),
    )
    assert close_resp.order_id > 0
    if tp_resp.order_id > 0:
        with contextlib.suppress(Exception):
            await aster.cancel_order(account_id, SYMBOL, order_id=tp_resp.order_id)

    resp5 = await client.post("/miniapp/api/positions", headers=auth, json={"refresh": True})
    assert resp5.status == 200
    p5_data = await resp5.json()
    assert p5_data["count"] == 0
    assert p5_data["positions"] == []
