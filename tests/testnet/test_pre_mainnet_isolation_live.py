"""Package D: failure-isolation live matrix (pre-mainnet, REAL testnet only).

Live-only qualification that one follower's failure never blocks the rest,
over the REAL Aster Testnet (``fapi.asterdex-testnet.com``) with real
``wallet.txt`` accounts (main + 3 REAL followers ``copyc1``/``copyc2``/
``copyc3``). Every test boots the PRODUCTION App on a scratch DB (seeded from
``data/copytrade.sqlite3`` accounts+credentials, never the prod file itself),
drives the REAL master user stream (``fstream.asterdex-testnet.com``) and REAL
signed REST, injects faults ONLY by wrapping the real ``AsterClient`` for a
single follower account, and ends with heal + full convergence + best-effort
flatten + an independent signed flat audit (``position_risk`` flat +
``open_orders`` empty) in ``finally``.

Row coverage (failure-isolation qualification rows) -> test node:
- F1 ``test_isolation_follower2_transport_fault``: ``copyc2`` order transport
  raises a 429-style ``AsterError`` (wrapper matching on its account_id only)
  -> ``copyc1``/``copyc3`` converge on every master cycle (open AND close)
  while ``copyc2`` stays flat with FAILED/UNKNOWN journal rows or a degraded
  flag naming it; then heal, full 3-follower convergence, flat audit.
- F2 ``test_isolation_follower3_permission_fault``: ``copyc3`` reads+writes
  raise a bad-key ``AsterError`` (-2015, that follower only) -> ``copyc1``/
  ``copyc2`` converge, ``copyc3`` untouched (verified via the independent
  audit client); then heal, full convergence, flat audit.
- F3 ``test_isolation_insufficient_margin``: copyc1's exchange leverage is
  really set to its bracket maximum while its SET_LEVERAGE raises a
  deterministic AsterError (wrapper on its own runtime client only), so the
  entry target (stop-capped far below the maximum) forces a NEEDED provision
  that fails -> copyc1 stays flat with FAILED SET rows + degraded evidence
  (executor denial) while copyc2/copyc3 converge; heal proves fresh-cycle
  convergence, then a steady-state 1x phase proves maintenance never
  upshifts a legitimate 1x back to the ceiling (review 636dfff P1);
  leverage restored, flat audit.

HEDGE-aware: main is HEDGE, so LONG/SHORT legs exist alongside zero legs;
quantity reads sum legs and only nonzero legs count toward convergence.
Settle/stability gates (``_wait_stable``) prove quiescence across consecutive
polls; no single-shot asserts against the LIVE App. No reconnect injection is
used here (no W-row), so no forced ``close_listen_key`` appears. No direct
executor submits occur anywhere in this file: every ``ExecutionAction`` flows
from the production Coordinator with its per-leg ``sync_basis`` revision, so
the deterministic-COID lesson holds by construction (each master order still
carries a unique ``time.time_ns()`` client order id).

Offline/fake NEVER closes a row: no mocks, no synthetic fills, no shadow
execution. NOBODY runs live pytest from shared-state checkouts: these tests
are implementation-only until the pre-mainnet run.
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
from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.storage.database import Database

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
QTY = D("0.002")
PROD_DB = Path("data/copytrade.sqlite3")
_SEED_TABLES = ("accounts", "credentials", "copy_settings", "agent_validations")
WANT_FOLLOWERS = ("copyc1", "copyc2", "copyc3")


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


def _seed_scratch_named(db_path: Path) -> list[str]:
    """Copy accounts+credentials into a fresh scratch DB; keep main + copyc1/2/3.

    Same shape as the master-matrix seeder, but pins the three named REAL
    followers this matrix isolates instead of the first K rows. The ciphertext
    blobs decrypt under the same CT_ENCRYPTION_KEY, so the production App
    boots against REAL keys without ever opening the prod file.
    """
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
        have = {str(row[0]) for row in rows}
        missing = [fid for fid in WANT_FOLLOWERS if fid not in have]
        if missing:
            pytest.skip(f"need imported REAL followers {missing} - run import-wallet first")
        wanted = ",".join("?" for _ in ("main", *WANT_FOLLOWERS))
        dst.execute(
            f"UPDATE accounts SET enabled = 0 WHERE id NOT IN ({wanted})", ("main", *WANT_FOLLOWERS)
        )
        dst.commit()
        return list(WANT_FOLLOWERS)
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
    followers = _seed_scratch_named(Path(str(settings.db_path)))
    return settings, followers


# --- timeout diagnostics -----------------------------------------------------------


_DIAG_CTX: dict = {"app": None, "audit": None, "notified": None, "followers": ()}


def _bind_diag(app, audit, notified, followers) -> None:
    """Point timeout snapshots at this test's live objects (serial runs only)."""
    _DIAG_CTX.update(app=app, audit=audit, notified=notified, followers=tuple(followers))


