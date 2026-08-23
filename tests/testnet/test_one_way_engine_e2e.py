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
from copytradeaster.trading.executor import Executor

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
OPEN_QTY = D("0.003")


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
async def stack():
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()
    creds = SqliteCredentials(db, settings.encryption_key_bytes())
    await creds.load()
    if "main" not in creds.account_ids() or "copyc1" not in creds.account_ids():
        await db.close()
        pytest.skip("need main + copyc1 imported - run import-wallet first")

    import httpx

    from copytradeaster.aster.rate_limits import RateLimits

    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    client = AsterClient(http, settings.rest_base_url, limiter, creds, creds, NonceStore(db))

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
    )
    projector = MasterProjector(
        MasterEventRepo(db),
        CopyTradeRepo(db),
        OrderLinkRepo(db),
        SettingsRepo(db),
        coordinator.on_master_delta,
    )

    info = await client.exchange_info(force=True)
    filters_cache.update(info.tradable_filters())
    if await SettingsRepo(db).get_policy("copyc1") is None:
        await SettingsRepo(db).upsert_policy(
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


def _master_prot(base: int, offset: int, coid: str, otype: str, sp: str, qty: str) -> dict:
    e = base + offset
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": e,
        "T": e,
        "o": {
            "s": SYMBOL,
            "c": coid,
            "S": "SELL",
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
    filters: SymbolFilters = stack["filters"][SYMBOL]
    tick, step = filters.tick_size, filters.step_size
    fr_id = "copyc1"
    base = int(time.time() * 1000)  # unique per run: master_events dedups persistently
    run_started = datetime.now(UTC).isoformat()

    async def wait_until(predicate, timeout_s: float, what: str, *, rebuilds: int = 1) -> None:
        """Wait for a condition; between attempts resnapshot the master
        authoritatively via REST (heals a lagging synthetic-event path)."""
        for attempt in range(rebuilds + 1):
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if await predicate():
                    return
                await asyncio.sleep(1.0)
            if attempt < rebuilds:
                await projector.rebuild_full(client, "main")
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
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=OPEN_QTY,
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

        # --- phase 5: master TP -> follower mirrors it -----------------------
        tp_level = _snap(entry + D("900"), tick)
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="TAKE_PROFIT_MARKET",
                quantity=abs(master_left),
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
                base, 5, f"m-tp-{base}", "TAKE_PROFIT_MARKET", str(tp_level), str(abs(master_left))
            )
        )

        async def follower_tp_placed() -> bool:
            tps = [
                o for o in await _conditionals(client, fr_id) if o.type.startswith("TAKE_PROFIT")
            ]
            return len(tps) == 1 and tps[0].stop_price == tp_level

        await wait_until(follower_tp_placed, 45, "follower TP placement", rebuilds=2)

        # --- phase 6: master closes -> follower flattens, no orders remain ---
        await _place(
            client,
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=abs(master_left),
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_order_resp_type="RESULT",
                new_client_order_id=f"owe2e-close-{base}"[:36],
            ),
        )
        await projector.apply_raw(
            _master_trade(base, 6, str(abs(master_left)), str(entry), side="SELL")
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
            if str(row["kind"]) in ("PLACE_STOP", "PLACE_TP", "MARKET_CLOSE"):
                assert payload.get("reduceOnly") == "true", (row["kind"], payload)
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
