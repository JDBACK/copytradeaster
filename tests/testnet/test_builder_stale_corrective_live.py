"""Corrective live gate: stale Builder fee must not block risk reduction."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.app import App
from copytradeaster.aster.trading_api import AsterClient, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, RiskPolicy, Side
from copytradeaster.onboarding.builder import BuilderOnboardingService
from copytradeaster.onboarding.errors import OnboardingError
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderPrepareRequest,
    BuilderSubmitRequest,
)
from copytradeaster.storage.repositories import SettingsRepo, TelegramUserRepo
from tests.testnet.support.builder_live import ensure_testnet_guard, load_wallet_section

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
MASTER_QTY = D("0.004")
BUILDER_USER_TG = 777_000_003
PROD_DB = Path("data/copytrade.sqlite3")
STALE_FEE = "0.0002"


def _backup(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


async def _place(client: AsterClient, aid: str, req: PlaceOrderRequest):
    return await client.place_order(aid, req)


async def _flatten(client: AsterClient, aid: str, hedge: bool, prefix: str) -> None:
    ts = int(time.time() * 1000)
    for o in await client.open_orders(aid, symbol=SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order(aid, SYMBOL, orig_client_order_id=o.client_order_id)
    for i, pos in enumerate(await client.position_risk(aid, SYMBOL)):
        if pos.position_amt == 0:
            continue
        with contextlib.suppress(Exception):
            await client.place_order(
                aid,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL if pos.position_amt > 0 else Side.BUY,
                    type="MARKET",
                    quantity=abs(pos.position_amt),
                    reduce_only=not hedge,
                    position_side=pos.position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"{prefix}-{ts}-{i}"[:36],
                ),
            )


async def test_stale_builder_fee_falls_back_direct_live(tmp_path):
    if not PROD_DB.exists():
        pytest.skip("need data/copytrade.sqlite3")
    db_path = tmp_path / "p27-stale.sqlite3"
    _backup(PROD_DB, db_path)
    settings = Settings(db_path=db_path)
    ensure_testnet_guard(settings.rest_base_url, 714, settings.registration_chain_id)
    http = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
    app = App(settings)
    await app.db.connect()
    await app.db.migrate()
    await app.credentials.load()
    for need in ("main", "copyc1", "copyc2"):
        if need not in app.credentials.account_ids():
            await app.db.close()
            pytest.skip(f"need {need} in prod backup")
    from copytradeaster.application.adapters import SqliteOnboardingStore
    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.storage.repositories import NonceStore

    await TelegramUserRepo(app.db).upsert(BUILDER_USER_TG, BUILDER_USER_TG)
    user_address, user_key = load_wallet_section("copyc2")
    builder_address, _builder_key = load_wallet_section("builder")
    store = SqliteOnboardingStore(app.db, app.credentials)
    await app.db.execute_write(
        "UPDATE telegram_users SET account_id=NULL WHERE account_id=?", ("copyc2",)
    )
    await app.db.execute_write("DELETE FROM credentials WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM agent_validations WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM copy_settings WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM sync_basis WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM accounts WHERE id=?", ("copyc2",))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )

    def _factory(identities, keys):
        return AsterClient(
            http=http,
            base_url=settings.rest_base_url,
            limiter=limiter,
            identities=identities,
            keys=keys,
            nonces=NonceStore(app.db),
            dry_run=False,
        )

    svc = BuilderOnboardingService(
        http,
        limiter,
        _factory,
        store,
        config=BuilderOnboardingConfig(
            base_url=settings.rest_base_url,
            trading_chain_id=714,
            registration_chain_id=settings.registration_chain_id,
            builder_address=builder_address,
            builder_name=settings.builder_name,
            max_fee_rate=settings.max_fee_rate,
            agent_expiry_days=settings.builder_agent_expiry_days,
            approval_ttl_s=settings.builder_approval_ttl_s,
            expected_egress_ips=(),
            require_agent_ip_whitelist=False,
            expected_position_mode=settings.position_mode,
        ),
    )
    try:
        prepared = await svc.prepare(BuilderPrepareRequest(BUILDER_USER_TG, user_address))
    except OnboardingError as exc:
        await app.db.close()
        pytest.skip(f"builder prepare unavailable: {exc.code.value}")
    typed = encode_typed_data(
        domain_data=prepared.eip712["domain"],
        message_types={"ApproveAgent": prepared.eip712["types"]["ApproveAgent"]},
        message_data=prepared.eip712["message"],
    )
    sig = Account.sign_message(typed, user_key).signature.hex()
    for _ in range(12):
        try:
            result = await svc.submit(
                BuilderSubmitRequest(BUILDER_USER_TG, prepared.pending_id, sig)
            )
            break
        except OnboardingError as exc:
            if exc.code.value in {"agent_not_confirmed", "signed_read_failed"}:
                import asyncio as _aio

                await _aio.sleep(1.5)
                continue
            if exc.code.value == "agent_submit_failed":
                await app.db.close()
                pytest.skip("builder address not registered in Aster")
            raise
    else:
        await app.db.close()
        pytest.fail("builder onboarding not confirmed")
    await app.credentials.load()
    app.builder_attribution._builder_address = settings.builder_address
    app.builder_attribution._max_fee_rate = settings.max_fee_rate
    builder_aid = result.account_id
    await app.register_follower("copyc1")
    await app.register_follower(builder_aid)
    info = await app.master_client.exchange_info(force=True)
    app._filters_cache.update(info.tradable_filters())
    await app.leverage.refresh_catalog()
    await app.leverage.prepare_account("copyc1", [SYMBOL])
    await app.leverage.prepare_account(builder_aid, [SYMBOL])
    repo = SettingsRepo(app.db)
    await repo.set_flag("allow_new_entries", "true")
    await repo.set_flag("emergency_active", "false")
    await repo.set_flag("ready", "true")
    for aid in ("copyc1", builder_aid):
        await repo.upsert_policy(
            RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
            aid,
        )
    hedge = await app.master_client.get_position_mode("main")
    for aid in ("main", "copyc1", builder_aid):
        h = await app.master_client.get_position_mode(aid)
        await _flatten(app.master_client, aid, h, "pre")
    seen: list = []
    orig_place = AsterClient.place_order

    async def _spy(self, account_id, req):
        if account_id in ("copyc1", builder_aid):
            seen.append(
                {
                    "aid": account_id,
                    "type": req.type,
                    "builder": req.builder,
                    "fee": None if req.fee_rate is None else str(req.fee_rate),
                    "coid": req.new_client_order_id,
                }
            )
        return await orig_place(self, account_id, req)

    AsterClient.place_order = _spy
    try:
        long_side = PosSide.LONG if hedge else PosSide.BOTH
        ts = int(time.time() * 1000)
        await _place(
            app.master_client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=MASTER_QTY,
                position_side=long_side,
                new_order_resp_type="RESULT",
                new_client_order_id=f"stale-m-{ts}"[:36],
            ),
        )
        risks = await app.master_client.position_risk("main", SYMBOL)
        mark = risks[0].mark_price if risks and risks[0].mark_price > 0 else D("60000")
        stop_px = (mark * D("0.95")).quantize(D("0.1"))
        await _place(
            app.master_client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                stop_price=stop_px,
                close_position=True,
                position_side=long_side,
                new_client_order_id=f"stale-s-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        # NOTE (Package B entry admission): a bare, never-started App keeps
        # process-local entry admission closed (READY + live WS + clean
        # projector required), so the engine cannot drive the opening leg
        # here. Seed both follower legs directly on the REAL exchange without
        # builder fields (Builder MARKET-entry attribution itself is proven by
        # the P2.7 live attribution gate); this test owns the reduction leg:
        # stale BUILDER_VALID -> real -5026 -> exactly one DIRECT retry.
        for aid in ("copyc1", builder_aid):
            await _place(
                app.master_client,
                aid,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.BUY,
                    type="MARKET",
                    quantity=MASTER_QTY,
                    position_side=long_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"stale-f-{aid}-{ts}"[:36],
                ),
            )
        seed_open = [s for s in seen if s["type"] == "MARKET"]
        assert len(seed_open) == 2, seed_open
        assert all(s["builder"] is None for s in seed_open)
        for aid in ("copyc1", builder_aid):
            qty = D("0")
            for p in await app.master_client.position_risk(aid, SYMBOL):
                if p.symbol == SYMBOL and p.position_side == long_side:
                    qty = p.position_amt
            assert qty == MASTER_QTY, (aid, qty)
        seen.clear()
        snap = await app.db.fetchone("SELECT * FROM accounts WHERE id=?", (builder_aid,))
        assert snap is not None
        await app.db.execute_write(
            "UPDATE accounts SET builder_max_fee_rate=?, builder_verified_at=? WHERE id=?",
            (STALE_FEE, datetime.now(UTC).isoformat(), builder_aid),
        )
        app.builder_attribution._max_fee_rate = STALE_FEE
        decision = await app.builder_attribution.resolve(builder_aid)
        assert decision.kind.value == "BUILDER_VALID"
        assert str(decision.attribution.fee_rate) == STALE_FEE
        seen.clear()
        close_side = Side.SELL
        await _place(
            app.master_client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=close_side,
                type="MARKET",
                quantity=MASTER_QTY,
                reduce_only=not hedge,
                position_side=long_side,
                new_order_resp_type="RESULT",
                new_client_order_id=f"stale-c-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower(builder_aid, reason="stale-close")
        b_close = [s for s in seen if s["aid"] == builder_aid and s["type"] == "MARKET"]
        assert len(b_close) >= 2, b_close
        assert b_close[0]["builder"] is not None
        assert b_close[0]["fee"] == STALE_FEE
        assert b_close[1]["builder"] is None
        assert b_close[1]["fee"] is None
        assert b_close[0]["coid"] != b_close[1]["coid"]
        for p in await app.master_client.position_risk(builder_aid, SYMBOL):
            assert p.position_amt == 0
        snap2 = await app.db.fetchone("SELECT * FROM accounts WHERE id=?", (builder_aid,))
        assert str(snap2["identity_source"]) == "builder"
        assert snap2["builder_address"] is None
        decision2 = await app.builder_attribution.resolve(builder_aid)
        assert decision2.kind.value == "BUILDER_UNAVAILABLE"
        degraded = await SettingsRepo(app.db).get_flag("degraded")
        assert degraded is not None and "builder-rejected" in degraded and "-5026" in degraded
        rows = await app.db.fetchall(
            "SELECT payload FROM execution_commands WHERE account_id=?", (builder_aid,)
        )
        for r in rows:
            assert "builder" not in str(r["payload"]) and "feeRate" not in str(r["payload"])
        seen.clear()
        ts2 = int(time.time() * 1000)
        await _place(
            app.master_client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=MASTER_QTY,
                position_side=long_side,
                new_order_resp_type="RESULT",
                new_client_order_id=f"stale-m2-{ts2}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower(builder_aid, reason="stale-block")
        b_block = [s for s in seen if s["aid"] == builder_aid]
        assert b_block == [], b_block
        for p in await app.master_client.position_risk(builder_aid, SYMBOL):
            assert p.position_amt == 0
        for aid in ("main", "copyc1", builder_aid):
            h = await app.master_client.get_position_mode(aid)
            await _flatten(app.master_client, aid, h, "pre-final")
        for aid in ("main", "copyc1", builder_aid):
            for p in await app.master_client.position_risk(aid, SYMBOL):
                assert p.position_amt == 0
            assert await app.master_client.open_orders(aid, symbol=SYMBOL) == []
    finally:
        AsterClient.place_order = orig_place
        with contextlib.suppress(Exception):
            for aid in ("main", "copyc1", builder_aid):
                h = await app.master_client.get_position_mode(aid)
                await _flatten(app.master_client, aid, h, "post")
        await http.aclose()
        await app.db.close()