async def _diag_snapshot() -> str:
    """Best-effort state dump for timeout errors; never raises, timeout-path only."""
    try:
        app = _DIAG_CTX.get("app")
        audit = _DIAG_CTX.get("audit")
        notified = _DIAG_CTX.get("notified") or []
        followers = _DIAG_CTX.get("followers") or ()
        if app is None or audit is None:
            return "diag-unbound"
        parts: list[str] = []
        try:
            parts.append(f"ready={await app.settings.get_flag('ready')}")
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            parts.append(f"ready=? ({type(exc).__name__})")
        try:
            parts.append(f"entry_admitted={bool(app._master_entry_admitted())}")
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            parts.append(f"entry_admitted=? ({type(exc).__name__})")
        for account_id in ["main", *followers]:
            try:
                qty: object = await _symbol_qty(audit, account_id)
            except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
                qty = f"? ({type(exc).__name__})"
            extra: list[str] = []
            if account_id == "main":
                try:
                    extra.append(f"orders={len(await audit.open_orders('main', SYMBOL))}")
                except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
                    extra.append(f"orders=? ({type(exc).__name__})")
            else:
                try:
                    extra.append(f"stops={len(await _follower_stops(audit, account_id))}")
                except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
                    extra.append(f"stops=? ({type(exc).__name__})")
                try:
                    lev = app.leverage.confirmed(account_id, SYMBOL)
                except Exception:  # noqa: BLE001 - diagnostics must never raise
                    lev = None
                if lev is None:
                    try:
                        rows = await audit.position_risk(account_id, SYMBOL)
                        lev = next(
                            (r.leverage for r in rows if r.symbol == SYMBOL and r.leverage),
                            "?",
                        )
                    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
                        lev = f"? ({type(exc).__name__})"
                extra.append(f"leverage={lev}")
            parts.append(f"{account_id}:qty={qty} {' '.join(extra)}")
        tail = [
            f"{getattr(e, 'kind', '?')}|{getattr(e, 'event_key', '?')}"
            for e in notified
            if getattr(e, "account_id", None) in set(followers)
        ][-8:]
        parts.append(f"records=[{'; '.join(tail) if tail else 'none'}]")
        return " ".join(parts)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return f"diag-unavailable ({type(exc).__name__})"


async def _wait_for(predicate, timeout: float, what: str, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}; diag: {await _diag_snapshot()}")


async def _ready_flag(app: App) -> bool:
    return await app.settings.get_flag("ready") == "true"


# --- live trading helpers (all REAL signed REST) --------------------------------


async def _position_side(client, account_id: str = "main") -> PosSide:
    hedge = await client.get_position_mode(account_id)
    return PosSide.LONG if hedge else PosSide.BOTH


async def _symbol_qty(client, account_id: str) -> D:
    """HEDGE-aware total: sums LONG/SHORT legs, ignores zero legs."""
    total = D("0")
    for position in await client.position_risk(account_id, SYMBOL):
        if position.symbol == SYMBOL:
            total += position.position_amt
    return total


async def _follower_stops(client, account_id: str) -> list:
    return [
        order
        for order in await client.open_orders(account_id, SYMBOL)
        if order.client_order_id.startswith("ct") and order.type.startswith("STOP")
    ]


