"""Signed testnet scenarios (marker `testnet`). Skipped unless CT_TESTNET=1.

Covers the §8 research checklist subset automatable here, PLUS the
review_stage1.md acceptance-gate scenarios that need REAL signed API keys:
server time drift, account reads, listenKey lifecycle, master open/partial/stop
replace cycle, insufficient margin - and the engine-level live proofs:
double identical close (transition idempotency), one-tick stop replace,
TP removal propagation. Builder e2e runs LAST; skipped without main keys.
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal as D

import pytest

from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import ExecutionPlan
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import NonceStore

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
QTY = D("0.002")
QTY_RED = D("0.001")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings()


@pytest.fixture
async def db(settings):
    database = Database(settings.db_path)
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def creds(settings, db):
    c = SqliteCredentials(db, settings.encryption_key_bytes())
    await c.load()
    if not c.account_ids():
        pytest.skip("no accounts imported - run import-wallet first")
    return c


@pytest.fixture
async def clients(settings, creds, db):
    import httpx

    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail(f"signed tests are restricted to testnet, got {settings.rest_base_url!r}")
    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.aster.trading_api import AsterClient

    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    main = AsterClient(http, settings.rest_base_url, limiter, creds, creds, NonceStore(db))
    yield {"main": main}


async def test_server_time_drift(clients):
    server_ms = await clients["main"].server_time()
    assert abs(server_ms - int(time.time() * 1000)) < 5000


async def test_account_reads_both_roles(clients, creds):
    for account_id in creds.account_ids():
        balances = await clients["main"].balance(account_id)
        assert any(b.asset == "USDT" for b in balances)
        positions = await clients["main"].position_risk(account_id)
        assert isinstance(positions, list)
        orders = await clients["main"].open_orders(account_id)
        assert isinstance(orders, list)


async def test_listen_key_lifecycle(clients, creds):
    main_id = "main" if "main" in creds.account_ids() else creds.account_ids()[0]
    key = await clients["main"].start_listen_key(main_id)
    assert key
    await clients["main"].keepalive_listen_key(main_id)
    await clients["main"].close_listen_key(main_id)


async def test_master_market_open_partial_close_and_stop_replace(clients, creds):
    """Full cycle on the master account only; follower copy is covered by e2e."""
    client = clients["main"]
    main_id = next((a for a in creds.account_ids() if a == "main"), creds.account_ids()[0])
    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    hedge = await client.get_position_mode(main_id)
    pside = PosSide.LONG if hedge else PosSide.BOTH
    ro = not hedge
    info = await client.exchange_info(force=True)
    filters = info.tradable_filters()[SYMBOL]
    rows = await client.position_risk(main_id, SYMBOL)
    mark = D(str(rows[0].mark_price)) if rows else D("65000")

    open_req = PlaceOrderRequest(
        symbol=SYMBOL,
        side=Side.BUY,
        type="MARKET",
        quantity=QTY,
        new_order_resp_type="RESULT",
        position_side=pside,
        new_client_order_id=f"tn-open-{int(time.time())}",
    )
    order = await client.place_order(main_id, open_req)
    assert order.status in ("FILLED", "NEW")

    stop_price = (mark - D("500")).quantize(filters.tick_size)
    stop = await client.place_order(
        main_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=QTY,
            stop_price=stop_price,
            reduce_only=ro,
            working_type="MARK_PRICE",
            position_side=pside,
            new_client_order_id=f"tn-sl-{int(time.time())}",
        ),
    )
    # replace via cancel+new
    await client.cancel_order(main_id, SYMBOL, orig_client_order_id=stop.client_order_id)
    new_stop = (mark - D("600")).quantize(filters.tick_size)
    replaced = await client.place_order(
        main_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=QTY,
            stop_price=new_stop,
            reduce_only=ro,
            working_type="MARK_PRICE",
            position_side=pside,
            new_client_order_id=f"tn-sl2-{int(time.time())}",
        ),
    )
    assert replaced.stop_price == new_stop

    tp = await client.place_order(
        main_id,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            quantity=QTY,
            stop_price=(mark + D("800")).quantize(filters.tick_size),
            reduce_only=ro,
            working_type="MARK_PRICE",
            position_side=pside,
            new_client_order_id=f"tn-tp-{int(time.time())}",
        ),
    )
    assert tp.status in ("NEW", "FILLED")

    # partial close 25%
    partial_qty = (QTY * D("0.25")).quantize(filters.step_size)
    if partial_qty >= filters.min_qty:
        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=partial_qty,
                reduce_only=ro,
                new_order_resp_type="RESULT",
                position_side=pside,
                new_client_order_id=f"tn-pc-{int(time.time())}",
            ),
        )

    # cleanup: cancel conditionals and flatten
    for o in await client.open_orders(main_id, symbol=SYMBOL):
        await client.cancel_order(main_id, SYMBOL, orig_client_order_id=o.client_order_id)
        await asyncio.sleep(0.05)
    pos_list = await client.position_risk(main_id, SYMBOL)
    remaining = abs(pos_list[0].position_amt) if pos_list else D(0)
    if remaining > 0:
        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=remaining,
                reduce_only=ro,
                new_order_resp_type="RESULT",
                position_side=pside,
                new_client_order_id=f"tn-flat-{int(time.time())}",
            ),
        )


async def test_insufficient_margin_fails_cleanly(clients, creds):
    from copytradeaster.aster.trading_api import AsterError, PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    client = clients["main"]
    main_id = next((a for a in creds.account_ids() if a == "main"), creds.account_ids()[0])
    pside = PosSide.LONG if await client.get_position_mode(main_id) else PosSide.BOTH
    huge = D("5")  # within MARKET_LOT_SIZE, beyond available margin
    with pytest.raises(AsterError) as exc_info:
        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=huge,
                new_order_resp_type="RESULT",
                position_side=pside,
                new_client_order_id=f"tn-big-{int(time.time())}",
            ),
        )
    assert exc_info.value.code in (-2018, -2019, -4014, -4131)


# --- acceptance-gate scenarios over the REAL exchange --------------------------


async def _is_hedge(client) -> bool:
    return bool(await client.get_position_mode("main"))


async def _flatten_main(client) -> None:
    """Best-effort flatten BTC position and clear protections before a scenario."""
    import contextlib

    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import Side

    for o in await client.open_orders("main", SYMBOL):
        with contextlib.suppress(Exception):
            await client.cancel_order("main", SYMBOL, orig_client_order_id=o.client_order_id)
    for p in await client.position_risk("main", SYMBOL):
        if p.symbol == SYMBOL and p.position_amt != 0:
            with contextlib.suppress(Exception):
                await client.place_order(
                    "main",
                    PlaceOrderRequest(
                        symbol=SYMBOL,
                        side=Side.SELL if p.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(p.position_amt),
                        reduce_only=p.position_side.value == "BOTH",
                        position_side=p.position_side,
                        new_client_order_id=f"tn-flat-{int(time.time() * 1000)}",
                        new_order_resp_type="RESULT",
                    ),
                )


def _executor(clients, db):
    from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo
    from copytradeaster.trading.executor import Executor

    return Executor(lambda aid: clients["main"], CommandRepo(db), OrderLinkRepo(db), {})


def _close_action(coid: str, qty: D, revision: int = 0, hedge: bool = False):
    from copytradeaster.domain.models import ActionKind, ExecutionAction

    payload = {
        "symbol": SYMBOL,
        "side": "SELL",
        "type": "MARKET",
        "quantity": str(qty),
        "newOrderRespType": "RESULT",
    }
    if hedge:
        payload["positionSide"] = "LONG"
    else:
        payload["reduceOnly"] = "true"
    return ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol=SYMBOL,
        payload=payload,
        client_order_id=coid,
        replaces_client_order_id=None,
        priority=1,
        revision=revision,
    )


async def _open_long(client, qty: D, hedge: bool) -> None:
    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    await client.place_order(
        "main",
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=qty,
            position_side=PosSide.LONG if hedge else PosSide.BOTH,
            new_client_order_id=f"tn-open-{int(time.time() * 1000)}",
            new_order_resp_type="RESULT",
        ),
    )


def _prot_payload(hedge: bool, stop_price: str, order_type: str, qty: str = "0.002") -> dict:
    payload = {
        "symbol": SYMBOL,
        "side": "SELL",
        "type": order_type,
        "quantity": qty,
        "stopPrice": stop_price,
    }
    if hedge:
        payload["positionSide"] = "LONG"  # script-style HEDGE protection
    else:
        payload["reduceOnly"] = "true"
    return payload


async def _btc_position(client) -> D:
    for p in await client.position_risk("main", SYMBOL):
        if p.position_side.value in ("BOTH", "LONG"):
            return p.position_amt
    return D(0)


async def test_double_identical_close_sends_both_commands(clients, db):
    """THE idempotency regression, live: two consecutive reductions with
    IDENTICAL payloads are distinct transitions (revision 0 vs 1) - the second
    close must really reach the exchange instead of being swallowed by the
    first command's CONFIRMED key."""
    client = clients["main"]
    executor = _executor(clients, db)
    await _flatten_main(client)

    hedge = await _is_hedge(client)
    await _open_long(client, D("0.003"), hedge)
    assert await _btc_position(client) >= D("0.003")

    ts = int(time.time() * 1000)
    # rerunnable: revisions are unique PER RUN (epoch-based) yet stable within
    # this run, so retries keep one logical id while a re-RUN gets fresh keys
    base_rev = int(time.time()) % 1_000_000_000
    plan1 = ExecutionPlan(
        account_id="main",
        trade_id=None,
        actions=[_close_action(f"tn-c1a-{ts}", QTY_RED, revision=base_rev, hedge=hedge)],
    )
    r1 = await executor.submit(plan1)
    assert all(r.status == "CONFIRMED" for r in r1), r1
    mid = await _btc_position(client)
    assert mid <= D("0.002"), mid

    # identical payload; only the transition revision differs
    plan2 = ExecutionPlan(
        account_id="main",
        trade_id=None,
        actions=[_close_action(f"tn-c1b-{ts}", QTY_RED, revision=base_rev + 1, hedge=hedge)],
    )
    r2 = await executor.submit(plan2)
    assert all(r.status == "CONFIRMED" for r in r2), r2
    final = await _btc_position(client)
    assert final <= mid - QTY_RED + D("0.0000001"), final  # second close executed


