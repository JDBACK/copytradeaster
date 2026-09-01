"""Signed LIVE leverage control-plane coverage (testnet wallets).

Covers what the offline fake-based suite cannot prove against a real exchange:

1. over-bracket prepare is rejected (-4028) and falls back to the signed
   account bracket maximum;
2. an unready control plane fails closed: entries skip with
   LEVERAGE_NOT_READY, zero order requests reach the exchange, and the live
   background worker provisions readiness;
3. a REALLY-rejected SET_LEVERAGE (-4028) blocks its dependent MARKET_OPEN /
   PLACE_STOP inside the executor while a risk-reducing MARKET_CLOSE in the
   same situation still executes (P0 contract);
4. a REAL leverage downshift on the wallet makes the control plane durably
   unready (observed value disagrees with the candidate), clamping master
   increases until re-provisioning flows them.

Scope notes from live probing:
* the FULL coordinator pipeline cannot emit an exchange-rejected SET: the
  authoritative snapshot heals any RAM divergence before planning (that is
  exactly fix #25/#26 quality), so scenario 3 proves the P0 gate at the
  executor boundary with genuine exchange rejection;
* Aster returns LONG/SHORT positionRisk rows even in ONE_WAY mode; helpers
  poll instead of assuming instant consistency.

Isolation: trading repos live in a PER-TEST temporary SQLite database; the
shared local wallet database only supplies credentials and signing nonces.
Master fills are placed as REAL signed orders and registered through the
established harness convention of feeding one ORDER_TRADE_UPDATE whose
price/quantity come from the real fill.

The only seeded state is control-plane divergence produced by REAL wallet
operations (direct signed set_leverage / over-bracket candidates); every
network interaction is real testnet traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import tempfile
import time
from collections.abc import Callable
from decimal import ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import (
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PosSide,
    RiskPolicy,
    Side,
    SkipReason,
    SymbolFilters,
)
from copytradeaster.master.projector import MasterDelta, MasterProjector
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CommandRepo,
    CopyTradeRepo,
    MasterEventRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.coordinator import Coordinator, FollowerRuntime
from copytradeaster.trading.executor import Executor
from copytradeaster.trading.leverage import (
    CandidateLeverage,
    LeverageManager,
)
from copytradeaster.trading.planner import _order_payload

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
FOLLOWER = "copyc1"
QTY = D("0.002")
RESTORE_LEVERAGE = 200

_event_ids = itertools.count(int(time.time() * 1000))


def _snap(value: D, tick: D) -> D:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


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
    """Signed order with outcome-unknown query recovery (never blind-retry)."""
    for attempt in range(3):
        try:
            return await client.place_order(account_id, req)
        except Exception as exc:  # noqa: BLE001 - classify unknown outcomes only
            if not _unknown_outcome(exc) or attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
            found = await client.query_order(
                account_id, req.symbol, orig_client_order_id=req.new_client_order_id
            )
            if found is not None:
                return found
    raise RuntimeError("unreachable")


async def _flatten_strict(client: AsterClient, account_id: str, prefix: str) -> None:
    """Cancel everything and close any residue; retry until verifiably flat."""
    for attempt in range(6):
        ts = int(time.time() * 1000)
        for order in await client.open_orders(account_id, symbol=SYMBOL):
            with contextlib.suppress(Exception):
                await client.cancel_order(
                    account_id, SYMBOL, orig_client_order_id=order.client_order_id
                )
        rows = await client.position_risk(account_id, SYMBOL)
        residue = sum((abs(p.position_amt) for p in rows), D(0))
        if residue == 0 and not await client.open_orders(account_id, symbol=SYMBOL):
            return
        for index, p in enumerate(rows):
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
                        reduce_only=True,
                        position_side=p.position_side,
                        new_order_resp_type="RESULT",
                        new_client_order_id=f"{prefix}-{ts}-{index}-{attempt}"[:36],
                    ),
                )
        await asyncio.sleep(0.7)
    raise AssertionError(f"{account_id} failed to flatten on {SYMBOL}")


async def _wait_flag(flag: Callable[[], bool], timeout_s: float, what: str) -> None:
    """Poll a synchronous readiness flag; exchange reads lag fills."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if flag():
            return
        await asyncio.sleep(0.4)
    raise TimeoutError(f"timed out waiting for {what}")