async def _open_master_long(client, position_side: PosSide, tag: str) -> dict:
    """REAL master MARKET open + protective STOP_MARKET. Returns ids for cleanup.

    The engine only copies a PROTECTED master position, so the stop rests from
    entry on (master-matrix mirror: SELL STOP_MARKET below entry, reduce_only
    only in one-way mode, MARK_PRICE working type, unique COID, 2%-of-mark
    offset floor so it can never trigger on placement).
    """
    stamp = time.time_ns()
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=QTY,
            position_side=position_side,
            new_client_order_id=f"{tag}-open-{stamp}"[:36],
            new_order_resp_type="RESULT",
        ),
    )
    position = next(p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0)
    mark = next(
        (p.mark_price for p in await client.position_risk("main", SYMBOL) if p.mark_price > 0),
        position.entry_price,
    )
    stop_level = (
        position.entry_price - max(D("1200"), (mark * D("0.02")).quantize(D("0.1")))
    ).quantize(D("0.1"))
    hedge = position_side != PosSide.BOTH
    master_stop = await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=QTY,
            stop_price=stop_level,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"{tag}-stop-{stamp}"[:36],
        ),
    )
    return {"position_side": position_side, "stop_coid": master_stop.client_order_id}


async def _cancel_coid(client, account_id: str, coid: str) -> None:
    with contextlib.suppress(Exception):
        await client.cancel_order(account_id, SYMBOL, orig_client_order_id=coid)


async def _close_master_long(
    client, position_side: PosSide, tag: str, stop_coid: str | None = None
) -> None:
    if stop_coid is not None:
        await _cancel_coid(client, "main", stop_coid)
    qty = abs(await _symbol_qty(client, "main"))
    if qty == 0:
        return
    hedge = position_side != PosSide.BOTH
    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=qty,
            reduce_only=not hedge,
            position_side=position_side,
            new_client_order_id=f"{tag}-close-{time.time_ns()}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


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
                        new_client_order_id=f"pm-flat-{account_id}-{time.time_ns()}"[:36],
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


async def _wait_stable(
    check, *, polls: int = 3, interval: float = 1.5, timeout: float = 60.0, what: str
) -> None:
    """Poll an async ``check`` until it holds across N consecutive polls.

    ``check`` returns ``(ok, detail)``; on timeout the error names the last
    failing sub-condition(s) with values. Single-shot asserts against a LIVE
    App (background sweep/reconcile loops) are inherently racy; stability
    across consecutive polls is the only meaningful quiescence proof.
    """
    deadline = time.monotonic() + timeout
    stable = 0
    last_detail = ""
    while time.monotonic() < deadline:
        ok, detail = await check()
        if ok:
            stable += 1
            if stable >= polls:
                return
        else:
            stable = 0
            last_detail = detail
        await asyncio.sleep(interval)
    raise TimeoutError(
        f"timed out waiting for stable {what}; last failing: {last_detail}; "
        f"diag: {await _diag_snapshot()}"
    )


async def _wait_followers_protected(app: App, followers: list[str], timeout: float = 120.0) -> None:
    async def protected() -> bool:
        for account_id in followers:
            if await _symbol_qty(app.master_client, account_id) == 0:
                return False
            if len(await _follower_stops(app.master_client, account_id)) != 1:
                return False
        return True

    await _wait_for(protected, timeout, "followers protected from real WS event")


async def _wait_followers_flat(app: App, followers: list[str], timeout: float = 120.0) -> None:
    async def flat() -> bool:
        for account_id in followers:
            if await _symbol_qty(app.master_client, account_id) != 0:
                return False
        return True

    await _wait_for(flat, timeout, "followers flat after master close")


async def _failure_evidence(app: App, account_id: str) -> tuple[bool, str]:
    """Journal FAILED/UNKNOWN rows or a degraded flag naming the account."""
    rows = await app.db.fetchall(
        "SELECT status FROM execution_commands WHERE account_id = ? "
        "AND status IN ('FAILED','UNKNOWN')",
        (account_id,),
    )
    degraded = await app.settings.get_flag("degraded_reason") or ""
    if rows or account_id in degraded:
        return True, f"journal-failures={len(rows)} degraded={degraded!r}"
    return False, f"no journal failures and degraded={degraded!r}"


# --- F1: follower-2 transport/429 fault --------------------------------------------