async def test_one_tick_stop_replace_reaches_exchange(clients, db):
    """A one-tick stop move is a REAL change on the exchange grid: the engine
    must replace the resting order, not no-op it."""
    from copytradeaster.domain.models import ActionKind, ExecutionAction

    client = clients["main"]
    executor = _executor(clients, db)
    await _flatten_main(client)
    hedge = await _is_hedge(client)
    await _open_long(client, D("0.002"), hedge)
    pos = next(p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0)
    tick = D("0.1")
    base_sp = ((pos.entry_price - D("1500")) / tick).to_integral_value() * tick
    ts = int(time.time() * 1000)
    place = ExecutionAction(
        kind=ActionKind.PLACE_STOP,
        symbol=SYMBOL,
        payload=_prot_payload(hedge, str(base_sp), "STOP_MARKET"),
        client_order_id=f"tn-st-a-{ts}",
        replaces_client_order_id=None,
        priority=1,
        revision=0,
    )
    res = await executor.submit(ExecutionPlan("main", None, [place]))
    assert res[0].status == "CONFIRMED", res
    resting = {o.stop_price: o for o in await client.open_orders("main", SYMBOL)}
    assert base_sp in resting
    old_coid = resting[base_sp].client_order_id

    moved = base_sp + tick  # ONE tick: smallest real move
    place2 = ExecutionAction(
        kind=ActionKind.PLACE_STOP,
        symbol=SYMBOL,
        payload=_prot_payload(hedge, str(moved), "STOP_MARKET"),
        client_order_id=f"tn-st-b-{ts}",
        replaces_client_order_id=old_coid,
        priority=2,
        revision=1,
    )
    cancel_old = ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol=SYMBOL,
        payload={"symbol": SYMBOL, "origClientOrderId": old_coid},
        client_order_id=None,
        replaces_client_order_id=old_coid,
        priority=2,
        revision=1,
        depends_on_client_order_id=place2.client_order_id,
    )
    res2 = await executor.submit(ExecutionPlan("main", None, [place2, cancel_old]))
    assert all(r.status == "CONFIRMED" for r in res2), res2
    levels = {o.stop_price for o in await client.open_orders("main", SYMBOL)}
    assert moved in levels and base_sp not in levels
    await _flatten_main(client)


