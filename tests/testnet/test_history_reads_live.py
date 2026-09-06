"""Read-only history contract probe; never starts engine or places orders."""

from __future__ import annotations

import os
import time
from decimal import Decimal

import httpx
import pytest

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.onboarding.providers import EphemeralIdentityProvider, EphemeralKeyProvider
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import NonceStore
from tests.testnet.support.builder_live import load_api_wallet_section

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]


async def test_existing_follower_history_contract(tmp_path):
    user, signer, key = load_api_wallet_section("copyc1")
    db = Database(tmp_path / "history-probe.sqlite3")
    await db.connect()
    await db.migrate()
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            client = AsterClient(
                http,
                "https://fapi.asterdex-testnet.com",
                RateLimits(1200, 1200, 1200),
                EphemeralIdentityProvider(
                    "probe",
                    TradingIdentity(account_id="probe", user=user, signer=signer, chain_id=714),
                ),
                EphemeralKeyProvider("probe", key),
                NonceStore(db),
            )
            info = await client.exchange_info(force=True)
            symbol = next(row for row in info.symbols if row.symbol == "BTCUSDT")
            assert symbol.baseAsset == "BTC"
            assert symbol.quoteAsset == "USDT"
            assert symbol.marginAsset == "USDT"
            positions = await client.position_risk("probe", "BTCUSDT")
            assert positions
            print(
                {
                    "position_update_time_present": all(
                        p.update_time is not None for p in positions
                    ),
                    "position_updates_nonzero": sum(bool(p.update_time) for p in positions),
                }
            )
            end = int(time.time() * 1000)
            start = end - 7 * 86400000
            fills = await client.user_trades("probe", "BTCUSDT", start_time=start, end_time=end)
            assert fills, "nonempty follower history required for evidence"
            # Probe a bounded populated slice, rather than treating the first full page as complete.
            start = fills[0].time
            end = start + 60000
            fills = await client.user_trades("probe", "BTCUSDT", start_time=start, end_time=end)
            income = await client.income_history("probe", start_time=start, end_time=end)
            assert fills
            assert len(fills) < 1000, "probe requires an unsaturated window"
            assert len(income) < 1000, "probe requires an unsaturated income window"
            assert all(start <= fill.time <= end for fill in fills)
            assert len({fill.trade_id for fill in fills}) == len(fills)
            tail = await client.user_trades("probe", "BTCUSDT", from_id=fills[-1].trade_id)
            assert any(fill.trade_id == fills[-1].trade_id for fill in tail)
            commissions = {
                row.trade_id: row.income
                for row in income
                if row.income_type == "COMMISSION" and row.symbol == "BTCUSDT"
            }
            matched = [fill for fill in fills if str(fill.trade_id) in commissions]
            assert matched, "commission linkage must be observed"
            signs = set()
            for fill in matched:
                flow = commissions[str(fill.trade_id)]
                assert abs(fill.commission) == abs(flow)
                if fill.commission:
                    signs.add("same" if fill.commission == flow else "opposite")
            assert len(signs) == 1
            print(
                {
                    "fills": len(fills),
                    "income": len(income),
                    "matched": len(matched),
                    "commission_sign_vs_income": sorted(signs),
                    "income_types": sorted({row.income_type for row in income}),
                    "commission_positive": sum(f.commission > Decimal(0) for f in fills),
                }
            )
    finally:
        await db.close()