async def test_isolation_follower2_transport_fault(tmp_path):
    """F1: copyc2 429-isolated; copyc1/copyc3 converge every master cycle.

    With copyc2's order transport raising a 429-style error (wrapper matching
    on its account_id only), the master open cycle converges copyc1/copyc3
    while copyc2 stays flat with failure evidence; the master close cycle
    converges them back to flat. Then heal (restore the real transport),
    reconcile, prove full 3-follower convergence on a fresh cycle, and audit
    flat.
    """
    settings, followers = await _prepare_scratch(tmp_path, "iso_d1")
    assert followers == ["copyc1", "copyc2", "copyc3"]
    notified: list = []

    async def _record(event) -> None:
        notified.append(event)

    app = App(settings, notifier=_record)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        _bind_diag(app, audit, notified, followers)
        for account_id in ["main", *followers]:
            await _flatten_account(client, account_id)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        # Seam: followers trade through their OWN FollowerRuntime.client
        # (trading/coordinator.py; one AsterClient per runtime via
        # App._make_client), never app.master_client. Fault copyc2 there.
        runtime_c2 = app.follower_registry.get("copyc2")
        assert runtime_c2 is not None, "copyc2 runtime missing from follower_registry"
        target_c2 = runtime_c2.client
        original_place = target_c2.place_order
        original_cancel = target_c2.cancel_order

        async def _place_429(account_id: str, req):
            if account_id == "copyc2":
                raise AsterError(-1003, "injected-429(copyc2)", 429)
            return await original_place(account_id, req)

        async def _cancel_429(account_id: str, symbol: str, **kwargs):
            if account_id == "copyc2":
                raise AsterError(-1003, "injected-429(copyc2)", 429)
            return await original_cancel(account_id, symbol, **kwargs)

        target_c2.place_order = _place_429  # type: ignore[method-assign]
        target_c2.cancel_order = _cancel_429  # type: ignore[method-assign]
        try:
            # Fault cycle 1: master open -> healthy followers converge.
            opened_d1a = await _open_master_long(client, position_side, "pm-d1a")
            await _wait_followers_protected(app, ["copyc1", "copyc3"])

            async def _f1_open_isolated() -> tuple[bool, str]:
                failures = []
                for account_id in ("copyc1", "copyc3"):
                    if await _symbol_qty(audit, account_id) == 0:
                        failures.append(f"{account_id} flat under fault")
                    if len(await _follower_stops(audit, account_id)) != 1:
                        failures.append(f"{account_id} stops != 1 under fault")
                if await _symbol_qty(audit, "copyc2") != 0:
                    failures.append("copyc2 opened despite 429 fault")
                ok, detail = await _failure_evidence(app, "copyc2")
                if not ok:
                    failures.append(f"no failure evidence for copyc2 ({detail})")
                return (not failures, "; ".join(failures))

            await _wait_stable(_f1_open_isolated, what="F1 open-cycle isolation")

            # Fault cycle 2: master close -> healthy followers converge to flat.
            await _close_master_long(client, position_side, "pm-d1a", opened_d1a["stop_coid"])
            await _wait_followers_flat(app, ["copyc1", "copyc3"])

            async def _f1_close_isolated() -> tuple[bool, str]:
                failures = []
                for account_id in ("copyc1", "copyc3"):
                    if await _symbol_qty(audit, account_id) != 0:
                        failures.append(f"{account_id} not flat after close under fault")
                if await _symbol_qty(audit, "copyc2") != 0:
                    failures.append("copyc2 non-flat despite never opening")
                return (not failures, "; ".join(failures))

            await _wait_stable(_f1_close_isolated, what="F1 close-cycle isolation")
        finally:
            target_c2.place_order = original_place  # type: ignore[method-assign]
            target_c2.cancel_order = original_cancel  # type: ignore[method-assign]

        # Heal: fresh master cycle converges ALL THREE followers.
        await app.reconcile_follower("copyc2", reason="pm-d1-heal")
        opened_d1b = await _open_master_long(client, position_side, "pm-d1b")
        await _wait_followers_protected(app, followers)

        async def _f1_healed() -> tuple[bool, str]:
            failures = []
            for account_id in followers:
                if await _symbol_qty(audit, account_id) == 0:
                    failures.append(f"{account_id} flat after heal")
                if len(await _follower_stops(audit, account_id)) != 1:
                    failures.append(f"{account_id} stops != 1 after heal")
            return (not failures, "; ".join(failures))

        await _wait_stable(_f1_healed, what="F1 full convergence after heal")

        await _close_master_long(client, position_side, "pm-d1b", opened_d1b["stop_coid"])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", *followers])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()
        with contextlib.suppress(Exception):
            await app.http.aclose()
        with contextlib.suppress(Exception):
            await app.db.close()


# --- F2: follower-3 permission fault -------------------------------------------------