async def test_tp_removal_cancels_resting_take_profit(clients, db):
    """Master TP removal must cancel its own resting TP (target-state semantics)."""
    from copytradeaster.domain.models import ActionKind, ExecutionAction

    client = clients["main"]
    executor = _executor(clients, db)
    await _flatten_main(client)

    hedge = await _is_hedge(client)
    await _open_long(client, D("0.002"), hedge)
    pos = next(p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0)
    tp_level = ((pos.entry_price + D("2000")) / D("0.1")).to_integral_value() * D("0.1")
    ts = int(time.time() * 1000)
    place = ExecutionAction(
        kind=ActionKind.PLACE_TP,
        symbol=SYMBOL,
        payload=_prot_payload(hedge, str(tp_level), "TAKE_PROFIT_MARKET"),
        client_order_id=f"tn-tp-a-{ts}",
        replaces_client_order_id=None,
        priority=1,
        revision=0,
    )
    res = await executor.submit(ExecutionPlan("main", None, [place]))
    assert res[0].status == "CONFIRMED"
    tps = [o for o in await client.open_orders("main", SYMBOL) if o.type.startswith("TAKE_PROFIT")]
    assert tps, "resting TP expected"
    coid = tps[0].client_order_id
    cancel = ExecutionAction(
        kind=ActionKind.CANCEL_ORDER,
        symbol=SYMBOL,
        payload={"symbol": SYMBOL, "origClientOrderId": coid},
        client_order_id=None,
        replaces_client_order_id=coid,
        priority=1,
        revision=1,
    )
    res2 = await executor.submit(ExecutionPlan("main", None, [cancel]))
    assert res2[0].status == "CONFIRMED"
    tps_after = [
        o for o in await client.open_orders("main", SYMBOL) if o.type.startswith("TAKE_PROFIT")
    ]
    assert tps_after == []
    await _flatten_main(client)