async def _wait_qty(
    client: AsterClient,
    account_id: str,
    matches: Callable[[D], bool],
    timeout_s: float,
    what: str,
) -> D:
    """Poll signed positionRisk until the quantity satisfies ``matches``."""
    deadline = time.monotonic() + timeout_s
    qty = D(0)
    while time.monotonic() < deadline:
        qty = await _position_qty(client, account_id)
        if matches(qty):
            return qty
        await asyncio.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {what} (last qty={qty})")


async def _wait_cond_count(
    client: AsterClient,
    account_id: str,
    matches: Callable[[int], bool],
    timeout_s: float,
    what: str,
) -> int:
    """Poll conditional orders until the count satisfies ``matches``."""
    deadline = time.monotonic() + timeout_s
    count = -1
    while time.monotonic() < deadline:
        count = len(await _conditionals(client, account_id))
        if matches(count):
            return count
        await asyncio.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {what} (last count={count})")


async def _position_qty(client: AsterClient, account_id: str) -> D:
    return sum((p.position_amt for p in await client.position_risk(account_id, SYMBOL)), D(0))


async def _conditionals(client: AsterClient, account_id: str) -> list:
    return [
        o
        for o in await client.open_orders(account_id, symbol=SYMBOL)
        if o.type in {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}
    ]


async def _follower_leverage(client: AsterClient, account_id: str) -> int | None:
    """Authoritative configured leverage from positionRisk (present even flat)."""
    for p in await client.position_risk(account_id, SYMBOL):
        if p.symbol == SYMBOL and p.leverage > 0:
            return int(p.leverage)
    return None


def _master_trade_event(qty: D, price: D, side: str = "BUY") -> dict:
    """ORDER_TRADE_UPDATE mirroring a REAL master fill (harness convention)."""
    e = next(_event_ids)
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": e,
        "T": e,
        "o": {
            "s": SYMBOL,
            "c": f"lev-m-{e}",
            "S": side,
            "o": "MARKET",
            "q": str(qty),
            "p": "0",
            "ap": str(price),
            "sp": "0",
            "x": "TRADE",
            "X": "FILLED",
            "i": e,
            "l": str(qty),
            "z": str(qty),
            "L": str(price),
            "N": "USDT",
            "n": "0",
            "R": False,
            "wt": "MARK_PRICE",
            "ps": "BOTH",
            "rp": "0",
        },
    }


async def _master_open_long(client: AsterClient, projector: MasterProjector, qty: D) -> None:
    """Real master market entry + protective stop; registers the real fill."""
    rows = await client.position_risk("main", SYMBOL)
    mark = D(str(rows[0].mark_price)) if rows and rows[0].mark_price else D("65000")
    stamp = int(time.time() * 1000)
    order = await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=qty,
            new_order_resp_type="RESULT",
            position_side=PosSide.BOTH,
            new_client_order_id=f"lev-m-{stamp}"[:36],
        ),
    )
    fill_price = order.avg_price or mark
    stop_price = _snap(mark * D("0.98"), D("0.10"))
    await _place(
        client,
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=qty,
            stop_price=stop_price,
            reduce_only=True,
            working_type="MARK_PRICE",
            position_side=PosSide.BOTH,
            new_client_order_id=f"lev-ms-{stamp}"[:36],
        ),
    )
    await projector.apply_raw(_master_trade_event(qty, fill_price))


