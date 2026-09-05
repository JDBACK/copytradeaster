"""Package C: network-profile + unified preflight live matrix (marker `testnet`).

Live-only qualification of the PM-B4 single-profile injection over the REAL
Aster Testnet (``fapi.asterdex-testnet.com``) with real ``wallet.txt``-backed
accounts.  No Shadow/Fake anywhere: every exchange read goes through a real
``AsterClient``; fault injection only wraps the real client around the failure
window (forced position-mode outage) and NEVER substitutes state.  The MAINNET
profile object is composed but never started: no test opens a mainnet
connection or sends a mainnet request.

Row coverage (Network / Preflight qualification rows):
- N1 ``test_mainnet_profile_app_composition_no_orders``: a MAINNET-profile
  ``Settings`` resolves to the exact mainnet hosts/trading-1666/reg-56, mixed
  combos fail closed at construction, the production ``App`` composes on it
  (init only, zero HTTP, zero orders), and onboarding configs bind to the same
  profile via ``from_profile``.
- P1 ``test_preflight_blocks_on_forced_mode_metadata_failure``: with a MAINNET
  profile, a forced ``get_position_mode`` outage blocks readiness fail-CLOSED
  (the ``app.py:440`` fail-open path is not repeated here).
- P2 ``test_preflight_blocks_on_seeded_in_flight_row``: a seeded SENDING
  command row blocks readiness via the ``no_inflight`` row.

Every test ends with best-effort flatten + an independent signed flat audit
(``position_risk`` flat + ``open_orders`` empty) in ``finally``.  Skipped
unless ``CT_TESTNET=1``.  NOBODY runs live pytest from shared-state
checkouts: these tests are implementation-only until the pre-mainnet run.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from copytradeaster.app import App, now_ms
from copytradeaster.aster.network_profile import AsterNetworkProfile
from copytradeaster.aster.trading_api import (
    AsterClient,
    AsterError,
    PlaceOrderRequest,
)
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import Side
from copytradeaster.onboarding.models import BuilderOnboardingConfig, DirectOnboardingConfig
from copytradeaster.onboarding.preflight import run_mainnet_preflight
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import CommandRepo, NonceStore

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

PROD_DB = Path("data/copytrade.sqlite3")
_SEED_TABLES = ("accounts", "credentials", "copy_settings", "agent_validations")

MAINNET_REST = "https://fapi.asterdex.com"
MAINNET_WS = "wss://fstream.asterdex.com/ws"


# --- scratch scaffolding (mirrors test_pre_mainnet_master_live.py) -----------------


def _testnet_settings_for(tmp_path: Path, name: str) -> Settings:
    settings = Settings(
        db_path=tmp_path / f"{name}.sqlite3",
        log_path=tmp_path / f"{name}.log",
    )
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    if settings.dry_run:
        pytest.fail("live matrix refuses to run in dry_run: orders would be fake")
    return settings


def _mainnet_settings_for(tmp_path: Path, name: str) -> Settings:
    # The live .env sets CT_TESTNET_IP_WHITELIST_OVERRIDE=true, which the
    # Settings guard correctly rejects for non-testnet hosts. The MAINNET
    # profile composition needs that testnet-only flag off; pass it
    # explicitly (init kwargs beat env/.env) without touching the guard.
    settings = Settings(
        db_path=tmp_path / f"{name}.sqlite3",
        log_path=tmp_path / f"{name}.log",
        rest_base_url=MAINNET_REST,
        ws_user_base=MAINNET_WS,
        trading_chain_id=1666,
        allow_mainnet=True,
        testnet_ip_whitelist_override=False,
    )
    if settings.dry_run:
        pytest.fail("live matrix refuses to run in dry_run: orders would be fake")
    return settings


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    src_tables = {
        row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    dst_tables = {
        row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table not in src_tables or table not in dst_tables:
        return 0
    src_cols = [row[1] for row in src.execute(f"PRAGMA table_info({table})")]
    dst_cols = [row[1] for row in dst.execute(f"PRAGMA table_info({table})")]
    cols = [col for col in src_cols if col in dst_cols]
    if not cols:
        return 0
    names = ", ".join(f'"{col}"' for col in cols)
    placeholders = ", ".join("?" for _ in cols)
    rows = src.execute(f"SELECT {names} FROM {table}").fetchall()
    if rows:
        dst.executemany(f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({placeholders})', rows)
        dst.commit()
    return len(rows)


def _seed_scratch(db_path: Path, max_followers: int = 2) -> list[str]:
    """Copy accounts+credentials into a fresh scratch DB; keep main + K followers."""
    if not PROD_DB.exists():
        pytest.skip("production DB data/copytrade.sqlite3 is required to seed live credentials")
    src = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    try:
        for table in _SEED_TABLES:
            _copy_table(src, dst, table)
        acct_cols = [row[1] for row in dst.execute("PRAGMA table_info(accounts)")]
        if "execution_mode" in acct_cols:
            mode_filter = "AND COALESCE(execution_mode, 'REAL') = 'REAL'"
        else:  # pragma: no cover - legacy schema without execution_mode
            mode_filter = ""
        rows = dst.execute(
            "SELECT id FROM accounts WHERE role = 'follower' AND enabled = 1 "
            f"{mode_filter} ORDER BY id"
        ).fetchall()
        keep = [str(row[0]) for row in rows[:max_followers]]
        if not keep:
            pytest.skip("at least one imported REAL follower is required")
        wanted = ",".join("?" for _ in ("main", *keep))
        dst.execute(f"UPDATE accounts SET enabled = 0 WHERE id NOT IN ({wanted})", ("main", *keep))
        dst.commit()
        return keep
    finally:
        dst.close()
        src.close()


async def _prepare_scratch(tmp_path: Path, name: str) -> tuple[Settings, list[str]]:
    settings = _testnet_settings_for(tmp_path, name)
    seed_db = Database(settings.db_path)
    await seed_db.connect()
    try:
        await seed_db.migrate()
    finally:
        await seed_db.close()
    followers = _seed_scratch(Path(str(settings.db_path)))
    return settings, followers


async def _testnet_client(
    settings: Settings, db: Database
) -> tuple[AsterClient, httpx.AsyncClient]:
    from copytradeaster.aster.rate_limits import RateLimits

    creds = SqliteCredentials(db, settings.encryption_key_bytes())
    await creds.load()
    if "main" not in creds.account_ids():
        pytest.skip("main account credentials are required for the flat audit")
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    client = AsterClient(http, settings.rest_base_url, limiter, creds, creds, NonceStore(db))
    return client, http


async def _flatten_account(client: AsterClient, account_id: str) -> None:
    """Best-effort cancel + close for one account (REAL-05 convention)."""
    for order in await client.open_orders(account_id):
        with contextlib.suppress(Exception):
            await client.cancel_order(
                account_id, order.symbol, orig_client_order_id=order.client_order_id
            )
    for position in await client.position_risk(account_id):
        if position.position_amt != 0:
            with contextlib.suppress(Exception):
                await client.place_order(
                    account_id,
                    PlaceOrderRequest(
                        symbol=position.symbol,
                        side=Side.SELL if position.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(position.position_amt),
                        reduce_only=position.position_side.value == "BOTH",
                        position_side=position.position_side,
                        new_client_order_id=f"pm-flat-{int(time.time() * 1000)}",
                        new_order_resp_type="RESULT",
                    ),
                )


async def _audit_flat(client: AsterClient, account_id: str = "main") -> None:
    """Independent signed audit: flat positions and zero open orders."""
    nonzero = [p for p in await client.position_risk(account_id) if p.position_amt != 0]
    if nonzero:
        pytest.fail(f"flat audit failed: {len(nonzero)} open position leg(s)")
    orders = await client.open_orders(account_id)
    if orders:
        pytest.fail(f"flat audit failed: {len(orders)} open order(s)")


async def _flatten_and_audit(settings: Settings) -> None:
    db = Database(settings.db_path)
    await db.connect()
    http: httpx.AsyncClient | None = None
    try:
        client, http = await _testnet_client(settings, db)
        try:
            await _flatten_account(client, "main")
        finally:
            await _audit_flat(client, "main")
    finally:
        if http is not None:
            with contextlib.suppress(Exception):
                await http.aclose()
        await db.close()


# --- N1: MAINNET-profile App composition consistency (no orders) --------------------


async def test_mainnet_profile_app_composition_no_orders(tmp_path) -> None:
    settings, _ = await _prepare_scratch(tmp_path, "pm-c-network")
    mainnet = _mainnet_settings_for(tmp_path, "pm-c-network-mainnet").model_copy(
        update={"db_path": settings.db_path}
    )
    # The App under test is composed but never started: zero HTTP, zero orders.
    app = App(mainnet)
    try:
        profile = mainnet.network_profile
        assert profile == AsterNetworkProfile.mainnet()
        assert profile.is_mainnet and not profile.is_testnet
        assert profile.rest_base_url == MAINNET_REST
        assert profile.ws_user_base == MAINNET_WS
        assert profile.trading_chain_id == 1666
        assert profile.registration_chain_id == 56
        # App composition follows the single injection: no second host source.
        assert app.cfg.rest_base_url == profile.rest_base_url
        assert app.cfg.ws_user_base == profile.ws_user_base
        assert app.cfg.trading_chain_id == profile.trading_chain_id
        assert app.cfg.network_profile == profile
        # Onboarding binds to the same profile object, not copied strings.
        direct = DirectOnboardingConfig.from_profile(profile)
        builder = BuilderOnboardingConfig.from_profile(
            profile, builder_address="0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
        )
        assert direct.base_url == MAINNET_REST and direct.trading_chain_id == 1666
        assert builder.base_url == MAINNET_REST and builder.trading_chain_id == 1666
        assert builder.registration_chain_id == 56
        # Mixed combos fail closed at construction (dev-override ban included).
        with pytest.raises(ValueError):
            AsterNetworkProfile.resolve(MAINNET_REST, MAINNET_WS, 714)
        with pytest.raises(ValueError):
            AsterNetworkProfile.resolve(
                "https://fapi.asterdex-testnet.com",
                "wss://fstream.asterdex-testnet.com/ws",
                1666,
            )
        with pytest.raises(ValueError):
            AsterNetworkProfile.resolve("https://localhost:8443", "wss://localhost:8443/ws", 714)
        with pytest.raises(ValueError):
            AsterNetworkProfile.resolve(
                "https://fapi.asterdex.com.evil.example",
                "wss://fstream.asterdex.com.evil.example/ws",
                1666,
            )
    finally:
        with contextlib.suppress(Exception):
            await app.stop()
        await _flatten_and_audit(settings)


class _ModeFailingReads:
    """Wrap the real client; only the position-mode window raises (no Fake state)."""

    def __init__(self, inner: AsterClient) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def get_position_mode(self, account_id: str) -> bool:
        raise AsterError(-1006, "injected mode-metadata outage", None)


# --- P1: preflight blocks fail-CLOSED on forced mode-metadata failure --------------


async def test_preflight_blocks_on_forced_mode_metadata_failure(tmp_path) -> None:
    settings, _ = await _prepare_scratch(tmp_path, "pm-c-preflight-mode")
    mainnet = _mainnet_settings_for(tmp_path, "pm-c-preflight-mode-mainnet")
    profile = mainnet.network_profile
    assert profile.is_mainnet

    db = Database(settings.db_path)
    await db.connect()
    http: httpx.AsyncClient | None = None
    try:
        client, http = await _testnet_client(settings, db)
        creds_identity = SqliteCredentials(db, settings.encryption_key_bytes())
        await creds_identity.load()
        identity = creds_identity.identity("main")
        reads = _ModeFailingReads(client)
        report = await run_mainnet_preflight(
            profile=profile,
            settings=mainnet,
            reads=reads,  # type: ignore[arg-type]
            commands=CommandRepo(db),
            account_id="main",
            expected_signer_address=identity.signer,
            user_address=identity.user,
            agent_config=DirectOnboardingConfig(
                base_url=settings.rest_base_url,
                trading_chain_id=714,
                require_agent_ip_whitelist=False,
            ),
            now_ms=now_ms,
        )
        assert report.blocked
        assert "position_mode" in report.blocked_names()
    finally:
        if http is not None:
            with contextlib.suppress(Exception):
                await http.aclose()
        await db.close()
        await _flatten_and_audit(settings)


# --- P2: preflight blocks on a seeded in-flight row --------------------------------


async def test_preflight_blocks_on_seeded_in_flight_row(tmp_path) -> None:
    settings, _ = await _prepare_scratch(tmp_path, "pm-c-preflight-inflight")

    db = Database(settings.db_path)
    await db.connect()
    http: httpx.AsyncClient | None = None
    try:
        client, http = await _testnet_client(settings, db)
        creds_identity = SqliteCredentials(db, settings.encryption_key_bytes())
        await creds_identity.load()
        identity = creds_identity.identity("main")
        commands = CommandRepo(db)
        await commands.insert(
            command_key="pm-c-seeded-inflight",
            account_id="main",
            trade_id=None,
            kind="OPEN",
            payload_json="{}",
            priority=1,
            client_order_id="pm-c-seeded-inflight",
        )
        await commands.set_status("pm-c-seeded-inflight", "SENDING")
        try:
            report = await run_mainnet_preflight(
                profile=settings.network_profile,
                settings=settings,
                reads=client,  # type: ignore[arg-type]
                commands=commands,
                account_id="main",
                expected_signer_address=identity.signer,
                user_address=identity.user,
                agent_config=DirectOnboardingConfig(
                    base_url=settings.rest_base_url,
                    trading_chain_id=714,
                    require_agent_ip_whitelist=False,
                ),
                now_ms=now_ms,
            )
            assert report.blocked
            assert "no_inflight" in report.blocked_names()
        finally:
            with contextlib.suppress(Exception):
                await commands.set_status("pm-c-seeded-inflight", "FAILED", "seed cleanup")
    finally:
        if http is not None:
            with contextlib.suppress(Exception):
                await http.aclose()
        await db.close()
        await _flatten_and_audit(settings)