async def test_isolation_follower3_permission_fault(tmp_path):
    """F2: copyc3 bad-key-isolated; copyc1/copyc2 converge, isolation holds.

    With copyc3's reads+writes raising a bad-key error (-2015, wrapper matching
    on its account_id only), the master open cycle converges copyc1/copyc2
    while copyc3 stays untouched (verified through the independent audit
    client, which bypasses the injected wrapper). Then heal, reconcile, prove
    full 3-follower convergence, and audit flat.
    """
    settings, followers = await _prepare_scratch(tmp_path, "iso_d2")
    assert followers == ["copyc1", "copyc2", "copyc3"]
    notified: list = []

    async def _record(event) -> None:
        notified.append(event)

    app = App(settings, notifier=_record)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        _bind_diag(app, audit, notified, followers)
        for account_id in ["main", *followers]:
            await _flatten_account(client, account_id)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")

        position_side = await _position_side(client)
        # Seam: followers trade through their OWN FollowerRuntime.client
        # (trading/coordinator.py; one AsterClient per runtime via
        # App._make_client), never app.master_client. Fault copyc3 there.
        runtime_c3 = app.follower_registry.get("copyc3")
        assert runtime_c3 is not None, "copyc3 runtime missing from follower_registry"
        target_c3 = runtime_c3.client
        original_place = target_c3.place_order
        original_cancel = target_c3.cancel_order
        original_risk = target_c3.position_risk
        original_open = target_c3.open_orders

        async def _place_perm(account_id: str, req):
            if account_id == "copyc3":
                raise AsterError(-2015, "injected-permission(copyc3)", 401)
            return await original_place(account_id, req)

        async def _cancel_perm(account_id: str, symbol: str, **kwargs):
            if account_id == "copyc3":
                raise AsterError(-2015, "injected-permission(copyc3)", 401)
            return await original_cancel(account_id, symbol, **kwargs)

        async def _risk_perm(account_id: str, *args, **kwargs):
            if account_id == "copyc3":
                raise AsterError(-2015, "injected-permission(copyc3)", 401)
            return await original_risk(account_id, *args, **kwargs)

        async def _open_perm(account_id: str, *args, **kwargs):
            if account_id == "copyc3":
                raise AsterError(-2015, "injected-permission(copyc3)", 401)
            return await original_open(account_id, *args, **kwargs)

        target_c3.place_order = _place_perm  # type: ignore[method-assign]
        target_c3.cancel_order = _cancel_perm  # type: ignore[method-assign]
        target_c3.position_risk = _risk_perm  # type: ignore[method-assign]
        target_c3.open_orders = _open_perm  # type: ignore[method-assign]
        try:
            opened_d2a = await _open_master_long(client, position_side, "pm-d2a")
            await _wait_followers_protected(app, ["copyc1", "copyc2"])

            async def _f2_isolated() -> tuple[bool, str]:
                failures = []
                for account_id in ("copyc1", "copyc2"):
                    if await _symbol_qty(audit, account_id) == 0:
                        failures.append(f"{account_id} flat under fault")
                    if len(await _follower_stops(audit, account_id)) != 1:
                        failures.append(f"{account_id} stops != 1 under fault")
                if await _symbol_qty(audit, "copyc3") != 0:
                    failures.append("copyc3 opened despite permission fault")
                if await audit.open_orders("copyc3", SYMBOL):
                    failures.append("copyc3 holds orders despite permission fault")
                ok, detail = await _failure_evidence(app, "copyc3")
                if not ok:
                    failures.append(f"no failure evidence for copyc3 ({detail})")
                return (not failures, "; ".join(failures))

            await _wait_stable(_f2_isolated, what="F2 permission isolation")

            await _close_master_long(client, position_side, "pm-d2a", opened_d2a["stop_coid"])
            await _wait_followers_flat(app, ["copyc1", "copyc2"])
        finally:
            target_c3.place_order = original_place  # type: ignore[method-assign]
            target_c3.cancel_order = original_cancel  # type: ignore[method-assign]
            target_c3.position_risk = original_risk  # type: ignore[method-assign]
            target_c3.open_orders = original_open  # type: ignore[method-assign]

        # Heal: fresh master cycle converges ALL THREE followers.
        await app.reconcile_follower("copyc3", reason="pm-d2-heal")
        opened_d2b = await _open_master_long(client, position_side, "pm-d2b")
        await _wait_followers_protected(app, followers)

        async def _f2_healed() -> tuple[bool, str]:
            failures = []
            for account_id in followers:
                if await _symbol_qty(audit, account_id) == 0:
                    failures.append(f"{account_id} flat after heal")
                if len(await _follower_stops(audit, account_id)) != 1:
                    failures.append(f"{account_id} stops != 1 after heal")
            return (not failures, "; ".join(failures))

        await _wait_stable(_f2_healed, what="F2 full convergence after heal")

        await _close_master_long(client, position_side, "pm-d2b", opened_d2b["stop_coid"])
    finally:
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", *followers])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()
        with contextlib.suppress(Exception):
            await app.http.aclose()
        with contextlib.suppress(Exception):
            await app.db.close()