@pytest.fixture
async def lev_stack():
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")

    # Shared local wallet DB: credentials + signing nonces ONLY.
    db_main = Database(settings.db_path)
    await db_main.connect()
    await db_main.migrate()
    creds = SqliteCredentials(db_main, settings.encryption_key_bytes())
    await creds.load()
    if "main" not in creds.account_ids() or FOLLOWER not in creds.account_ids():
        await db_main.close()
        pytest.skip("need main + copyc1 imported - run import-wallet first")

    # Per-test isolated database for every trading repo (episodes, commands,
    # links, basis, settings, master events).
    tmp_dir = tempfile.mkdtemp(prefix="ct-live-lev-")
    db = Database(Path(tmp_dir) / "engine.sqlite")
    await db.connect()
    await db.migrate()

    # Trading repos reference accounts(id); mirror the registry so policies
    # and trades satisfy their foreign keys.
    for account in await db_main.fetchall("SELECT id, role, label, user_address FROM accounts"):
        await db.execute_write(
            "INSERT OR IGNORE INTO accounts(id, role, label, user_address) VALUES (?,?,?,?)",
            (
                account["id"],
                account["role"],
                f"{account['label']} (isolated)",
                f"{account['user_address']}-isolated",
            ),
        )

    import httpx

    from copytradeaster.aster.rate_limits import RateLimits

    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    client = AsterClient(http, settings.rest_base_url, limiter, creds, creds, NonceStore(db_main))

    original_master_hedge = await client.get_position_mode("main")
    original_follower_hedge = await client.get_position_mode(FOLLOWER)
    if original_master_hedge:
        await client.set_position_mode("main", False)
    if original_follower_hedge:
        await client.set_position_mode(FOLLOWER, False)
    await _flatten_strict(client, "main", "lev-setup-m")
    await _flatten_strict(client, FOLLOWER, "lev-setup-f")

    filters_cache: dict[str, SymbolFilters] = {}
    info = await client.exchange_info(force=True)
    filters_cache.update(info.tradable_filters())

    settings_repo = SettingsRepo(db)
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
        FOLLOWER,
    )

    followers = [FollowerRuntime(account_id=FOLLOWER, client=client, identity=None)]
    executor = Executor(
        lambda _aid: client,
        CommandRepo(db),
        OrderLinkRepo(db),
        {},
        settings=settings_repo,
    )

    manager = LeverageManager(
        client_for=lambda _aid: client,
        symbols=[SYMBOL],
        accounts=lambda: [FOLLOWER],
        catalog_account="main",
        concurrency=2,
        pacing_s=0.05,
        maintenance_interval_s=0.5,
    )
    coordinator = Coordinator(
        followers=followers,
        settings=settings_repo,
        executor=executor,
        trades=CopyTradeRepo(db),
        links=OrderLinkRepo(db),
        filters_provider=lambda: filters_cache,
        app_flags=settings_repo,
        symbol_whitelist=[SYMBOL],
        fee_buffer_pct=D("0.001"),
        working_type="MARK_PRICE",
        basis=SyncBasisRepo(db),
        leverage=manager,
    )
    projector = MasterProjector(
        MasterEventRepo(db),
        CopyTradeRepo(db),
        OrderLinkRepo(db),
        settings_repo,
        coordinator.on_master_delta,
    )
    fr = followers[0]
    policy = await settings_repo.get_policy(FOLLOWER)

    yield {
        "settings": settings,
        "db": db,
        "client": client,
        "coordinator": coordinator,
        "executor": executor,
        "projector": projector,
        "manager": manager,
        "fr": fr,
        "policy": policy,
        "filters": filters_cache[SYMBOL],
    }

    with contextlib.suppress(Exception):
        await _flatten_strict(client, "main", "lev-tear-m")
    with contextlib.suppress(Exception):
        await _flatten_strict(client, FOLLOWER, "lev-tear-f")
    with contextlib.suppress(Exception):
        await client.set_leverage(FOLLOWER, SYMBOL, RESTORE_LEVERAGE)
    if original_master_hedge:
        with contextlib.suppress(Exception):
            await client.set_position_mode("main", True)
    if original_follower_hedge:
        with contextlib.suppress(Exception):
            await client.set_position_mode(FOLLOWER, True)
    await db.close()
    await db_main.close()
    await http.aclose()
    with contextlib.suppress(OSError):
        os.unlink(Path(tmp_dir) / "engine.sqlite")


async def _drive(stack: dict) -> None:
    """Authoritative master REST resnapshot + one full engine pass."""
    coordinator: Coordinator = stack["coordinator"]
    projector: MasterProjector = stack["projector"]
    fr: FollowerRuntime = stack["fr"]
    client: AsterClient = stack["client"]
    await projector.rebuild_full(client, "main")
    await coordinator.recompute_follower(fr, MasterDelta(frozenset({SYMBOL}), projector.state))


# --------------------------------------------------------------------------- #
# S1: reject -> signed account bracket fallback                                #
# --------------------------------------------------------------------------- #