async def test_one_way_signed_cycle(clients, creds):
    """Run the core open/protect/replace/partial-close cycle in ONE_WAY mode.

    The account mode is restored in ``finally`` so this live proof does not
    leave the local testnet wallet in a different configuration.
    """
    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    client = clients["main"]
    main_id = next((a for a in creds.account_ids() if a == "main"), creds.account_ids()[0])
    original_hedge = await client.get_position_mode(main_id)
    await _flatten_main(client)
    if original_hedge:
        await client.set_position_mode(main_id, False)

    ts = int(time.time() * 1000)
    try:
        info = await client.exchange_info(force=True)
        filters = info.tradable_filters()[SYMBOL]
        open_order = await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=QTY,
                position_side=PosSide.BOTH,
                new_client_order_id=f"tn-ow-open-{ts}",
                new_order_resp_type="RESULT",
            ),
        )
        assert open_order.status in ("FILLED", "NEW")
        position = next(
            p for p in await client.position_risk(main_id, SYMBOL) if p.position_amt != 0
        )
        assert position.position_side == PosSide.BOTH
        assert abs(position.position_amt) >= QTY

        first_stop_price = (
            (position.entry_price - D("500")) / filters.tick_size
        ).to_integral_value() * filters.tick_size
        first_stop = await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=QTY,
                stop_price=first_stop_price,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_client_order_id=f"tn-ow-sl-a-{ts}",
            ),
        )
        assert first_stop.status in ("NEW", "FILLED")
        await client.cancel_order(main_id, SYMBOL, orig_client_order_id=first_stop.client_order_id)

        second_stop_price = first_stop_price - filters.tick_size
        second_stop = await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=QTY,
                stop_price=second_stop_price,
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_client_order_id=f"tn-ow-sl-b-{ts}",
            ),
        )
        assert second_stop.stop_price == second_stop_price

        await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=QTY_RED,
                reduce_only=True,
                position_side=PosSide.BOTH,
                new_client_order_id=f"tn-ow-partial-{ts}",
                new_order_resp_type="RESULT",
            ),
        )
        remaining = next(
            (
                abs(p.position_amt)
                for p in await client.position_risk(main_id, SYMBOL)
                if p.position_amt
            ),
            D(0),
        )
        assert remaining < QTY

        tp = await client.place_order(
            main_id,
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="TAKE_PROFIT_MARKET",
                quantity=remaining,
                stop_price=position.entry_price + D("800"),
                reduce_only=True,
                position_side=PosSide.BOTH,
                working_type="MARK_PRICE",
                new_client_order_id=f"tn-ow-tp-{ts}",
            ),
        )
        assert tp.status in ("NEW", "FILLED")

        for order in await client.open_orders(main_id, symbol=SYMBOL):
            await client.cancel_order(main_id, SYMBOL, orig_client_order_id=order.client_order_id)
        if remaining > 0:
            await client.place_order(
                main_id,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL,
                    type="MARKET",
                    quantity=remaining,
                    reduce_only=True,
                    position_side=PosSide.BOTH,
                    new_client_order_id=f"tn-ow-flat-{ts}",
                    new_order_resp_type="RESULT",
                ),
            )
        assert await _btc_position(client) == 0
        assert await client.open_orders(main_id, symbol=SYMBOL) == []
    finally:
        await _flatten_main(client)
        if original_hedge:
            await client.set_position_mode(main_id, True)


async def test_builder_e2e_last(clients, creds, settings, db):
    """Runs LAST among signed scenarios; skipped when accounts lack main keys."""
    import httpx

    from copytradeaster.aster.management_api import ManagementClient
    from copytradeaster.aster.rate_limits import RateLimits

    targets = [a for a in creds.account_ids() if creds.has_main_key(a)]
    if not targets:
        pytest.skip("no main private keys stored (builder step of verification)")
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    mgmt = ManagementClient(
        http=http,
        base_url=settings.rest_base_url,
        limiter=limiter,
        identities=creds,
        keys=creds,
        nonces=NonceStore(db),
        cfg=settings,
    )
    expired_ms = int(time.time() * 1000) + 30 * 24 * 3600 * 1000
    results = {}
    for account_id in targets:
        agent_resp = await mgmt.approve_agent(
            account_id,
            agent_name=settings.builder_name,
            expired_ms=expired_ms,
            with_builder=bool(settings.builder_address),
        )
        builder_resp = await mgmt.approve_builder(account_id)
        results[account_id] = (agent_resp, builder_resp)
    print("builder e2e results:", results)
    await http.aclose()
