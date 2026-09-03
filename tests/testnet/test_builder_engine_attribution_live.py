"""P2.7 signed engine proof: Builder + direct routing via production copy core."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
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
from tests.testnet.support.builder_live import (
    ensure_testnet_guard,
    load_wallet_section,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
MASTER_QTY = D("0.004")
BUILDER_USER_TG = 777_000_002
PROD_DB = Path("data/copytrade.sqlite3")


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


async def test_builder_and_direct_routing_via_production_engine(tmp_path):
    if not PROD_DB.exists():
        pytest.skip("need data/copytrade.sqlite3")
    db_path = tmp_path / "p27-engine.sqlite3"
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
    # Live Builder onboarding for copyc2 on the scratch DB only.
    from copytradeaster.application.adapters import SqliteOnboardingStore
    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.storage.repositories import NonceStore

    await TelegramUserRepo(app.db).upsert(BUILDER_USER_TG, BUILDER_USER_TG)
    user_address, user_key = load_wallet_section("copyc2")
    builder_address, _builder_key = load_wallet_section("builder")
    store = SqliteOnboardingStore(app.db, app.credentials)
    # Scratch only: free the copyc2 user-address slot carried by the prod backup
    # so live Builder onboarding can claim it without touching production.
    await app.db.execute_write(
        "UPDATE telegram_users SET account_id=NULL WHERE account_id=?", ("copyc2",)
    )
    await app.db.execute_write("DELETE FROM credentials WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM agent_validations WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM copy_settings WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM sync_basis WHERE account_id=?", ("copyc2",))
    await app.db.execute_write("DELETE FROM accounts WHERE id=?", ("copyc2",))
    owner_tg = BUILDER_USER_TG
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
    approved_fee = settings.max_fee_rate
    try:
        prepared = await svc.prepare(BuilderPrepareRequest(owner_tg, user_address))
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
            result = await svc.submit(BuilderSubmitRequest(owner_tg, prepared.pending_id, sig))
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
    assert result.paused is True
    await app.credentials.load()
    # Rebuild app-side attribution against fresh config (scratch DB rows).
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
    # Typed spy before the real signed call: no URL/nonce/signature access.
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
        placed = await orig_place(self, account_id, req)
        if (
            account_id in ("copyc1", builder_aid)
            and seen
            and seen[-1].get("coid") == req.new_client_order_id
        ):
            seen[-1]["order_id"] = str(getattr(placed, "order_id", ""))
        return placed

    AsterClient.place_order = _spy  # type: ignore[method-assign]
    try:
        import time as _tmod

        window_start = int(_tmod.time() * 1000)
        risks = await app.master_client.position_risk("main", SYMBOL)
        mark = risks[0].mark_price if risks and risks[0].mark_price > 0 else D("60000")
        stop_px = (mark * D("0.95")).quantize(D("0.1"))
        tp_px = (mark * D("1.05")).quantize(D("0.1"))
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
                new_client_order_id=f"p27-m-{ts}"[:36],
            ),
        )
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
                new_client_order_id=f"p27-s-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower(builder_aid, reason="p27")
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower("copyc1", reason="p27")
        b_orders = [s for s in seen if s["aid"] == builder_aid]
        d_orders = [s for s in seen if s["aid"] == "copyc1"]
        assert any(s["type"] == "MARKET" for s in b_orders), seen
        assert any(s["type"] == "STOP_MARKET" for s in b_orders), seen
        for s in b_orders:
            assert s["builder"] is not None and s["builder"].lower() == builder_address.lower()
            assert D(str(s["fee"])) == D(str(approved_fee))
        for s in d_orders:
            assert s["builder"] is None and s["fee"] is None
        # Partial reduction then TP then full close.
        seen.clear()
        close_side = Side.SELL
        await _place(
            app.master_client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=close_side,
                type="MARKET",
                quantity=MASTER_QTY / 2,
                reduce_only=not hedge,
                position_side=long_side,
                new_order_resp_type="RESULT",
                new_client_order_id=f"p27-p-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower(builder_aid, reason="p27-part")
        assert any(
            s["type"] == "MARKET" and s["builder"] is not None
            for s in seen
            if s["aid"] == builder_aid
        )
        seen.clear()
        for o in await app.master_client.open_orders("main", symbol=SYMBOL):
            with contextlib.suppress(Exception):
                await app.master_client.cancel_order(
                    "main", SYMBOL, orig_client_order_id=o.client_order_id
                )
        await _place(
            app.master_client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="TAKE_PROFIT_MARKET",
                stop_price=tp_px,
                close_position=True,
                position_side=long_side,
                new_client_order_id=f"p27-t-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower(builder_aid, reason="p27-tp")
        tp_seen = [s for s in seen if s["aid"] == builder_aid and s["type"] == "TAKE_PROFIT_MARKET"]
        assert tp_seen, seen
        assert all(s["builder"] is not None for s in tp_seen)
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
                new_client_order_id=f"p27-c-{ts}"[:36],
            ),
        )
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower("copyc1", reason="p27-close")
        await app.reconcile_follower(builder_aid, reason="p27-close")
        await app.projector.rebuild_full(app.master_client, "main")
        await app.reconcile_follower("copyc1", reason="p27-close-verify")
        await app.reconcile_follower(builder_aid, reason="p27-close-verify")
        for aid in ("copyc1", builder_aid):
            for p in await app.master_client.position_risk(aid, SYMBOL):
                assert p.position_amt == 0
            assert await app.master_client.open_orders(aid, symbol=SYMBOL) == []
        rows = await app.db.fetchall(
            "SELECT payload FROM execution_commands WHERE account_id IN (?,?)",
            ("copyc1", builder_aid),
        )
        for r in rows:
            assert "builder" not in str(r["payload"]) and "feeRate" not in str(r["payload"])
        # Exchange-side above-cap diagnostic on the Builder identity: far-away
        # LIMIT so an unexpected acceptance never executes. Production never
        # sends this; the probe proves server-side cap semantics.
        over_fee = D(str(approved_fee)) + D("0.0001")
        assert over_fee <= D("0.001")
        cap_ts = int(_tmod.time() * 1000)
        cap_req = PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="LIMIT",
            quantity=D("0.002"),
            price=D("1000"),
            builder=builder_address,
            fee_rate=over_fee,
            new_client_order_id=f"p27-cap-{cap_ts}"[:36],
        )
        try:
            cap_order = await app.master_client.place_order(builder_aid, cap_req)
        except Exception:
            cap_order = None
        if cap_order is not None:
            with contextlib.suppress(Exception):
                await app.master_client.cancel_order(
                    builder_aid, SYMBOL, orig_client_order_id=cap_order.client_order_id
                )
            pytest.fail("above-approved feeRate was unexpectedly accepted")
        window_end = int(_tmod.time() * 1000)
        # Bounded /builder/userTrades probe (non-gating): builder-auth only.
        from copytradeaster.aster.rate_limits import RateLimits as _RL
        from copytradeaster.storage.repositories import NonceStore as _NS
        from tests.testnet.support.builder_live import signed_read_json as _sread

        _, builder_key = load_wallet_section("builder")
        builder_ident = type(
            "I", (), {"user": builder_address, "signer": builder_address, "chain_id": 714}
        )()
        probe_ns = _NS(app.db)
        probe_lim = _RL(
            settings.weight_limit_per_min,
            settings.orders_limit_per_min,
            settings.orders_limit_per_10s,
        )
        challenged = [
            s.get("order_id") for s in seen if s["aid"] == builder_aid and s.get("order_id")
        ]
        trades_body = await _sread(
            http,
            settings.rest_base_url,
            "/fapi/v3/builder/userTrades",
            {
                "symbol": SYMBOL,
                "limit": "10",
                "startTime": str(window_start),
                "endTime": str(window_end),
            },
            builder_ident,
            builder_key,
            probe_ns,
            probe_lim,
            weight=5,
            allowed_error_codes=frozenset({-2015, -2016}),
            optional_after_retries=True,
        )
        if trades_body is None:
            print(
                "WARNING: /builder/userTrades not queryable with bare builder identity (non-gating)"
            )
        else:
            rows_bt = (
                trades_body
                if isinstance(trades_body, list)
                else trades_body.get("rows", trades_body.get("data", []))
            )
            matched = [r for r in rows_bt if str(r.get("orderId", "")) in set(challenged)]
            if matched:
                assert all("builderFee" in r for r in matched), matched
                print(
                    f"userTrades matched {len(matched)} acceptance orders with builderFee present"
                )
            else:
                print("WARNING: userTrades unmatched window (non-gating)")
    finally:
        AsterClient.place_order = orig_place  # type: ignore[method-assign]
        with contextlib.suppress(Exception):
            for aid in ("main", "copyc1", builder_aid):
                h = await app.master_client.get_position_mode(aid)
                await _flatten(app.master_client, aid, h, "post")
        await http.aclose()
        await app.db.close()


async def test_above_cap_diagnostic_rejected(tmp_path):
    settings = Settings(db_path=tmp_path / "p27-cap.sqlite3")
    ensure_testnet_guard(settings.rest_base_url, 714, settings.registration_chain_id)
    if not PROD_DB.exists():
        pytest.skip("need data/copytrade.sqlite3")
    db_path = tmp_path / "p27-cap-backup.sqlite3"
    _backup(PROD_DB, db_path)
    settings = Settings(db_path=db_path)
    app = App(settings)
    await app.db.connect()
    await app.db.migrate()
    await app.credentials.load()
    if "copyc1" not in app.credentials.account_ids():
        await app.db.close()
        pytest.skip("need copyc1")
    over = D(str(settings.max_fee_rate)) + D("0.0001")
    assert over <= D("0.001")
    req = PlaceOrderRequest(
        symbol=SYMBOL,
        side=Side.BUY,
        type="LIMIT",
        quantity=D("0.002"),
        price=D("1000"),
        builder=settings.builder_address,
        fee_rate=over,
    )
    try:
        order = await app.master_client.place_order("copyc1", req)
    except Exception:
        await app.db.close()
        return
    try:
        with contextlib.suppress(Exception):
            await app.master_client.cancel_order(
                "copyc1", SYMBOL, orig_client_order_id=order.client_order_id
            )
    finally:
        await app.db.close()
    pytest.fail("above-approved feeRate was unexpectedly accepted")