async def test_rejected_prepare_falls_back_to_signed_account_maximum(lev_stack):
    """POST above the bracket is rejected by the REAL exchange (-4028); the
    manager falls back to the account-specific signed bracket maximum."""
    stack = lev_stack
    client: AsterClient = stack["client"]
    manager: LeverageManager = stack["manager"]

    await manager.refresh_catalog()
    base = manager._candidate[SYMBOL]
    assert base.leverage > 0
    # Seed divergence: candidate claims a tier the follower cannot hold.
    manager._candidate[SYMBOL] = CandidateLeverage(
        leverage=999, observed_at=base.observed_at, revision=manager._next_revision()
    )

    leverage = await manager.prepare_symbol(FOLLOWER, SYMBOL)

    assert leverage == base.leverage, "fallback must confirm the signed account maximum"
    assert manager.is_ready(FOLLOWER, SYMBOL)
    assert manager.confirmed(FOLLOWER, SYMBOL) == base.leverage
    # Authoritative cross-check: the exchange really sits at that level.
    assert await _follower_leverage(client, FOLLOWER) == base.leverage

    # Concurrent re-prepare deduplicates through the singleflight ticket.
    both = await asyncio.gather(
        manager.prepare_symbol(FOLLOWER, SYMBOL),
        manager.prepare_symbol(FOLLOWER, SYMBOL),
    )
    assert list(both) == [base.leverage, base.leverage]


# --------------------------------------------------------------------------- #
# S2: fail-closed admission + background worker provisioning                   #
# --------------------------------------------------------------------------- #


async def test_unready_control_plane_skips_entry_then_worker_provisions(lev_stack):
    """Fresh RAM state: the entry must NOT reach the exchange; the live
    background worker closes the readiness gap and the entry then flows."""
    stack = lev_stack
    client: AsterClient = stack["client"]
    manager: LeverageManager = stack["manager"]
    coordinator: Coordinator = stack["coordinator"]
    fr: FollowerRuntime = stack["fr"]
    projector: MasterProjector = stack["projector"]

    await _master_open_long(client, projector, QTY)
    await projector.rebuild_full(client, "main")

    # Pure planning read: the skip reason is surfaced before any execution.
    actual = await coordinator.snapshot_follower(fr)
    _, skips = await coordinator.targets_for(FOLLOWER, projector.state, actual, stack["policy"])
    assert any(s.reason is SkipReason.LEVERAGE_NOT_READY for s in skips), [s.reason for s in skips]

    await _drive(stack)

    # Fail-closed proof: no exposure, no resting orders, preparation enqueued.
    assert await _position_qty(client, FOLLOWER) == 0
    assert await client.open_orders(FOLLOWER, symbol=SYMBOL) == []
    assert manager.pending_count() >= 1
    assert not manager.is_ready(FOLLOWER, SYMBOL)

    # The REAL maintenance loop provisions readiness end-to-end.
    stop_event = asyncio.Event()
    worker = asyncio.create_task(manager.run(stop_event))
    try:
        await _wait_flag(lambda: manager.is_ready(FOLLOWER, SYMBOL), 25, "worker provisioning")
    finally:
        stop_event.set()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(worker, timeout=10)
    assert manager.is_ready(FOLLOWER, SYMBOL), "worker failed to provision against real API"

    await _drive(stack)
    await _wait_qty(client, FOLLOWER, lambda q: q != 0, 30, "entry visibility")
    await _wait_cond_count(client, FOLLOWER, lambda n: n == 1, 20, "protective stop placement")


# --------------------------------------------------------------------------- #
# S3: REALLY-rejected SET blocks dependents; closes stay free                  #
# --------------------------------------------------------------------------- #


