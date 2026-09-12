"""Signed ONE_WAY E2E through the REAL engine (marker ``testnet``).

Reviewer blocker #2: the earlier one-way proof called AsterClient directly and
never exercised the copy core. This scenario runs the full pipeline
Projector -> Coordinator -> Risk -> Planner -> Executor -> journal -> REAL
follower on the Aster testnet in ONE_WAY mode:

    master open -> stop -> stop move (replacement) -> partial close ->
    TP -> master close -> follower flat audit

The copyc1 follower executes real signed orders; master positions are real
signed orders too, but master events are fed to the projector directly, so the
scenario stays deterministic even if the testnet user stream lags. Between
wait attempts the projector resnapshots authoritatively via REST
(``rebuild_full``), which also picks up the REAL resting master protection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import UTC, datetime
from decimal import ROUND_FLOOR
from decimal import Decimal as D

import pytest

from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import PosSide, RiskPolicy, Side, SymbolFilters
from copytradeaster.master.projector import MasterProjector
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
from copytradeaster.trading.executor import Executor, resolve_boot_unknowns
from copytradeaster.trading.leverage import LeverageManager

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"


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


@pytest.fixture
async def stack(tmp_path):
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    # The provisioned DB supplies encrypted credentials and durable nonces
    # only. Journal, episodes, policies and sync-basis state live in a fresh
    # per-test DB so prior signed campaigns cannot influence this harness.
    db_main = Database(settings.db_path)
    await db_main.connect()
    await db_main.migrate()
    creds = SqliteCredentials(db_main, settings.encryption_key_bytes())
    await creds.load()
    if "main" not in creds.account_ids() or "copyc1" not in creds.account_ids():
        await db_main.close()
        pytest.skip("need main + copyc1 imported - run import-wallet first")

    db = Database(tmp_path / "one-way-engine.sqlite3")
    await db.connect()
    await db.migrate()
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

    filters_cache: dict[str, SymbolFilters] = {}
    followers = [FollowerRuntime(account_id="copyc1", client=client, identity=None)]
    executor = Executor(
        lambda _aid: client,
        CommandRepo(db),
        OrderLinkRepo(db),
        {},
        settings=SettingsRepo(db),
    )
    coordinator = Coordinator(
        followers=followers,
        settings=SettingsRepo(db),
        executor=executor,
        trades=CopyTradeRepo(db),
        links=OrderLinkRepo(db),
        filters_provider=lambda: filters_cache,
        app_flags=SettingsRepo(db),
        symbol_whitelist=[SYMBOL],
        fee_buffer_pct=D("0.001"),
        working_type="MARK_PRICE",
        basis=SyncBasisRepo(db),
        # Production parity: the engine reads leverage from the RAM
        # control-plane, never from a per-client TTL cache.
        leverage=LeverageManager(
            client_for=lambda _aid: client,
            symbols=[SYMBOL],
            accounts=lambda: [],
            catalog_account="main",
            pacing_s=0.0,
        ),
    )

    async def on_master_delta(delta):
        # Match App._on_delta: a WS trigger is never a mode/mark authority.
        await projector.refresh_mark_prices(client, "main", delta.changed_symbols)
        await coordinator.on_master_delta(delta)

    projector = MasterProjector(
        MasterEventRepo(db),
        CopyTradeRepo(db),
        OrderLinkRepo(db),
        SettingsRepo(db),
        on_master_delta,
    )
    info = await client.exchange_info(force=True)
    filters_cache.update(info.tradable_filters())
    settings_repo = SettingsRepo(db)
    # Seed the candidate catalog and confirm leverage for the follower up
    # front, exactly like App.start + onboarding preparation do in production.
    await coordinator._leverage.refresh_catalog()  # noqa: SLF001
    await coordinator._leverage.prepare_account("copyc1", [SYMBOL])  # noqa: SLF001

    # The emergency signed scenario intentionally leaves the shared operator
    # state paused.  This independent engine scenario must opt back into
    # entries explicitly instead of depending on test execution order.
    await settings_repo.set_flag("allow_new_entries", "true")
    await settings_repo.set_flag("emergency_active", "false")
    await settings_repo.upsert_policy(
        RiskPolicy(risk_percent=D("0.01"), max_leverage=20, max_entry_deviation_pct=D("1")),
        "copyc1",
    )

    yield {
        "settings": settings,
        "db": db,
        "client": client,
        "coordinator": coordinator,
        "projector": projector,
        "filters": filters_cache,
    }

    with contextlib.suppress(Exception):
        await http.aclose()
    await db.close()
    await db_main.close()


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


async def _position_qty(client: AsterClient, account_id: str) -> D:
    return sum((p.position_amt for p in await client.position_risk(account_id, SYMBOL)), D(0))


async def _conditionals(client: AsterClient, account_id: str) -> list:
    return [
        o
        for o in await client.open_orders(account_id, symbol=SYMBOL)
        if o.type in {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}
    ]


def _master_trade(base: int, offset: int, qty: str, price: str, side: str = "BUY") -> dict:
    e = base + offset
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": e,
        "T": e,
        "o": {
            "s": SYMBOL,
            "c": f"m-{e}",
            "S": side,
            "o": "MARKET",
            "q": qty,
            "p": "0",
            "ap": price,
            "sp": "0",
            "x": "TRADE",
            "X": "FILLED",
            "i": e,
            "l": qty,
            "z": qty,
            "L": price,
            "N": "USDT",
            "n": "0",
            "R": False,
            "wt": "MARK_PRICE",
            "ps": "BOTH",
            "rp": "0",
        },
    }


def _master_prot(
    base: int, offset: int, coid: str, otype: str, sp: str, qty: str, side: str = "SELL"
) -> dict:
    e = base + offset
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": e,
        "T": e,
        "o": {
            "s": SYMBOL,
            "c": coid,
            "S": side,
            "o": otype,
            "ot": otype,
            "q": qty,
            "p": "0",
            "ap": "0",
            "sp": sp,
            "x": "NEW",
            "X": "NEW",
            "i": e,
            "l": "0",
            "z": "0",
            "L": "0",
            "N": "USDT",
            "n": "0",
            "R": True,
            "cp": False,
            "wt": "MARK_PRICE",
            "ps": "BOTH",
            "rp": "0",
        },
    }


async def test_one_way_full_engine_signed_cycle(stack):
    """ONE_WAY copy cycle: open/stop/stop-move/partial/TP/flat through the core."""
    client: AsterClient = stack["client"]
    projector: MasterProjector = stack["projector"]
    coordinator: Coordinator = stack["coordinator"]
    filters: SymbolFilters = stack["filters"][SYMBOL]
    tick, step = filters.tick_size, filters.step_size
    fr_id = "copyc1"
    base = int(time.time() * 1000)  # unique per run: master_events dedups persistently
    run_started = datetime.now(UTC).isoformat()

    async def wait_until(predicate, timeout_s: float, what: str, *, rebuilds: int = 1) -> None:
        """Wait for a condition; between attempts resnapshot the master
        authoritatively via REST AND drive the follower through the real core.
        rebuild_full() only refreshes projector.state - unlike the production
        App this bare harness has no reconcile loop, so without an explicit
        recompute_follower the coordinator would never see the new snapshot."""
        fr = coordinator.followers[0]

        async def _drive() -> None:
            from copytradeaster.master.projector import MasterDelta

            await coordinator.recompute_follower(
                fr, MasterDelta(frozenset({SYMBOL}), projector.state)
            )

        for attempt in range(rebuilds + 1):
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if await predicate():
                    return
                await asyncio.sleep(1.0)
            if attempt < rebuilds:
                await projector.rebuild_full(client, "main")
                await _drive()
        raise TimeoutError(f"timed out waiting for {what}")

    original_master_hedge = await client.get_position_mode("main")
    original_follower_hedge = await client.get_position_mode(fr_id)

    try:
        if original_master_hedge:
            await client.set_position_mode("main", False)
        if original_follower_hedge:
            await client.set_position_mode(fr_id, False)

        await _flatten(client, "main", False, f"owe2e-m-{base}")
        await _flatten(client, fr_id, False, f"owe2e-f-{base}")
        await asyncio.sleep(0.5)
        assert await _position_qty(client, fr_id) == 0

        # --- phase 1: master opens long (real order + projector event) -------
        # The dust phase (4b) needs the master position strictly above the
        # follower's own risk-sized entry so the proportional target floors
        # to zero. Size the entry from the follower's real budget instead of
        # guessing a constant.
        follower_budget_probe = await client.balance(fr_id)
        follower_usdt = next(
            (b.available_balance for b in follower_budget_probe if b.asset == "USDT"), D("10")
        )
        master_budget_probe = await client.balance("main")
        master_usdt = next(
            (b.available_balance for b in master_budget_probe if b.asset == "USDT"), D(0)
        )
        master_rows = await client.position_risk("main", SYMBOL)
        master_row = next((p for p in master_rows if p.symbol == SYMBOL), None)
        master_mark = (
            master_row.mark_price
            if master_row is not None and master_row.mark_price > 0
            else D("65000")
        )
        master_leverage = (
            int(master_row.leverage)
            if master_row is not None and int(master_row.leverage) > 0
            else 1
        )
        probe_policy = await SettingsRepo(stack["db"]).get_policy(fr_id)
        assert probe_policy is not None
        # mirror entry_quantity(): qty = budget*risk% / (distance*(1+fee))
        est_follower_qty = _snap(
            follower_usdt * probe_policy.risk_percent / D("500"), step
        )  # ~stop distance used below
        desired_open_qty = max(_snap(est_follower_qty * D("20"), step), step * 5)
        # Keep half of the signed available-margin envelope unused. The old
        # harness sized the master solely from follower equity and could ask
        # this separate wallet for an unaffordable 0.180 BTC order.
        max_affordable = _snap(
            master_usdt * D(master_leverage) * D("0.5") / master_mark,
            step,
        )
        minimum_shape = max(est_follower_qty + step, step * 5)
        if max_affordable < minimum_shape:
            pytest.skip(
                "master Testnet balance cannot safely shape the mandatory dust scenario "
                f"(safe_qty={max_affordable}, required={minimum_shape})"
            )
        open_qty = min(desired_open_qty, max_affordable)
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=open_qty,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"owe2e-open-{base}"[:36],
            ),
        )
        master_qty = await _position_qty(client, "main")
        assert master_qty > 0
        master_pos = next(
            p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0
        )
        entry = master_pos.entry_price
        await projector.apply_raw(_master_trade(base, 1, str(master_qty), str(entry)))

        # --- phase 2: master stop #1 -> follower must enter WITH protection --
        stop_a = _snap(entry - D("500"), tick)
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=master_qty,
                stop_price=stop_a,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_order_resp_type="ACK",
                new_client_order_id=f"owe2e-sla-{base}"[:36],
            ),
        )
        await projector.apply_raw(
            _master_prot(base, 2, f"m-sla-{base}", "STOP_MARKET", str(stop_a), str(master_qty))
        )

        async def follower_entered() -> bool:
            held = await _position_qty(client, fr_id) > 0
            return held and bool(await _conditionals(client, fr_id))

        await wait_until(follower_entered, 45, "follower entry with protection", rebuilds=2)

        follower_prot = await _conditionals(client, fr_id)
        # ONE_WAY closing semantics on the REAL exchange
        assert all(o.reduce_only for o in follower_prot), follower_prot
        assert all(o.position_side == PosSide.BOTH for o in follower_prot), follower_prot
        old_stop_level = next(o.stop_price for o in follower_prot if o.type.startswith("STOP"))
        assert abs(await _position_qty(client, fr_id)) >= filters.min_qty

        # --- phase 3: master moves the stop -> engine replaces protection ----
        stop_b = _snap(entry - D("800"), tick)
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=master_qty,
                stop_price=stop_b,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_order_resp_type="ACK",
                new_client_order_id=f"owe2e-slb-{base}"[:36],
            ),
        )
        await projector.apply_raw(
            _master_prot(base, 3, f"m-slb-{base}", "STOP_MARKET", str(stop_b), str(master_qty))
        )

        async def follower_stop_replaced() -> bool:
            stops = [o for o in await _conditionals(client, fr_id) if o.type.startswith("STOP")]
            return len(stops) == 1 and stops[0].stop_price == stop_b

        await wait_until(follower_stop_replaced, 45, "follower stop replacement", rebuilds=2)

        # --- phase 4: master partial close -> follower reduces ---------------
        part = _snap(master_qty * D("0.5"), step)
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=part,
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"owe2e-part-{base}"[:36],
            ),
        )
        master_left = await _position_qty(client, "main")
        qty_before_reduce = abs(await _position_qty(client, fr_id))
        await projector.apply_raw(_master_trade(base, 4, str(part), str(entry), side="SELL"))

        # Expected follower magnitude after the proportional reduction. Mirrors
        # scaled_reduce(): floor_to_step(qty * master_new / master_old). If the
        # follower's size is already minimal the reduction rounds to zero and
        # the engine legitimately flattens the whole leg.
        expected_target = (qty_before_reduce * abs(master_left) / master_qty).quantize(
            step, rounding=ROUND_FLOOR
        )

        async def follower_reduced() -> bool:
            actual = abs(await _position_qty(client, fr_id))
            return actual == expected_target

        await wait_until(follower_reduced, 45, "follower partial reduction", rebuilds=2)

        # --- phase 4b: dust reduction -> intentional flat, NO re-entry -------
        # Master cuts to a level whose proportional follower target rounds
        # below the step: the leg must flatten ON PURPOSE and stay flat
        # through repeated reconciles (no accidental re-entry), because the
        # master episode is still open.
        #
        # Reviewer pass 3: this phase used to be behind a soft `if` that could
        # silently skip it (0.003 * 0.005 = 0.000015 < stepSize) and it fed the
        # projector a REMAINING qty as if it were the fill size. Now the dust
        # scenario is MANDATORY: the master remainder is exactly one step, the
        # real REST state feeds the projector via rebuild_full, and any guard
        # violation fails the test.
        assert expected_target > 0, "phase-3 reduction must leave a non-dust leg"
        # The smallest legal remainder: the follower's proportional target
        # floor(qty * step / master_qty) is then provably below one step, so
        # the engine must flatten the whole leg ON PURPOSE (sticky dust-flat).
        dust_left = step
        assert dust_left < abs(master_left) < open_qty, (
            f"dust scenario impossible: open_qty={open_qty}, master_left={master_left}, step={step}"
        )
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=abs(master_left) - dust_left,
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"owe2e-dust-{base}"[:36],
            ),
        )
        # authoritative REST state -> projector (no hand-synthesized fill):
        # the exchange is the source of truth for what actually executed
        master_state = await projector.rebuild_full(client, "main")
        master_left = next(
            p.qty for k, p in master_state.positions.items() if k.symbol == SYMBOL and p.qty != 0
        )
        assert abs(master_left - dust_left) < step, (master_left, dust_left)

        async def follower_flat_on_dust() -> bool:
            return await _position_qty(client, fr_id) == 0

        await wait_until(follower_flat_on_dust, 45, "dust-target intentional flat", rebuilds=1)
        # repeated reconciles must NOT re-open while the episode is open
        for _ in range(2):
            await projector.rebuild_full(client, "main")
            await asyncio.sleep(1.0)
            assert await _position_qty(client, fr_id) == 0, (
                "intentional flat must survive reconciliation"
            )
        assert not await _conditionals(client, fr_id)

        # --- phase 4c: master flips LONG -> SHORT -----------------------------
        # Reviewer pass 3 regression: the transitional zero of a flip used to
        # latch zero_target and permanently block the reversed entry. The
        # engine must close the (already flat) old side, treat the flip as a
        # NEW master episode, and open the follower SHORT on the next pass.
        flip_sell = _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=dust_left * 2,  # +dust -> -dust: a real sign flip
                # A reduce-only order would be capped at the held size and
                # leave the master flat instead of flipping it; a genuine
                # sign flip needs a plain (non-reduce) sell past zero.
                reduce_only=False,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"owe2e-flip-{base}"[:36],
            ),
        )
        try:
            await flip_sell
        except AsterError as exc:
            pytest.fail(f"flip order rejected: {exc}")
        master_state = await projector.rebuild_full(client, "main")
        flipped = [
            p.qty for k, p in master_state.positions.items() if k.symbol == SYMBOL and p.qty != 0
        ]
        assert len(flipped) == 1 and flipped[0] < 0, f"master did not actually flip: {flipped}"
        mark_now = (
            mpos_now.mark_price
            if (
                mpos_now := next(
                    p
                    for k, p in master_state.positions.items()
                    if k.symbol == SYMBOL and p.qty != 0
                )
            )
            else entry
        )

        # short protection above the market makes the entry copyable
        short_stop_level = _snap(mark_now + D("400"), tick)
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="STOP_MARKET",
                quantity=abs(flipped[0]),
                stop_price=short_stop_level,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_order_resp_type="ACK",
                new_client_order_id=f"owe2e-ss-{base}"[:36],
            ),
        )
        state = await projector.rebuild_full(client, "main")
        mpos = next(p for k, p in state.positions.items() if k.symbol == SYMBOL and p.qty != 0)
        await projector.apply_raw(
            _master_prot(
                base, 46, f"m-ss-{base}", "STOP_MARKET", str(short_stop_level), str(abs(mpos.qty))
            )
        )

        async def follower_flipped_short() -> bool:
            return await _position_qty(client, fr_id) < 0

        await wait_until(
            follower_flipped_short, 60, "follower opens the SHORT after flip", rebuilds=2
        )
        short_stop = [o for o in await _conditionals(client, fr_id) if o.type.startswith("STOP")]
        assert len(short_stop) == 1 and short_stop[0].stop_price == short_stop_level

        # --- phase 5: master TP -> follower mirrors it -----------------------
        # Direction-aware: after the 4c flip the master is SHORT, so its TP
        # must be a BUY below the market (a long-side SELL TP above the
        # market would -2021 immediately-trigger on the exchange).
        master_short = await _position_qty(client, "main") < 0
        tp_side = Side.BUY if master_short else Side.SELL
        tp_level = _snap(
            (mark_now if master_short else entry) + (D("-900") if master_short else D("900")),
            tick,
        )
        tp_qty = abs(await _position_qty(client, "main"))
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=tp_side,
                type="TAKE_PROFIT_MARKET",
                quantity=tp_qty,
                stop_price=tp_level,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_order_resp_type="ACK",
                new_client_order_id=f"owe2e-tp-{base}"[:36],
            ),
        )
        await projector.apply_raw(
            _master_prot(
                base,
                5,
                f"m-tp-{base}",
                "TAKE_PROFIT_MARKET",
                str(tp_level),
                str(tp_qty),
                side="BUY" if master_short else "SELL",
            )
        )

        async def follower_tp_placed() -> bool:
            tps = [
                o for o in await _conditionals(client, fr_id) if o.type.startswith("TAKE_PROFIT")
            ]
            return len(tps) == 1 and tps[0].stop_price == tp_level

        await wait_until(follower_tp_placed, 45, "follower TP placement", rebuilds=2)

        # --- phase 6: master closes -> follower flattens, no orders remain ---
        close_side = Side.BUY if master_short else Side.SELL
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=close_side,
                type="MARKET",
                quantity=tp_qty,
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"owe2e-close-{base}"[:36],
            ),
        )
        await projector.apply_raw(
            _master_trade(base, 6, str(tp_qty), str(entry), side="BUY" if master_short else "SELL")
        )

        async def both_flat_clean() -> bool:
            return (
                await _position_qty(client, "main") == 0
                and await _position_qty(client, fr_id) == 0
                and not await _conditionals(client, fr_id)
            )

        await wait_until(both_flat_clean, 60, "master+follower flat without orders", rebuilds=2)
        assert await client.open_orders(fr_id) == []

        # --- phase 7: journal audit (this run only) --------------------------
        rows = await CommandRepo(stack["db"]).recent(10_000)
        own = [r for r in rows if r["account_id"] == fr_id and str(r["created_at"]) >= run_started]
        statuses: dict[str, int] = {}
        for row in own:
            statuses[str(row["status"])] = statuses.get(str(row["status"]), 0) + 1
        assert own, "engine must have issued commands for the follower"
        assert statuses.get("FAILED", 0) == 0, statuses
        assert statuses.get("UNKNOWN", 0) == 0, statuses
        assert statuses.get("BLOCKED", 0) == 0, statuses
        for row in own:
            payload = json.loads(str(row["payload"]))
            if str(row["kind"]) == "MARKET_CLOSE":
                assert payload.get("reduceOnly") == "true", (row["kind"], payload)
                assert "positionSide" not in payload, (row["kind"], payload)
            if str(row["kind"]) in ("PLACE_STOP", "PLACE_TP"):
                assert payload.get("closePosition") == "true", (row["kind"], payload)
                assert "quantity" not in payload and "reduceOnly" not in payload
                assert "positionSide" not in payload, (row["kind"], payload)
        kinds = [str(r["kind"]) for r in own]
        assert "MARKET_OPEN" in kinds and "PLACE_STOP" in kinds, kinds
        assert "MARKET_CLOSE" in kinds and "PLACE_TP" in kinds, kinds
        assert old_stop_level > 0  # replacement really moved from a live level
    finally:
        with contextlib.suppress(Exception):
            await _flatten(client, "main", False, f"owe2e-clean-m-{base}")
        with contextlib.suppress(Exception):
            await _flatten(client, fr_id, False, f"owe2e-clean-f-{base}")
        if original_master_hedge:
            with contextlib.suppress(Exception):
                await client.set_position_mode("main", True)
        if original_follower_hedge:
            with contextlib.suppress(Exception):
                await client.set_position_mode(fr_id, True)


async def test_external_follower_close_stays_flat_until_next_master_episode(stack):
    """REAL follower manual close latches no-reentry across reconcile + restart."""
    from copytradeaster.master.projector import MasterDelta

    client: AsterClient = stack["client"]
    projector: MasterProjector = stack["projector"]
    coordinator: Coordinator = stack["coordinator"]
    filters: SymbolFilters = stack["filters"][SYMBOL]
    db: Database = stack["db"]
    fr_id = "copyc1"
    base = int(time.time() * 1000)
    original_master_hedge = await client.get_position_mode("main")
    original_follower_hedge = await client.get_position_mode(fr_id)

    async def drive(active: Coordinator = coordinator) -> int:
        state = await projector.rebuild_full(client, "main")
        fr = active.followers[0]
        return await active.recompute_follower(
            fr,
            MasterDelta(frozenset({SYMBOL}), state),
            snapshot_scope={SYMBOL},
        )

    async def wait_for(predicate, what: str, *, active: Coordinator = coordinator) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if await predicate():
                return
            await drive(active)
            await asyncio.sleep(1)
        raise TimeoutError(f"timed out waiting for {what}")

    try:
        if original_master_hedge:
            await client.set_position_mode("main", False)
        if original_follower_hedge:
            await client.set_position_mode(fr_id, False)
        await _flatten(client, "main", False, f"ext-pre-m-{base}")
        await _flatten(client, fr_id, False, f"ext-pre-f-{base}")

        master_rows = await client.position_risk("main", SYMBOL)
        master_flat = next(row for row in master_rows if row.position_side == PosSide.BOTH)
        mark = master_flat.mark_price
        minimum_notional_qty = _snap(
            filters.min_notional / mark + filters.step_size, filters.step_size
        )
        open_qty = max(filters.min_qty, minimum_notional_qty, filters.step_size)
        master_balance = next(
            b.available_balance for b in await client.balance("main") if b.asset == "USDT"
        )
        affordable = _snap(
            master_balance * D(max(master_flat.leverage, 1)) * D("0.25") / mark,
            filters.step_size,
        )
        if affordable < open_qty:
            pytest.skip(
                "master Testnet balance cannot safely fund external-close scenario "
                f"(safe_qty={affordable}, required={open_qty})"
            )

        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=open_qty,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"ext-m-open-{base}"[:36],
            ),
        )
        master_pos = next(
            row
            for row in await client.position_risk("main", SYMBOL)
            if row.position_side == PosSide.BOTH and row.position_amt > 0
        )
        stop = _snap(
            master_pos.entry_price
            - max(master_pos.entry_price * D("0.01"), filters.tick_size * 10),
            filters.tick_size,
        )
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=master_pos.position_amt,
                stop_price=stop,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_order_resp_type="ACK",
                new_client_order_id=f"ext-m-stop-{base}"[:36],
            ),
        )
        await drive()

        async def follower_open_and_protected() -> bool:
            return await _position_qty(client, fr_id) > 0 and bool(
                await _conditionals(client, fr_id)
            )

        await wait_for(follower_open_and_protected, "initial follower entry")
        trade = await CopyTradeRepo(db).active_trade(SYMBOL, PosSide.BOTH.value)
        assert trade is not None
        first_trade_id = int(trade["trade_id"])
        opens_before = await db.fetchone(
            "SELECT COUNT(*) AS n FROM execution_commands"
            " WHERE account_id=? AND trade_id=? AND kind='MARKET_OPEN'",
            (fr_id, first_trade_id),
        )
        assert opens_before is not None and int(opens_before["n"]) >= 1

        follower_qty = abs(await _position_qty(client, fr_id))
        await _place(
            client,
            fr_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=follower_qty,
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"ext-user-close-{base}"[:36],
            ),
        )

        async def follower_flat() -> bool:
            return await _position_qty(client, fr_id) == 0

        await wait_for(follower_flat, "manual follower flat")
        # Drive once authoritatively after the direct exchange mutation.
        await drive()
        assert await _position_qty(client, fr_id) == 0
        assert not await _conditionals(client, fr_id)

        for _ in range(3):
            await drive()
            await asyncio.sleep(1)
            assert await _position_qty(client, fr_id) == 0
        basis = SyncBasisRepo(db)
        blocked = await basis.get(fr_id, SYMBOL, PosSide.BOTH.value)
        assert blocked is not None
        assert int(blocked["episode_trade_id"]) == first_trade_id
        assert int(blocked["external_close_blocked"]) == 1
        opens_after = await db.fetchone(
            "SELECT COUNT(*) AS n FROM execution_commands"
            " WHERE account_id=? AND trade_id=? AND kind='MARKET_OPEN'",
            (fr_id, first_trade_id),
        )
        assert opens_after is not None
        assert int(opens_after["n"]) == int(opens_before["n"])

        restarted_fr = FollowerRuntime(account_id=fr_id, client=client, identity=None)
        restarted = Coordinator(
            followers=[restarted_fr],
            settings=SettingsRepo(db),
            executor=Executor(
                lambda _aid: client,
                CommandRepo(db),
                OrderLinkRepo(db),
                {},
                settings=SettingsRepo(db),
            ),
            trades=CopyTradeRepo(db),
            links=OrderLinkRepo(db),
            filters_provider=lambda: stack["filters"],
            app_flags=SettingsRepo(db),
            symbol_whitelist=[SYMBOL],
            fee_buffer_pct=D("0.001"),
            working_type="MARK_PRICE",
            basis=basis,
            leverage=coordinator._leverage,  # noqa: SLF001 - restart parity in signed harness
        )
        await drive(restarted)
        assert await _position_qty(client, fr_id) == 0

        # End the old episode authoritatively, then create a fresh protected
        # master episode.  Only that episode boundary may re-arm the follower.
        for order in await client.open_orders("main", symbol=SYMBOL):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=order.client_order_id)
        old_master_qty = abs(await _position_qty(client, "main"))
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=old_master_qty,
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"ext-m-close-{base}"[:36],
            ),
        )
        await drive(restarted)
        assert await CopyTradeRepo(db).active_trade(SYMBOL, PosSide.BOTH.value) is None

        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=open_qty,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"ext-m-reopen-{base}"[:36],
            ),
        )
        new_master = next(
            row
            for row in await client.position_risk("main", SYMBOL)
            if row.position_side == PosSide.BOTH and row.position_amt > 0
        )
        new_stop = _snap(
            new_master.entry_price
            - max(new_master.entry_price * D("0.01"), filters.tick_size * 10),
            filters.tick_size,
        )
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=new_master.position_amt,
                stop_price=new_stop,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_order_resp_type="ACK",
                new_client_order_id=f"ext-m-restop-{base}"[:36],
            ),
        )
        await drive(restarted)

        async def follower_reentered() -> bool:
            return await _position_qty(client, fr_id) > 0

        await wait_for(follower_reentered, "new-episode follower entry", active=restarted)
        second_trade = await CopyTradeRepo(db).active_trade(SYMBOL, PosSide.BOTH.value)
        assert second_trade is not None
        assert int(second_trade["trade_id"]) != first_trade_id
    finally:
        with contextlib.suppress(Exception):
            await _flatten(client, "main", False, f"ext-clean-m-{base}")
        with contextlib.suppress(Exception):
            await _flatten(client, fr_id, False, f"ext-clean-f-{base}")
        for account_id in ("main", fr_id):
            assert await _position_qty(client, account_id) == 0
            assert not await client.open_orders(account_id, symbol=SYMBOL)
        if original_master_hedge:
            await client.set_position_mode("main", True)
        if original_follower_hedge:
            await client.set_position_mode(fr_id, True)


async def test_margin_mode_mirrors_master_and_isolated_collateral_is_double_stop_risk(stack):
    """Signed V3: both margin-mode transitions through the production copy core."""
    from copytradeaster.master.projector import MasterDelta

    client: AsterClient = stack["client"]
    projector: MasterProjector = stack["projector"]
    coordinator: Coordinator = stack["coordinator"]
    db: Database = stack["db"]
    filters: SymbolFilters = stack["filters"][SYMBOL]
    follower = "copyc1"
    base = int(time.time() * 1000)
    original_hedge = {
        account: await client.get_position_mode(account) for account in ("main", follower)
    }
    original_margin: dict[str, str] = {}
    for account in ("main", follower):
        # The disposable accounts may be left in HEDGE mode by an earlier
        # signed campaign, so there is no BOTH row until the setup below
        # switches them to ONE_WAY.  Preserve the authoritative mode from any
        # flat positionRisk leg and verify HEDGE legs agree.
        rows = await client.position_risk(account, SYMBOL)
        modes = {str(p.margin_type).upper() for p in rows}
        if len(modes) != 1:
            pytest.fail(f"inconsistent initial margin types for {account}: {modes}")
        original_margin[account] = next(iter(modes))
    original_multi_assets = {
        account: await client.get_multi_assets_mode(account) for account in ("main", follower)
    }

    async def drive() -> None:
        state = await projector.rebuild_full(client, "main")
        await coordinator.recompute_follower(
            coordinator.followers[0],
            MasterDelta(frozenset({SYMBOL}), state),
            snapshot_scope={SYMBOL},
        )

    try:
        for account in ("main", follower):
            await _flatten(client, account, original_hedge[account], f"mode-pre-{account}-{base}")
            if original_hedge[account]:
                await client.set_position_mode(account, False)
            assert not any(p.position_amt != 0 for p in await client.position_risk(account))
            assert not await client.open_orders(account)
            if original_multi_assets[account]:
                await client.set_multi_assets_mode(account, False)
                assert not await client.get_multi_assets_mode(account)

        for index, desired in enumerate(("ISOLATED", "CROSSED")):
            opposite = "CROSSED" if desired == "ISOLATED" else "ISOLATED"
            for account, mode in (("main", desired), (follower, opposite)):
                await client.set_margin_type(account, SYMBOL, mode)
                observed = next(
                    p.margin_type.upper()
                    for p in await client.position_risk(account, SYMBOL)
                    if p.position_side == PosSide.BOTH
                )
                assert observed in ({"CROSS", "CROSSED"} if mode == "CROSSED" else {mode})

            master_flat = next(
                p
                for p in await client.position_risk("main", SYMBOL)
                if p.position_side == PosSide.BOTH
            )
            mark = master_flat.mark_price
            minimum_qty = _snap(filters.min_notional / mark + filters.step_size, filters.step_size)
            open_qty = max(filters.min_qty, minimum_qty)
            available = next(
                b.available_balance for b in await client.balance("main") if b.asset == "USDT"
            )
            assert open_qty * mark / D(max(master_flat.leverage, 1)) < available * D("0.25")
            await _place(
                client,
                "main",
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.BUY,
                    type="MARKET",
                    quantity=open_qty,
                    position_side=PosSide.BOTH,
                    new_order_resp_type="RESULT",
                    new_client_order_id=f"mode-m-open-{base}-{index}"[:36],
                ),
            )
            master_pos = next(
                p
                for p in await client.position_risk("main", SYMBOL)
                if p.position_side == PosSide.BOTH and p.position_amt > 0
            )
            stop = _snap(master_pos.entry_price * D("0.97"), filters.tick_size)
            assert stop < master_pos.mark_price
            await _place(
                client,
                "main",
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL,
                    type="STOP_MARKET",
                    quantity=master_pos.position_amt,
                    stop_price=stop,
                    reduce_only=True,
                    position_side=PosSide.BOTH,
                    working_type="MARK_PRICE",
                    new_order_resp_type="ACK",
                    new_client_order_id=f"mode-m-stop-{base}-{index}"[:36],
                ),
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                await drive()
                if await _position_qty(client, follower) > 0 and await _conditionals(
                    client, follower
                ):
                    break
                await asyncio.sleep(1)
            else:
                pytest.fail(f"real follower did not open/protect in {desired} mode")

            follower_pos = next(
                p
                for p in await client.position_risk(follower, SYMBOL)
                if p.position_side == PosSide.BOTH and p.position_amt > 0
            )
            assert follower_pos.margin_type.upper() in (
                {"CROSS", "CROSSED"} if desired == "CROSSED" else {"ISOLATED"}
            )
            assert follower_pos.liquidation_price < stop * D("0.995")
            if desired == "ISOLATED":
                assert follower_pos.isolated_margin is not None
                stop_loss = (
                    follower_pos.position_amt * abs(follower_pos.entry_price - stop) * D("1.001")
                )
                assert follower_pos.isolated_margin >= D(2) * stop_loss
                collateral_cap = int(
                    follower_pos.entry_price
                    // (D(2) * abs(follower_pos.entry_price - stop) * D("1.001"))
                )
                assert follower_pos.leverage <= collateral_cap
                print(
                    f"LIVE {desired}: leverage={follower_pos.leverage} "
                    f"isolatedMargin={follower_pos.isolated_margin} "
                    f"required={D(2) * stop_loss} "
                    f"liquidationPrice={follower_pos.liquidation_price} stop={stop}"
                )
            else:
                print(
                    f"LIVE {desired}: leverage={follower_pos.leverage} "
                    f"liquidationPrice={follower_pos.liquidation_price} stop={stop}"
                )

            journal = await db.fetchall(
                "SELECT rowid, command_key, kind, status FROM execution_commands"
                " WHERE account_id=? ORDER BY rowid",
                (follower,),
            )
            kinds = [str(row["kind"]) for row in journal]
            assert kinds.count("SET_MARGIN_TYPE") == index + 1, kinds
            mode_rowids = [int(row["rowid"]) for row in journal if row["kind"] == "SET_MARGIN_TYPE"]
            open_rowids = [int(row["rowid"]) for row in journal if row["kind"] == "MARKET_OPEN"]
            assert len(open_rowids) == index + 1, kinds
            assert mode_rowids[-1] < open_rowids[-1]
            assert all(str(row["status"]) == "CONFIRMED" for row in journal)

            # Crash-window replay is resolved by another signed V3 positionRisk
            # read, never by assuming that a no-coid config POST was accepted.
            latest_mode = next(row for row in reversed(journal) if row["kind"] == "SET_MARGIN_TYPE")
            await CommandRepo(db).set_status(str(latest_mode["command_key"]), "UNKNOWN")
            resolution = await resolve_boot_unknowns(
                lambda _account: client, CommandRepo(db), only_account=follower
            )
            assert resolution.confirmed == 1 and resolution.unresolved == 0
            for _ in range(2):
                await drive()
            repeated = await db.fetchall(
                "SELECT kind FROM execution_commands WHERE account_id=? ORDER BY rowid",
                (follower,),
            )
            assert [str(row["kind"]) for row in repeated] == kinds
            assert await _position_qty(client, follower) == follower_pos.position_amt

            await _flatten(client, "main", False, f"mode-m-end-{base}-{index}")
            await drive()
            await _flatten(client, follower, False, f"mode-f-end-{base}-{index}")
            await drive()
            assert await _position_qty(client, "main") == 0
            assert await _position_qty(client, follower) == 0
            assert not await client.open_orders("main", symbol=SYMBOL)
            assert not await client.open_orders(follower, symbol=SYMBOL)
    finally:
        for account in ("main", follower):
            with contextlib.suppress(Exception):
                await _flatten(client, account, False, f"mode-final-{account}-{base}")
            assert await _position_qty(client, account) == 0
            assert not await client.open_orders(account, symbol=SYMBOL)
            old_mode = original_margin[account].upper()
            await client.set_margin_type(
                account, SYMBOL, "CROSSED" if old_mode == "CROSS" else old_mode
            )
            if original_multi_assets[account]:
                assert not any(p.position_amt != 0 for p in await client.position_risk(account))
                assert not await client.open_orders(account)
                await client.set_multi_assets_mode(account, True)
                assert await client.get_multi_assets_mode(account)
            if original_hedge[account]:
                await client.set_position_mode(account, True)


async def test_hedge_fixed_protection_resizes_long_and_short(stack):
    """REAL-03: production Coordinator/Planner/Executor against a REAL HEDGE
    follower. A fixed STOP is resized on both reduce and grow at the same
    trigger, separately for LONG and SHORT legs."""
    from copytradeaster.domain.models import (
        PositionKey,
        PositionState,
        ProtectiveOrder,
    )
    from copytradeaster.master.projector import MasterDelta, MasterState

    client: AsterClient = stack["client"]
    coordinator: Coordinator = stack["coordinator"]
    filters: SymbolFilters = stack["filters"][SYMBOL]
    fr_id = "copyc1"
    trades = CopyTradeRepo(stack["db"])
    test_trade_ids: list[int] = []
    # This test needs at least two follower lot steps so the middle state is
    # a genuine partial reduction rather than a legitimate dust-flat. A
    # higher isolated test policy plus a still-distant stop shapes that state
    # without touching the operator's production policy DB.
    await SettingsRepo(stack["db"]).upsert_policy(
        RiskPolicy(risk_percent=D("0.05"), max_leverage=20, max_entry_deviation_pct=D("1")),
        fr_id,
    )
    original_hedge = await client.get_position_mode(fr_id)
    await _flatten(client, fr_id, original_hedge, f"hedge-pre-{int(time.time())}")
    if not original_hedge:
        await client.set_position_mode(fr_id, True)

    async def current_leg(key: PositionKey):
        rows = await client.position_risk(fr_id, SYMBOL)
        return next((row for row in rows if row.position_side == key.pos_side), None)

    try:
        for pos_side, sign, close_side in (
            (PosSide.LONG, D(1), Side.SELL),
            (PosSide.SHORT, D(-1), Side.BUY),
        ):
            await _flatten(client, fr_id, True, f"hedge-leg-{pos_side.value}-{int(time.time())}")
            key = PositionKey(SYMBOL, pos_side)
            # This synthetic MasterState represents a new master episode. Use
            # the production episode lifecycle so old intentional-flat or
            # own-protective latches from the persistent signed-test DB cannot
            # suppress an otherwise valid entry.
            active = await trades.active_trade(SYMBOL, pos_side.value)
            if active is not None:
                await trades.close_trade(int(active["trade_id"]))
            test_trade_ids.append(
                await trades.open_trade(SYMBOL, pos_side.value, None, direction=int(sign))
            )
            # Seed a fresh persisted execution generation. Deleting the row
            # would reset revision to 1 and could intentionally collide with a
            # historical synthetic test transition that has no trade_id.
            await SyncBasisRepo(stack["db"]).bump_revision(
                fr_id,
                SYMBOL,
                pos_side.value,
                f"signed-hedge-seed-{time.time_ns()}",
            )
            rows = await client.position_risk(fr_id, SYMBOL)
            mode_row = next(row for row in rows if row.position_side == pos_side)
            mark = mode_row.mark_price
            # This test verifies fixed-quantity replacement, not triggering.
            # The isolated 5% policy and 1000-point distance ensure that the
            # first risk-sized leg has room for a non-zero half reduction.
            stop_level = _snap(
                mark - D("1000") if sign > 0 else mark + D("1000"), filters.tick_size
            )

            def state(
                master_abs: D,
                key=key,
                sign=sign,
                close_side=close_side,
                stop_level=stop_level,
                mark=mark,
                pos_side=pos_side,
            ) -> MasterState:
                stop = ProtectiveOrder(
                    client_order_id=f"master-{pos_side.value}",
                    order_id=1,
                    type="STOP",
                    side=close_side,
                    stop_price=stop_level,
                    reduce_only=False,
                    close_position=False,
                    working_type="MARK_PRICE",
                    orig_qty=master_abs,
                )
                position = PositionState(
                    key=key,
                    qty=master_abs * sign,
                    entry_price=mark,
                    leverage=20,
                    mark_price=mark,
                    liquidation_price=D(0),
                    # Margin mode is part of the authoritative master
                    # state; omitting it must stay a deliberate unknown
                    # mode and therefore blocks new exposure.
                    margin_type="CROSSED",
                    stop=stop,
                )
                return MasterState(available_usdt=D("100000"), positions={key: position})

            initial = state(D(1))
            await coordinator.on_master_delta(MasterDelta(frozenset({SYMBOL}), initial))
            first_position = await current_leg(key)
            assert first_position is not None and first_position.position_amt * sign > 0
            first_abs = abs(first_position.position_amt)
            first_stop = next(
                order
                for order in await client.open_orders(fr_id, SYMBOL)
                if order.type == "STOP" and order.position_side == pos_side
            )
            assert first_stop.orig_qty == first_abs
            assert first_stop.working_type == "MARK_PRICE"

            reduced = state(D("0.5"))
            await coordinator.on_master_delta(MasterDelta(frozenset({SYMBOL}), reduced))
            reduced_position = await current_leg(key)
            assert reduced_position is not None
            reduced_abs = abs(reduced_position.position_amt)
            assert D(0) < reduced_abs < first_abs
            reduced_stop = next(
                order
                for order in await client.open_orders(fr_id, SYMBOL)
                if order.type == "STOP" and order.position_side == pos_side
            )
            assert reduced_stop.orig_qty == reduced_abs
            assert reduced_stop.stop_price == stop_level

            grown = state(D(1))
            await coordinator.on_master_delta(MasterDelta(frozenset({SYMBOL}), grown))
            grown_position = await current_leg(key)
            assert grown_position is not None
            grown_abs = abs(grown_position.position_amt)
            assert grown_abs > reduced_abs
            grown_stop = next(
                order
                for order in await client.open_orders(fr_id, SYMBOL)
                if order.type == "STOP" and order.position_side == pos_side
            )
            assert grown_stop.orig_qty == grown_abs
            assert grown_stop.stop_price == stop_level
            assert grown_stop.client_order_id != reduced_stop.client_order_id
    finally:
        await _flatten(client, fr_id, True, f"hedge-clean-{int(time.time())}")
        for trade_id in test_trade_ids:
            await trades.close_trade(trade_id)
        if not original_hedge:
            await client.set_position_mode(fr_id, False)