def _copy_skip_records(events: list, account_id: str) -> list[str]:
    """ALL copy.skip keys for the account/symbol, any reason (no filtering)."""
    hits = []
    for event in events:
        if getattr(event, "kind", None) != "copy.skip":
            continue
        if getattr(event, "account_id", None) != account_id:
            continue
        key = str(getattr(event, "event_key", ""))
        if f":{SYMBOL}:" not in key:
            continue
        hits.append(key)
    return hits


def _account_records_dump(events: list, account_id: str) -> str:
    """Every captured record for the account, any kind (no filtering)."""
    rows = [
        f"{getattr(e, 'kind', '?')}|{getattr(e, 'event_key', '?')}"
        for e in events
        if getattr(e, "account_id", None) == account_id
    ]
    return "; ".join(rows) if rows else "no records at all"


# --- F3: leverage-denial follower -------------------------------------------------------


async def test_isolation_insufficient_margin(tmp_path):
    """F3: copyc1 leverage-denied; a NEEDED provision fails, copy withheld pre-send.

    Effective-is-truth (review 636dfff P1): the control plane confirms the
    exchange-reported configuration instead of disagreeing with 1x, so the
    old "1x marker" denial premise is dead. Honest denial now needs a
    provision the entry genuinely requires: copyc1's exchange leverage is
    really set to its bracket maximum while its runtime client's
    SET_LEVERAGE raises a deterministic AsterError (wrapper matching on
    copyc1's own FollowerRuntime.client only). The entry target is
    stop-capped far below the maximum, so the planner emits SET_LEVERAGE,
    the POST fails deterministically, dependents stay BLOCKED, and copyc1
    stays flat with FAILED SET rows + degraded evidence (executor denial)
    while copyc2/copyc3 converge each master cycle. App stays READY.
    Heal restores the wrapper and proves full convergence on a fresh master
    cycle; a steady-state 1x phase then proves maintenance never upshifts a
    legitimate 1x back to the ceiling (zero set_leverage POSTs while copyc1
    converges at 1x). Teardown restores baseline leverage and audits flat.
    """
    settings, followers = await _prepare_scratch(tmp_path, "iso_d3")
    assert followers == ["copyc1", "copyc2", "copyc3"]
    notified: list = []

    async def _record(event) -> None:
        notified.append(event)

    app = App(settings, notifier=_record)
    started = False
    audit_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    baseline_leverage: int | None = None
    try:
        await app.start()
        started = True
        client = app.master_client
        audit = _audit_client(app, audit_http)
        _bind_diag(app, audit, notified, followers)
        for account_id in ["main", *followers]:
            await _flatten_account(client, account_id)
        await _wait_for(lambda: _ready_flag(app), 90, "READY after pre-flatten")
        # Fault-window observability: mark the notifier stream and count every
        # coordinator recompute attempt per follower (delegating wrapper).
        window_start = len(notified)
        recompute_attempts: dict[str, int] = {}
        original_recompute = app.coordinator._recompute_follower

        async def _counting_recompute(fr, delta, *args, **kwargs):
            recompute_attempts[fr.account_id] = recompute_attempts.get(fr.account_id, 0) + 1
            return await original_recompute(fr, delta, *args, **kwargs)

        app.coordinator._recompute_follower = _counting_recompute  # type: ignore[method-assign]

        position_side = await _position_side(client)
        for row in await client.position_risk("copyc1", SYMBOL):
            if row.symbol == SYMBOL and row.leverage:
                baseline_leverage = int(row.leverage)
                break
        from copytradeaster.trading.leverage import _max_initial_leverage

        bracket_max = _max_initial_leverage(await client.leverage_bracket("copyc1", SYMBOL))
        if bracket_max <= 30:
            pytest.skip(f"bracket max {bracket_max} cannot force a leverage transition")
        await client.set_leverage("copyc1", SYMBOL, bracket_max)
        # REAL exchange write to the bracket maximum: the entry target is
        # stop-capped far below it (2% stop -> cap ~28 on BTC), so the
        # planner must emit a NEEDED SET_LEVERAGE transition. Faulting that
        # POST denies the copy at the executor while the control plane
        # stays honest (effective-is-truth: the maximum IS confirmed).
        # Seam: the executor routes SET_LEVERAGE through
        # App._client_for -> FollowerRuntime.client (one AsterClient per
        # runtime via App._make_client). Fault only the POST on copyc1's
        # own client; reads and the global catalog (main's client) stay
        # healthy.
        runtime_c1 = app.follower_registry.get("copyc1")
        assert runtime_c1 is not None, "copyc1 runtime missing from follower_registry"
        target_c1 = runtime_c1.client
        original_setlev = target_c1.set_leverage

        async def _deny_setlev(account_id: str, symbol: str, leverage: int):
            if account_id == "copyc1":
                raise AsterError(-1003, "injected-levdeny(copyc1)", 429)
            return await original_setlev(account_id, symbol, leverage)

        target_c1.set_leverage = _deny_setlev  # type: ignore[method-assign]
        try:
            opened_d3a = await _open_master_long(client, position_side, "pm-d3a")
            await _wait_followers_protected(app, ["copyc2", "copyc3"])

            async def _f3_isolated() -> tuple[bool, str]:
                failures = []
                for account_id in ("copyc2", "copyc3"):
                    if await _symbol_qty(audit, account_id) == 0:
                        failures.append(f"{account_id} flat under leverage fault")
                    if len(await _follower_stops(audit, account_id)) != 1:
                        failures.append(f"{account_id} stops != 1 under leverage fault")
                if await _symbol_qty(audit, "copyc1") != 0:
                    failures.append("copyc1 opened despite leverage denial")
                # Refusal takes exactly one code-honest form (effective-is-truth:
                # the bracket maximum IS confirmed, so no LEVERAGE_NOT_READY
                # skip can occur): executor denial — FAILED SET_LEVERAGE rows
                # plus degraded evidence with zero MARKET-kind sends
                # (dependents stay BLOCKED in memory and send nothing).
                window = notified[window_start:]
                skips = _copy_skip_records(window, "copyc1")
                attempts = recompute_attempts.get("copyc1", 0)
                rows = await app.db.fetchall(
                    "SELECT kind, status FROM execution_commands WHERE account_id = ?",
                    ("copyc1",),
                )
                kinds = [(str(r["kind"]), str(r["status"])) for r in rows]
                failed_setlev = [k for k in kinds if k[0] == "SET_LEVERAGE" and k[1] == "FAILED"]
                market_rows = [
                    k
                    for k in kinds
                    if k[0] in ("MARKET_OPEN", "MARKET_CLOSE", "PLACE_STOP", "PLACE_TP")
                ]
                degraded_reason = await app.settings.get_flag("degraded_reason") or ""
                exec_deg = [
                    str(getattr(e, "event_key", ""))
                    for e in window
                    if getattr(e, "kind", None) == "execution.degraded"
                    and getattr(e, "account_id", None) == "copyc1"
                ]
                if not (
                    len(failed_setlev) >= 1
                    and ("copyc1" in degraded_reason or exec_deg)
                    and not market_rows
                ):
                    failures.append(
                        "copyc1 shows no executor denial "
                        f"(skips={len(skips)} rows={kinds} degraded={degraded_reason!r} "
                        f"recompute_attempts={attempts}; "
                        f"copyc1 records: {_account_records_dump(window, 'copyc1')})"
                    )
                if not await _ready_flag(app):
                    failures.append("app not READY/healthy during fault window")
                detail = (
                    f"executor-denial skips={len(skips)} "
                    f"setlev_fail={len(failed_setlev)} market_rows={len(market_rows)} "
                    f"recompute_attempts={attempts} "
                    f"reasons={[k.split(':')[4] for k in skips if len(k.split(':')) > 4]}"
                )
                return (not failures, "; ".join(failures) if failures else detail)

            await _wait_stable(_f3_isolated, what="F3 leverage-denial isolation")

            # Fault cycle 2: master close -> healthy followers converge to flat.
            await _close_master_long(client, position_side, "pm-d3a", opened_d3a["stop_coid"])
            await _wait_followers_flat(app, ["copyc2", "copyc3"])

            async def _f3_close_isolated() -> tuple[bool, str]:
                failures = []
                for account_id in ("copyc1", "copyc2", "copyc3"):
                    if await _symbol_qty(audit, account_id) != 0:
                        failures.append(f"{account_id} not flat after close under fault")
                return (not failures, "; ".join(failures))

            await _wait_stable(_f3_close_isolated, what="F3 close-cycle isolation")
        finally:
            target_c1.set_leverage = original_setlev  # type: ignore[method-assign]

        # Heal: wrapper restored, so the failed provision succeeds on the next
        # sweep / prerequisite. Prove full 3-follower convergence on a FRESH
        # master cycle: re-entry into a drifted leg would be correctly refused
        # by the ENTRY_DEVIATION gate, so flat copyc1 must never be expected
        # to chase a stale move.
        await app.reconcile_follower("copyc1", reason="pm-d3-heal")
        await _close_master_long(client, position_side, "pm-d3a", opened_d3a["stop_coid"])
        opened_d3b = await _open_master_long(client, position_side, "pm-d3b")
        await _wait_followers_protected(app, followers)

        async def _f3_healed() -> tuple[bool, str]:
            failures = []
            for account_id in followers:
                if await _symbol_qty(audit, account_id) == 0:
                    failures.append(f"{account_id} flat after heal")
                if len(await _follower_stops(audit, account_id)) != 1:
                    failures.append(f"{account_id} stops != 1 after heal")
            return (not failures, "; ".join(failures))

        await _wait_stable(_f3_healed, what="F3 fresh-cycle convergence after heal")

        await _close_master_long(client, position_side, "pm-d3b", opened_d3b["stop_coid"])

        # Steady-state 1x (review 636dfff P1 live pin): park copyc1 at a
        # legitimate 1x and prove it converges there with ZERO set_leverage
        # POSTs — maintenance must confirm the effective value, never
        # upshift it back toward the ceiling (the old clobber sent 20x over
        # a live stop-driven 1x every pass).
        copy_balances = await client.balance("copyc1")
        copy_usdt = next((b for b in copy_balances if b.asset == "USDT"), None)
        if copy_usdt is not None and copy_usdt.available_balance >= D("500"):
            await client.set_leverage("copyc1", SYMBOL, 1)
            setlev_posts: list[int] = []
            counting_setlev = target_c1.set_leverage

            async def _counting_setlev(account_id: str, symbol: str, leverage: int):
                if account_id == "copyc1":
                    setlev_posts.append(leverage)
                return await counting_setlev(account_id, symbol, leverage)

            target_c1.set_leverage = _counting_setlev  # type: ignore[method-assign]
            try:
                opened_d3c = await _open_master_long(client, position_side, "pm-d3c")
                await _wait_followers_protected(app, followers)

                async def _f3_steady() -> tuple[bool, str]:
                    failures = []
                    if await _symbol_qty(audit, "copyc1") == 0:
                        failures.append("copyc1 flat at steady 1x")
                    if setlev_posts:
                        failures.append(f"maintenance upshifted 1x: {setlev_posts}")
                    return (not failures, "; ".join(failures))

                await _wait_stable(_f3_steady, what="F3 steady-state 1x convergence")
                await _close_master_long(client, position_side, "pm-d3c", opened_d3c["stop_coid"])
            finally:
                target_c1.set_leverage = original_setlev  # type: ignore[method-assign]
        else:
            pytest.skip("copyc1 equity too small for steady-state 1x sizing")
    finally:
        with contextlib.suppress(Exception):
            app.coordinator._recompute_follower = original_recompute  # type: ignore[method-assign]
        if baseline_leverage is not None:
            with contextlib.suppress(Exception):
                await app.master_client.set_leverage("copyc1", SYMBOL, baseline_leverage)
        if started:
            await _flatten_audit_stop(app, audit, audit_http, ["main", *followers])
        else:  # pragma: no cover - boot never completed
            with contextlib.suppress(Exception):
                await audit_http.aclose()
        with contextlib.suppress(Exception):
            await app.http.aclose()
        with contextlib.suppress(Exception):
            await app.db.close()