async def test_rejected_set_blocks_dependents_but_close_executes(lev_stack):
    """Executor-boundary proof of the P0 contract against the REAL exchange:

    a plan whose SET_LEVERAGE the exchange rejects (-4028) must leave its
    dependent MARKET_OPEN BLOCKED with zero orders sent, while a
    risk-REDUCING MARKET_CLOSE submitted under the same failed prerequisite
    still executes. Recovery: a valid SET re-provisions and the entry flows.
    """
    stack = lev_stack
    client: AsterClient = stack["client"]
    executor: Executor = stack["executor"]

    await stack["manager"].refresh_catalog()
    await stack["manager"].prepare_account(FOLLOWER, [SYMBOL])

    # Real baseline exposure to prove closes stay independent of leverage.
    await _place(
        client,
        FOLLOWER,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=D("0.001"),
            new_order_resp_type="RESULT",
            position_side=PosSide.BOTH,
            new_client_order_id=f"lev-base-{int(time.time() * 1000)}"[:36],
        ),
    )
    baseline = await _wait_qty(
        client, FOLLOWER, lambda q: q != 0, 20, "baseline position visibility"
    )

    stamp = int(time.time() * 1000)
    failing_plan = ExecutionPlan(
        account_id=FOLLOWER,
        trade_id=None,
        actions=[
            ExecutionAction(
                kind=ActionKind.SET_LEVERAGE,
                symbol=SYMBOL,
                payload={"symbol": SYMBOL, "leverage": "999"},
                client_order_id=None,
                replaces_client_order_id=None,
                priority=0,
                action_id="leverage:BTCUSDT",
            ),
            ExecutionAction(
                kind=ActionKind.MARKET_OPEN,
                symbol=SYMBOL,
                payload=_order_payload(
                    SYMBOL, Side.BUY, "MARKET", QTY, PosSide.BOTH, closing=False
                ),
                client_order_id=f"lev-dep-open-{stamp}"[:36],
                replaces_client_order_id=None,
                priority=1,
                depends_on_action_id="leverage:BTCUSDT",
            ),
        ],
    )
    results = await executor.submit(failing_plan, client=client)
    by_status = [r.status for r in results]
    assert "FAILED" in by_status, results
    assert "BLOCKED" in by_status, results

    # P0 core: the rejected leverage left NO exposure change and no orders.
    await asyncio.sleep(4)
    current = await _position_qty(client, FOLLOWER)
    assert current == baseline, "dependent entry must not execute"

    # Reductions never carry the dependency: close must CONFIRM for real.
    closing_plan = ExecutionPlan(
        account_id=FOLLOWER,
        trade_id=None,
        actions=[
            ExecutionAction(
                kind=ActionKind.SET_LEVERAGE,
                symbol=SYMBOL,
                payload={"symbol": SYMBOL, "leverage": "999"},
                client_order_id=None,
                replaces_client_order_id=None,
                priority=0,
                action_id="leverage:BTCUSDT",
            ),
            ExecutionAction(
                kind=ActionKind.MARKET_CLOSE,
                symbol=SYMBOL,
                payload=_order_payload(
                    SYMBOL, Side.SELL, "MARKET", baseline, PosSide.BOTH, closing=True
                ),
                client_order_id=f"lev-free-close-{stamp}"[:36],
                replaces_client_order_id=None,
                priority=1,
            ),
        ],
    )
    close_results = await executor.submit(closing_plan, client=client)
    close_status = [r.status for r in close_results]
    assert "FAILED" in close_status  # SET still rejected
    assert "CONFIRMED" in close_status, close_results
    await _wait_qty(client, FOLLOWER, lambda q: q == 0, 20, "close fill visibility")

    # Recovery arc: valid leverage re-provision lets the entry flow.
    await client.set_leverage(FOLLOWER, SYMBOL, RESTORE_LEVERAGE)
    healed_plan = ExecutionPlan(
        account_id=FOLLOWER,
        trade_id=None,
        actions=[
            ExecutionAction(
                kind=ActionKind.SET_LEVERAGE,
                symbol=SYMBOL,
                payload={"symbol": SYMBOL, "leverage": str(RESTORE_LEVERAGE)},
                client_order_id=None,
                replaces_client_order_id=None,
                priority=0,
                action_id="leverage:BTCUSDT-heal",
            ),
            ExecutionAction(
                kind=ActionKind.MARKET_OPEN,
                symbol=SYMBOL,
                payload=_order_payload(
                    SYMBOL, Side.BUY, "MARKET", QTY, PosSide.BOTH, closing=False
                ),
                client_order_id=f"lev-heal-open-{stamp}"[:36],
                replaces_client_order_id=None,
                priority=1,
                depends_on_action_id="leverage:BTCUSDT-heal",
            ),
        ],
    )
    healed = await executor.submit(healed_plan, client=client)
    assert all(r.status in {"CONFIRMED", "SUBMITTED"} for r in healed), healed
    await _wait_qty(client, FOLLOWER, lambda q: q != 0, 20, "healed entry visibility")


