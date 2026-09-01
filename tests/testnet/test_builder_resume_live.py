"""Signed Aster Testnet + Mini App HTTP pause/resume scenarios (L7-L8).

These tests use a local aiohttp transport and SQLite control plane, but every
dashboard refresh/resume safety check reads the real Aster Testnet account with
the configured API signer. Telegram identity is accepted only through a valid
HMAC-signed initData string.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import CopyState, SystemState
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import AccountState, PositionKey, PositionState, RiskPolicy
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.onboarding.direct import validate_agent_permissions
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import DirectOnboardingConfig
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AgentValidation,
    NonceStore,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.testnet.support.builder_live import (
    best_effort_flatten,
    ensure_testnet_guard,
    load_api_wallet_section,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

USER_TG_ID = 777_000_103
USER_SECTION = "copyc2"
BOT_TOKEN = "123456:LIVE-MINIAPP-TEST"
UNKNOWN_READ_CODES = frozenset({-1000, -1006, -1007})


def _make_init_data(token: str, user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF" + "7" * 10,
        "user": f'{{"id":{user_id},"first_name":"Live"}}',
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


class LiveSignedRuntime:
    """Small runtime port whose read boundaries are the real Aster Testnet."""

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
        self.actions: list[str] = []
        self.signed_reads = 0
        self.permission_reads = 0

    async def _retry_read(self, call: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(8):
            try:
                result = await call()
                self.signed_reads += 1
                return result
            except AsterError as exc:
                if exc.code not in UNKNOWN_READ_CODES or attempt == 7:
                    raise
                await asyncio.sleep(min(0.5 * (attempt + 1), 1.5))
        raise AssertionError("unreachable signed-read retry state")

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
        balances = await self._retry_read(lambda: self.client.balance(account_id))
        positions_raw = await self._retry_read(lambda: self.client.position_risk(account_id))
        # The Testnet all-symbol openOrders route currently times out
        # persistently; this Stage-3 live scenario trades BTCUSDT only.
        orders = await self._retry_read(
            lambda: self.client.open_orders(account_id, symbol="BTCUSDT")
        )
        available = next(
            (balance.available_balance for balance in balances if balance.asset == "USDT"),
            Decimal(0),
        )
        positions: dict[PositionKey, PositionState] = {}
        leverages: dict[str, set[int]] = {}
        for position in positions_raw:
            if position.leverage > 0:
                leverages.setdefault(position.symbol, set()).add(position.leverage)
            if position.position_amt == 0:
                continue
            key = PositionKey(position.symbol, position.position_side)
            positions[key] = PositionState(
                key=key,
                qty=position.position_amt,
                entry_price=position.entry_price,
                leverage=position.leverage,
                mark_price=position.mark_price,
                liquidation_price=position.liquidation_price,
            )
        self.snapshot = AccountState(
            account_id=account_id,
            available_usdt=available,
            positions=positions,
            open_orders=[order.model_dump() for order in orders],
            symbol_leverages={
                symbol: next(iter(values))
                for symbol, values in leverages.items()
                if len(values) == 1
            },
        )
        self.actions.append("fresh_snapshot")
        return self.snapshot

    async def refresh_agent_permission(self, account_id: str) -> AgentValidation:
        agent = await self._retry_read(lambda: self.client.agent(account_id))
        self.permission_reads += 1
        checked_at_ms = int(time.time() * 1000)
        status = "VALID"
        try:
            validated = validate_agent_permissions(
                agent,
                self.credentials.identity(account_id).signer,
                DirectOnboardingConfig(
                    require_agent_ip_whitelist=self.cfg.require_agent_ip_whitelist,
                    expected_egress_ips=tuple(self.cfg.aster_egress_ips),
                ),
                checked_at_ms=checked_at_ms,
            )
        except OnboardingError as exc:
            if (
                exc.code is not OnboardingErrorCode.AGENT_IP_WHITELIST
                or not self.cfg.testnet_ip_whitelist_override
            ):
                raise
            validated = validate_agent_permissions(
                agent,
                self.credentials.identity(account_id).signer,
                DirectOnboardingConfig(require_agent_ip_whitelist=False),
                checked_at_ms=checked_at_ms,
            )
            status = "VALID_OVERRIDE"
        return AgentValidation(
            signer_address=validated.signer_address,
            can_read=True,
            can_perp_trade=True,
            can_withdraw=False,
            expired_ms=validated.expired_ms,
            ip_whitelist=validated.ip_whitelist,
            status=status,
            checked_at=datetime.fromtimestamp(checked_at_ms / 1000, tz=UTC).isoformat(),
        )

    async def permission_override_allowed(self, account_id: str) -> bool:
        return account_id == self.account_id and self.cfg.testnet_ip_whitelist_override

    async def validate_resume_safety(self, account_id: str, snapshot: AccountState) -> None:
        if account_id != self.account_id or snapshot.account_id != account_id:
            raise RuntimeError("wrong live account snapshot")
        has_position = any(position.qty != 0 for position in snapshot.positions.values())
        if snapshot.open_orders or has_position:
            raise RuntimeError("live Testnet account must be flat before resume")
        self.actions.append("resume_safety_checked")

    async def pause_account(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise KeyError(account_id)
        self.actions.append("paused")

    async def resume_account(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise KeyError(account_id)
        self.actions.append("resumed")

    async def reconcile_account(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise KeyError(account_id)
        self.actions.append("reconciled")

    async def read_activity(self, account_id: str, limit: int, offset: int) -> list[Any]:
        del account_id, limit, offset
        return []

    async def master_has_open_positions(self) -> bool:
        return False


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    cfg = Settings(
        db_path=tmp_path / "live_resume.sqlite3",
        log_path=tmp_path / "live.log",
        builder_name="copytradeaster",
        max_fee_rate="0.0001",
        telegram_bot_token=BOT_TOKEN,
        testnet_ip_whitelist_override=True,
    )
    ensure_testnet_guard(cfg.rest_base_url, 714, cfg.registration_chain_id)
    return cfg


@pytest.fixture
async def live_http_env(test_settings):
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
        expired_ms=int((datetime.now(UTC) + timedelta(days=30)).timestamp() * 1000),
        # Force Resume through the real signed /agent permission boundary.
        # A fresh seeded row would make this nominally-live test trust cache.
        checked_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    await credentials.import_identity(
        account_id=account_id,
        user=user_address,
        signer_private_key_hex=signer_key,
        role="follower",
        label="testnet-live-miniapp",
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
        RiskPolicy(Decimal("0.01"), 20, Decimal("0.01"), enabled=True, allow_new_entries=False),
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
    runtime = LiveSignedRuntime(db, test_settings, credentials, client, account_id)
    service = UserApplicationService(runtime, admin_ids=[])
    miniapp = TestClient(TestServer(create_miniapp_app(test_settings, service)))
    await miniapp.start_server()
    try:
        yield {
            "client": miniapp,
            "runtime": runtime,
            "db": db,
            "account_id": account_id,
            "auth": {"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, USER_TG_ID)},
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


async def test_l7_pause_resume_via_real_miniapp_http(live_http_env):
    """L7: Telegram HMAC + live snapshot + confirmation-bound resume/pause."""
    client: TestClient = live_http_env["client"]
    runtime: LiveSignedRuntime = live_http_env["runtime"]
    db: Database = live_http_env["db"]
    account_id: str = live_http_env["account_id"]
    auth = live_http_env["auth"]

    bad_signature = await client.post(
        "/miniapp/api/session",
        headers={"X-Telegram-Init-Data": auth["X-Telegram-Init-Data"] + "x"},
    )
    assert bad_signature.status == 401

    unknown_user = await client.post(
        "/miniapp/api/session",
        headers={"X-Telegram-Init-Data": _make_init_data(BOT_TOKEN, USER_TG_ID + 1)},
    )
    assert unknown_user.status == 200
    assert (await unknown_user.json())["access_state"] == "unknown"

    session = await client.post("/miniapp/api/session", headers=auth)
    assert session.status == 200
    session_body = await session.json()
    assert session_body["connection_state"] == "connected"
    assert session_body["copy_state"] == CopyState.PAUSED.value

    dashboard = await client.post(
        "/miniapp/api/dashboard",
        json={"refresh": True},
        headers=auth,
    )
    assert dashboard.status == 200, await dashboard.text()
    dash_body = await dashboard.json()
    assert dash_body["copy_state"] == CopyState.PAUSED.value
    assert dash_body["snapshot_captured_at"] is not None
    assert runtime.signed_reads >= 3

    missing = await client.post("/miniapp/api/resume", json={}, headers=auth)
    assert missing.status == 409
    assert (await missing.json())["error"] == "confirmation_expired"

    begin = await client.post(
        "/miniapp/api/confirmation/begin",
        json={"action": "resume"},
        headers=auth,
    )
    assert begin.status == 200
    confirmation = await begin.json()
    assert confirmation["account_id"] == account_id

    forged = dict(confirmation)
    forged["token"] = "not-the-issued-token"
    stale = await client.post(
        "/miniapp/api/resume",
        json={"confirmation": forged},
        headers=auth,
    )
    assert stale.status == 409
    assert (await stale.json())["error"] == "confirmation_expired"

    resumed = await client.post(
        "/miniapp/api/resume",
        json={"confirmation": confirmation},
        headers=auth,
    )
    assert resumed.status == 200, await resumed.text()
    assert (await resumed.json())["copy_state"] == CopyState.ACTIVE.value
    assert {"fresh_snapshot", "resume_safety_checked", "resumed", "reconciled"}.issubset(
        runtime.actions
    )
    assert runtime.permission_reads >= 1
    policy = await SettingsRepo(db).get_policy(account_id)
    assert policy is not None and policy.allow_new_entries is True

    paused = await client.post("/miniapp/api/pause", headers=auth)
    assert paused.status == 200, await paused.text()
    assert (await paused.json())["copy_state"] == CopyState.PAUSED.value
    policy = await SettingsRepo(db).get_policy(account_id)
    assert policy is not None and policy.allow_new_entries is False


async def test_l8_real_diagnostic_snapshot(live_http_env):
    """L8: Dashboard diagnostic is backed by a fresh signed Testnet snapshot."""
    client: TestClient = live_http_env["client"]
    runtime: LiveSignedRuntime = live_http_env["runtime"]
    response = await client.post(
        "/miniapp/api/dashboard",
        json={"refresh": True},
        headers=live_http_env["auth"],
    )
    assert response.status == 200, await response.text()
    dashboard = await response.json()
    assert dashboard["system_state"] == SystemState.READY.value
    assert dashboard["snapshot_captured_at"] is not None
    assert dashboard["open_positions"] == 0
    assert runtime.signed_reads >= 3
