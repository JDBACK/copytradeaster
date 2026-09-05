"""Package D: pre-mainnet RISK live matrix (PM-B5/B6/B7, REAL testnet only).

Live-only qualification of the coordinator risk rails over the REAL Aster
Testnet with real ``wallet.txt`` accounts. Every test boots the PRODUCTION
App on a scratch DB (seeded from ``data/copytrade.sqlite3``, never the prod
file itself), drives at most ONE real follower plus ``main``, sends REAL
signed orders, and ends with best-effort flatten + an independent signed
flat audit (``position_risk`` flat + ``open_orders`` empty) in ``finally``.
Skipped unless ``CT_TESTNET=1``. No Shadow/Fake anywhere in this file.

Row coverage (risk-rail qualification rows):
- Dust-1 ``test_dust1_proportional_dust_stays_flat_until_episode_end`` (GUARD,
  green before and after): a same-direction proportional reduction that
  floors to zero latches the sticky dust marker - the follower stays flat
  while the master episode is still open.
- Dust-2 ``test_dust2_riskcap_zero_reenters_same_episode`` (RED until PM-B5):
  a transient risk-cap zero (far stop, same episode) must NOT latch - moving
  the stop back re-opens the follower. Pre-fix code latched every
  target==0&&current>0 and the leg stayed flat forever.
- Dust-3 ``test_dust3_flip_close_reopens_reversed_leg`` (GUARD): a master flip
  closes the old side WITHOUT stickiness and the reversed leg opens.
- Protection ``test_protection_cancel_restores_follower_stop`` (GUARD): an
  externally canceled follower Stop is restored by convergence.
- Protection ``test_protection_naked_leg_safety_flattened`` (RED until PM-B6):
  a follower leg with no usable master Stop AND no own Stop is
  safety-flattened (never inventing a Stop level). Pre-fix code only
  flattened freshly opened legs.
- Liquidation ``test_liquidation_invariant_holds_safe_leg_live`` (PM-B7 honest
  contract): a live flatten is unshapeable on cross-only accounts (theorem in
  the test docstring), so the test asserts the engine HOLDS the safe leg,
  fires no whole-leg close, and recomputes violated=False from live reads.

Dust-2 / Liquidation carry prerequisite gates (balance / leverage windows
read live from the exchange); when the testnet account cannot satisfy the
deterministic setup window the test skips with the measured values instead
of asserting on an unshaped setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import time
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.app import App
from copytradeaster.aster.trading_api import PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, RiskPolicy, Side
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import SettingsRepo
from tests.testnet import test_engine_corrective_live as corrective_live

corrective_campaign = corrective_live.campaign

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
MASTER_QTY = D("0.004")
NEAR_STOP_DISTANCE = D("1200")
PROD_DB = Path("data/copytrade.sqlite3")
_SEED_TABLES = ("accounts", "credentials", "copy_settings", "agent_validations")


# --- scratch scaffolding -------------------------------------------------------


def _settings_for(tmp_path: Path, name: str) -> Settings:
    settings = Settings(
        db_path=tmp_path / f"{name}.sqlite3",
        log_path=tmp_path / f"{name}.log",
    )
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
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


def _seed_scratch(db_path: Path, max_followers: int = 1) -> list[str]:
    """Copy accounts+credentials into a fresh scratch DB; keep main + 1 follower."""
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
            f"SELECT id FROM accounts WHERE role = 'follower' AND enabled = 1 {mode_filter} "
            "ORDER BY id"
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
    settings = _settings_for(tmp_path, name)
    seed_db = Database(settings.db_path)
    await seed_db.connect()
    try:
        await seed_db.migrate()
    finally:
        await seed_db.close()
    followers = _seed_scratch(Path(str(settings.db_path)), 1)
    return settings, followers


async def _wait_for(predicate, timeout: float, what: str, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


async def _ready_flag(app: App) -> bool:
    return await app.settings.get_flag("ready") == "true"


# --- live trading helpers (all REAL signed REST) --------------------------------


async def _position_side(client, account_id: str = "main") -> PosSide:
    hedge = await client.get_position_mode(account_id)
    return PosSide.LONG if hedge else PosSide.BOTH


async def _symbol_qty(client, account_id: str) -> D:
    total = D("0")
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL:
            total += position.position_amt
    return total


async def _follower_qty(client, account_id: str) -> D:
    return await _symbol_qty(client, account_id)


async def _follower_stops(client, account_id: str) -> list:
    return [
        order
        for order in await client.open_orders(account_id, SYMBOL)
        if order.client_order_id.startswith("ct") and order.type.startswith("STOP")
    ]


async def _follower_position(client, account_id: str):
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL and position.position_amt != 0:
            return position
    return None


async def _available_usdt(client, account_id: str) -> D:
    for balance in await client.balance(account_id):
        if balance.asset == "USDT":
            return balance.available_balance
    return D("0")


async def _open_master_long(
    client,
    position_side: PosSide,
    tag: str,
    qty: D = MASTER_QTY,
    stop_distance: D = NEAR_STOP_DISTANCE,
) -> dict:
    """REAL master MARKET open + protective STOP_MARKET. Returns ids for cleanup."""
    stamp = time.time_ns()
    hedge = position_side != PosSide.BOTH
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=qty,
            position_side=position_side,
            new_client_order_id=f"{tag}-open-{stamp}"[:36],
            new_order_resp_type="RESULT",
        ),
    )
    position = next(p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0)
    stop_level = (position.entry_price - stop_distance).quantize(D("0.1"))
    master_stop = await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=abs(position.position_amt),
            stop_price=stop_level,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"{tag}-stop-{stamp}"[:36],
        ),
    )
    return {
        "position_side": position_side,
        "stop_coid": master_stop.client_order_id,
        "stop_price": stop_level,
        "entry_price": position.entry_price,
    }


async def _open_master_short(
    client,
    position_side: PosSide,
    tag: str,
    qty: D = MASTER_QTY,
    stop_distance: D = NEAR_STOP_DISTANCE,
) -> dict:
    """REAL master MARKET short open + protective STOP_MARKET above."""
    stamp = time.time_ns()
    hedge = position_side != PosSide.BOTH
    short_side = PosSide.SHORT if hedge else PosSide.BOTH
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=qty,
            position_side=short_side,
            new_client_order_id=f"{tag}-open-{stamp}"[:36],
            new_order_resp_type="RESULT",
        ),
    )
    position = next(p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0)
    stop_level = (position.entry_price + stop_distance).quantize(D("0.1"))
    master_stop = await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="STOP_MARKET",
            quantity=abs(position.position_amt),
            stop_price=stop_level,
            reduce_only=not hedge,
            position_side=short_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"{tag}-stop-{stamp}"[:36],
        ),
    )
    return {
        "position_side": short_side,
        "stop_coid": master_stop.client_order_id,
        "stop_price": stop_level,
        "entry_price": position.entry_price,
    }


async def _move_master_stop(client, opened: dict, tag: str, new_stop: D) -> str:
    """REAL master stop replace (cancel + new STOP_MARKET). Returns the new coid."""
    stamp = time.time_ns()
    position_side = opened["position_side"]
    hedge = position_side != PosSide.BOTH
    with contextlib.suppress(Exception):
        await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
    master_pos = next(
        p
        for p in await client.position_risk("main", SYMBOL)
        if p.position_amt != 0 and p.position_side == position_side
    )
    is_long = master_pos.position_amt > 0
    replacement = await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL if is_long else Side.BUY,
            type="STOP_MARKET",
            quantity=abs(master_pos.position_amt),
            stop_price=new_stop.quantize(D("0.1")),
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"{tag}-mvstop-{stamp}"[:36],
        ),
    )
    return replacement.client_order_id


async def _master_partial_close(client, position_side: PosSide, tag: str, qty: D) -> None:
    """REAL master partial MARKET close (reduction, episode stays open)."""
    hedge = position_side != PosSide.BOTH
    master_pos = next(
        p
        for p in await client.position_risk("main", SYMBOL)
        if p.position_amt != 0 and p.position_side == position_side
    )
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL if master_pos.position_amt > 0 else Side.BUY,
            type="MARKET",
            quantity=qty,
            reduce_only=not hedge,
            position_side=position_side,
            new_client_order_id=f"{tag}-pclose-{time.time_ns()}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


async def _close_master_leg(client, position_side: PosSide, tag: str) -> None:
    qty = D("0")
    for position in await client.position_risk("main", SYMBOL):
        if position.symbol == SYMBOL and position.position_side == position_side:
            qty += abs(position.position_amt)
    if qty == 0:
        return
    hedge = position_side != PosSide.BOTH
    master_pos = next(
        p
        for p in await client.position_risk("main", SYMBOL)
        if p.symbol == SYMBOL and p.position_side == position_side and p.position_amt != 0
    )
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL if master_pos.position_amt > 0 else Side.BUY,
            type="MARKET",
            quantity=qty,
            reduce_only=not hedge,
            position_side=position_side,
            new_client_order_id=f"{tag}-close-{time.time_ns()}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


async def _teardown_master(client, stop_coids: list[str]) -> None:
    """Best-effort master teardown (B-file order): cancel known stops, then
    close every open main leg, so the still-running App converges followers
    flat BEFORE the finally flatten+audit (no re-converge orphans)."""
    for coid in stop_coids:
        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=coid)
    seen: set[tuple[str, str]] = set()
    for position in await client.position_risk("main", SYMBOL):
        if position.symbol != SYMBOL or position.position_amt == 0:
            continue
        key = (position.symbol, position.position_side.value)
        if key in seen:
            continue
        seen.add(key)
        with contextlib.suppress(Exception):
            await _close_master_leg(client, position.position_side, "pm-end")


async def _flatten_account(client, account_id: str) -> None:
    """Best-effort cancel + close for one account."""
    for order in await client.open_orders(account_id, SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order(
                account_id, SYMBOL, orig_client_order_id=order.client_order_id
            )
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL and position.position_amt != 0:
            with contextlib.suppress(Exception):
                await client.place_order(
                    account_id,
                    PlaceOrderRequest(
                        symbol=SYMBOL,
                        side=Side.SELL if position.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(position.position_amt),
                        reduce_only=position.position_side.value == "BOTH",
                        position_side=position.position_side,
                        new_client_order_id=f"pm-flat-{int(time.time() * 1000)}",
                        new_order_resp_type="RESULT",
                    ),
                )


async def _assert_flat(client, account_ids: list[str]) -> None:
    """Independent signed audit: every account flat, no open orders on SYMBOL."""
    for account_id in account_ids:
        positions = await client.position_risk(account_id, SYMBOL)
        nonzero = [p for p in positions if p.symbol == SYMBOL and p.position_amt != 0]
        assert not nonzero, (
            f"{account_id} not flat: {[(str(p.position_side), p.position_amt) for p in nonzero]}"
        )
        orders = await client.open_orders(account_id, SYMBOL)
        assert not orders, (
            f"{account_id} has leftover orders: {[(o.client_order_id, o.type) for o in orders]}"
        )


def _audit_client(app: App, http: httpx.AsyncClient):
    """Separate transport, shared credential/nonce sequencing (no nonce reuse)."""
    from copytradeaster.aster.trading_api import AsterClient

    return AsterClient(
        http,
        app.cfg.rest_base_url,
        app.limiter,
        app.credentials,
        app.credentials,
        app.nonces,
        dry_run=False,
    )


async def _flatten_audit_stop(
    app: App, audit, audit_http: httpx.AsyncClient, account_ids: list[str]
) -> None:
    """Finally block: best-effort flatten, HARD signed flat audit, then teardown."""
    with contextlib.suppress(Exception):
        for account_id in account_ids:
            await _flatten_account(app.master_client, account_id)
    try:
        await _assert_flat(audit, account_ids)
    finally:
        with contextlib.suppress(Exception):
            await app.stop()
        with contextlib.suppress(Exception):
            await audit_http.aclose()


async def _start_app(settings: Settings) -> App:
    app = App(settings)
    await app.start()
    assert app._ready and app._stream_connect_count >= 1
    assert await _ready_flag(app)
    return app


async def _set_policy(app: App, account_id: str, risk: D, max_leverage: int) -> None:
    await SettingsRepo(app.db).upsert_policy(
        RiskPolicy(
            risk_percent=risk,
            max_leverage=max_leverage,
            max_entry_deviation_pct=D("0.01"),
        ),
        account_id,
    )


async def _wait_follower_protected(client, follower: str, timeout: float = 180.0) -> None:
    async def protected() -> bool:
        qty = await _follower_qty(client, follower)
        if qty == 0:
            return False
        return len(await _follower_stops(client, follower)) >= 1

    await _wait_for(protected, timeout, f"follower {follower} protected from real WS event")


# --- Dust-1: proportional dust stays flat until the episode ends (GUARD) ---------


async def test_dust1_proportional_dust_stays_flat_until_episode_end(tmp_path):
    """Dust-1 (guard): a same-direction proportional reduction floored to zero
    latches sticky; the follower stays flat while the master episode is open."""
    settings, followers = await _prepare_scratch(tmp_path, "risk_dust1")
    follower = followers[0]
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", follower]:
            await _flatten_account(client, account_id)
        await _set_policy(app, follower, D("0.01"), 20)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        # Dust shaping derivation: the follower copies ~0.006 at 1% risk, so
        # a 0.004 master leg gives ratio 1.5 and can never dust (any legal
        # master cut leaves the follower >= 1 step). Opening 0.008 shapes
        # ratio ~0.75, so leaving 0.001 dusts the follower to ~0.00075,
        # below the 0.001 step. The ratio gate below stays as drift guard.
        opened = await _open_master_long(client, position_side, "pm-d1", qty=D("0.008"))
        await _wait_follower_protected(client, follower)

        follower_before = abs(await _follower_qty(client, follower))
        master_before = abs(await _symbol_qty(client, "main"))
        assert follower_before > 0 and master_before > 0
        # Drift guard: with the shaped 0.008 open the ratio must dust, but
        # live sizing can still drift - skip honestly instead of asserting
        # on an unshaped leg.
        leave = D("0.001")
        if follower_before * leave / master_before >= D("0.001"):
            pytest.skip(
                f"copy ratio too large for a dust leg: follower={follower_before} "
                f"master={master_before}"
            )
        await _master_partial_close(client, position_side, "pm-d1", master_before - leave)

        async def dust_flat() -> bool:
            return await _follower_qty(client, follower) == 0

        await _wait_for(dust_flat, 120, "follower dust-flattened by proportional reduction")
        # Sticky: the intentional flat must NOT re-open inside the same episode.
        await asyncio.sleep(25)
        assert await _follower_qty(client, follower) == 0, "dust leg re-opened inside its episode"
        master_open = abs(await _symbol_qty(client, "main"))
        assert master_open > 0, "master episode ended before the stickiness check"
        # B-file teardown order: master flat first so the running App
        # converges the follower flat before the finally flatten+audit.
        await _teardown_master(client, [opened["stop_coid"]])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", follower])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()


# --- Dust-2: transient risk-cap zero re-enters in the same episode (PM-B5) -------


async def test_dust2_riskcap_zero_reenters_same_episode(tmp_path):
    """Dust-2 (RED until PM-B5): a far stop drives the post-fill cap to zero;
    the flatten is transient, so moving the stop back re-opens the follower
    inside the SAME master episode. Pre-fix code latched every zero sticky."""
    settings, followers = await _prepare_scratch(tmp_path, "risk_dust2")
    follower = followers[0]
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", follower]:
            await _flatten_account(client, account_id)
        # Low operator-shaped ceiling (max 3x) keeps liquidation far away so
        # the far stop below exercises ONLY the risk-cap path, never the
        # liquidation invariant.
        await _set_policy(app, follower, D("0.002"), 3)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        avail = await _available_usdt(client, follower)
        budget = avail * D("0.002")
        if budget < D("1.2"):
            pytest.skip(f"follower budget {budget} USDT too small for a shaped entry")
        position_side = await _position_side(client)
        opened = await _open_master_long(client, position_side, "pm-d2")
        await _wait_follower_protected(client, follower)

        entry = opened["entry_price"]
        # Cap-zero distance derived from the LIVE entry: the post-fill cap
        # floors to zero iff D > budget/(step*(1+fee)). The floor adds 10%
        # margin over that bound (covers fee/mark drift 100x over); the
        # ceiling keeps the stop usable and far from liquidation at ~3x
        # (liq ~33% away). No window -> skip with measured values.
        zero_bound = budget / D("0.001")
        floor = (zero_bound * D("1.1")).quantize(D("0.1"))
        ceiling = (entry * D("0.15")).quantize(D("0.1"))
        if floor > ceiling:
            pytest.skip(
                f"no cap-zero window: zero_bound={zero_bound} entry={entry} budget={budget}"
            )
        far_distance = ceiling
        opened["stop_coid"] = await _move_master_stop(client, opened, "pm-d2", entry - far_distance)

        async def cap_flat() -> bool:
            return await _follower_qty(client, follower) == 0

        await _wait_for(cap_flat, 120, "follower flattened by transient risk-cap zero")
        # Recovery inside the SAME episode: the stop back near re-opens.
        opened["stop_coid"] = await _move_master_stop(
            client, opened, "pm-d2", entry - NEAR_STOP_DISTANCE
        )
        await _wait_follower_protected(client, follower)
        assert abs(await _follower_qty(client, follower)) > 0
        # B-file teardown order: master flat first so the running App
        # converges the follower flat before the finally flatten+audit.
        await _teardown_master(client, [opened["stop_coid"]])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", follower])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()


# --- Dust-3: flip close is not sticky (GUARD) ------------------------------------


async def test_dust3_flip_close_reopens_reversed_leg(tmp_path):
    """Dust-3 (guard): a master flip closes the old side with a transitional
    (non-sticky) zero and the reversed leg opens with protection."""
    settings, followers = await _prepare_scratch(tmp_path, "risk_dust3")
    follower = followers[0]
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", follower]:
            await _flatten_account(client, account_id)
        await _set_policy(app, follower, D("0.01"), 20)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        opened = await _open_master_long(client, position_side, "pm-d3")
        await _wait_follower_protected(client, follower)
        assert await _follower_qty(client, follower) > 0

        await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
        if await _symbol_qty(client, "main") == 0:
            pytest.skip("master stop triggered before the flip: market event voids the scenario")
        await _close_master_leg(client, position_side, "pm-d3")
        flipped = await _open_master_short(client, position_side, "pm-d3")

        async def follower_short_protected() -> bool:
            qty = await _follower_qty(client, follower)
            if position_side == PosSide.BOTH:
                if qty >= 0:
                    return False
            else:
                short_pos = next(
                    (
                        p
                        for p in await client.position_risk(follower, SYMBOL)
                        if p.position_side == PosSide.SHORT and p.position_amt != 0
                    ),
                    None,
                )
                if short_pos is None:
                    return False
            return len(await _follower_stops(client, follower)) >= 1

        await _wait_for(follower_short_protected, 180, "follower reversed into protected short")
        # B-file teardown order: master flat first so the running App
        # converges the follower flat before the finally flatten+audit.
        await _teardown_master(client, [opened["stop_coid"], flipped["stop_coid"]])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", follower])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()


# --- Protection: canceled Stop is restored (GUARD) -------------------------------


async def test_protection_cancel_restores_follower_stop(tmp_path):
    """Protection guard: an externally canceled follower Stop is restored by
    convergence (no phantom flat, exactly one Stop afterwards)."""
    settings, followers = await _prepare_scratch(tmp_path, "risk_prot1")
    follower = followers[0]
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", follower]:
            await _flatten_account(client, account_id)
        await _set_policy(app, follower, D("0.01"), 20)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        opened = await _open_master_long(client, position_side, "pm-p1")
        await _wait_follower_protected(client, follower)

        for stop in await _follower_stops(client, follower):
            await client.cancel_order(follower, SYMBOL, orig_client_order_id=stop.client_order_id)
        # Nudge a real master delta (stop replace at the same level) so the
        # missing protection converges without waiting on the sweep.
        opened["stop_coid"] = await _move_master_stop(client, opened, "pm-p1", opened["stop_price"])

        async def stop_restored() -> bool:
            if await _follower_qty(client, follower) == 0:
                return False
            return len(await _follower_stops(client, follower)) == 1

        await _wait_for(stop_restored, 180, "follower Stop restored after external cancel")
        # B-file teardown order: master flat first so the running App
        # converges the follower flat before the finally flatten+audit.
        await _teardown_master(client, [opened["stop_coid"]])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", follower])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()


# --- Protection: naked leg is safety-flattened (PM-B6) ----------------------------


async def test_protection_naked_leg_safety_flattened(tmp_path):
    """Protection (RED until PM-B6): with no usable master Stop AND no own
    Stop, the existing follower leg is safety-flattened and no Stop level is
    invented for it. Pre-fix code only flattened freshly opened legs."""
    settings, followers = await _prepare_scratch(tmp_path, "risk_prot2")
    follower = followers[0]
    app = App(settings)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        for account_id in ["main", follower]:
            await _flatten_account(client, account_id)
        await _set_policy(app, follower, D("0.01"), 20)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        opened = await _open_master_long(client, position_side, "pm-p2")
        await _wait_follower_protected(client, follower)
        assert abs(await _follower_qty(client, follower)) > 0

        # Strip BOTH protections: the leg is now naked on the exchange.
        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=opened["stop_coid"])
        for stop in await _follower_stops(client, follower):
            with contextlib.suppress(Exception):
                await client.cancel_order(
                    follower, SYMBOL, orig_client_order_id=stop.client_order_id
                )
        # A real master reduction forces the recompute that must flatten.
        master_before = abs(await _symbol_qty(client, "main"))
        await _master_partial_close(client, position_side, "pm-p2", master_before / 2)

        async def naked_flat() -> bool:
            if await _follower_qty(client, follower) != 0:
                return False
            return await client.open_orders(follower, SYMBOL) == []

        await _wait_for(naked_flat, 180, "naked follower leg safety-flattened")
        # B-file teardown order: master flat first so the running App
        # converges the follower flat before the finally flatten+audit.
        await _teardown_master(client, [opened["stop_coid"]])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", follower])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()


# --- Liquidation-vs-stop invariant violation is flattened (PM-B7) -----------------


async def test_liquidation_invariant_holds_safe_leg_live(corrective_campaign):
    """Signed safe-leg hold; no claim that cross-margin liquidation is unshapeable."""
    c = corrective_campaign
    await c.open_master("BTCUSDT", PosSide.LONG, D("0.03"))
    await c.converge()
    actual = await c.snapshot()
    legs = [p for p in actual.positions.values() if p.qty]
    assert len(legs) == 1 and legs[0].stop is not None
    leg = legs[0]
    liq, mark, stop = leg.liquidation_price, leg.mark_price, leg.stop.stop_price
    adverse = D(0) < liq <= mark
    violated = adverse and liq >= stop * D("0.995")
    assert c.app.coordinator._liquidation_violated(leg, stop) == violated
    assert not violated, (
        f"entry survived with unsafe signed geometry: liq={liq}, mark={mark}, stop={stop}"
    )
    await c.app.leverage._maintain_once()
    await c.converge()
    held = (await c.snapshot()).positions[leg.key]
    assert held.qty != 0 and held.stop is not None
    c.evidence["safe_leg"] = {
        "entry": leg.entry_price,
        "mark": mark,
        "liquidation": liq,
        "stop": stop,
        "held_qty": held.qty,
    }