# --------------------------------------------------------------------------- #
# S4: REAL downshift -> durable unready -> re-entry gated, close flows         #
# --------------------------------------------------------------------------- #


async def _master_close(client: AsterClient, projector: MasterProjector) -> None:
    """Flatten the REAL master leg (position + protective stop)."""
    del projector
    for order in await client.open_orders("main", symbol=SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=order.client_order_id)
    for p in await client.position_risk("main", SYMBOL):
        if p.symbol == SYMBOL and p.position_amt != 0:
            await _place(
                client,
                "main",
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL if p.position_amt > 0 else Side.BUY,
                    type="MARKET",
                    quantity=abs(p.position_amt),
                    reduce_only=True,
                    position_side=p.position_side,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"lev-mc-{int(time.time() * 1000)}"[:36],
                ),
            )


async def test_real_downshift_gates_reentry_until_prepared(lev_stack):
    """A REAL leverage change on the wallet (200 -> 20) diverges from the RAM
    confirmation; every snapshot re-observes the disagreement so the pair
    stays durably unready. While unready:

    * the master closing still flows (risk-reducing path is never gated);
    * the master re-entry larger than before is skipped with
      LEVERAGE_NOT_READY and enqueues preparation - zero orders sent;
    * prepare_account posts the candidate again and the entry then flows.
    """
    stack = lev_stack
    client: AsterClient = stack["client"]
    manager: LeverageManager = stack["manager"]
    coordinator: Coordinator = stack["coordinator"]
    fr: FollowerRuntime = stack["fr"]
    projector: MasterProjector = stack["projector"]

    await manager.refresh_catalog()
    await manager.prepare_account(FOLLOWER, [SYMBOL])
    await _master_open_long(client, projector, QTY)
    await _drive(stack)
    first_qty = abs(
        await _wait_qty(client, FOLLOWER, lambda q: q != 0, 30, "initial entry visibility")
    )
    assert first_qty > 0

    # REAL divergence: the exchange now sits at 20 while RAM says 200.
    await client.set_leverage(FOLLOWER, SYMBOL, 20)

    # Risk-reducing flow must ignore readiness entirely.
    await _master_close(client, projector)
    await _drive(stack)
    await _wait_qty(client, FOLLOWER, lambda q: q == 0, 30, "follower close while unready")
    assert not manager.is_ready(FOLLOWER, SYMBOL), "observation must flag disagreement"

    # Master re-entry larger than before: gated, nothing reaches the exchange.
    await _master_open_long(client, projector, QTY * 2)
    await _drive(stack)
    await asyncio.sleep(3)
    assert await _position_qty(client, FOLLOWER) == 0, "gated entry must not execute"
    assert await client.open_orders(FOLLOWER, symbol=SYMBOL) == []
    assert manager.pending_count() >= 1, "gated entry must enqueue preparation"
    # Fresh master state for the planning read (stale_master window is 5s).
    await projector.rebuild_full(client, "main")
    actual = await coordinator.snapshot_follower(fr)
    _, skips = await coordinator.targets_for(FOLLOWER, projector.state, actual, stack["policy"])
    assert any(s.reason is SkipReason.LEVERAGE_NOT_READY for s in skips), [s.reason for s in skips]

    # Honest re-provision posts the candidate; the entry then flows.
    report = await manager.prepare_account(FOLLOWER, [SYMBOL])
    assert report.failed == {}, report.failed
    assert await _follower_leverage(client, FOLLOWER) == RESTORE_LEVERAGE
    assert manager.is_ready(FOLLOWER, SYMBOL)

    await _drive(stack)
    # Engine sizing is risk-based, not master-proportional: a flowed entry is
    # simply non-zero again, at the freshly confirmed leverage.
    reentered = await _wait_qty(
        client, FOLLOWER, lambda q: q != 0, 40, "gated entry flowing after prepare"
    )
    assert abs(reentered) >= D("0.001")
