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
import signal
import sys
import time
from decimal import Decimal as D

import pytest

from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import ActionKind, ExecutionAction, ExecutionPlan, PosSide
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import CommandRepo, NonceStore, OrderLinkRepo, SettingsRepo
from copytradeaster.trading.executor import Executor

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
def settings(tmp_path_factory) -> Settings:
    return Settings(
        log_path=tmp_path_factory.mktemp("signed-logs") / "copytradeaster.log",
    )


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


async def test_follower_agent_permissions_signed_v3(clients, creds, settings):
    """Prove the Stage 2 USER_DATA Agent endpoint with persisted follower keys."""
    follower_ids = creds.account_ids("follower")
    if not follower_ids:
        pytest.skip("no follower credentials imported")
    client = clients["main"]
    for account_id in follower_ids:
        agent = await client.agent(account_id)
        identity = creds.identity(account_id)
        assert agent.agent_address is not None
        assert agent.agent_address.casefold() == identity.signer.casefold()
        assert agent.can_read is True
        assert agent.can_perp_trade is True
        assert agent.can_withdraw is False
        assert agent.expired is not None and agent.expired > int(time.time() * 1000)
        if settings.require_agent_ip_whitelist and not settings.testnet_ip_whitelist_override:
            assert agent.ip_whitelist


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


async def _run_pm_b1_sigkill_worker(journal_path: str) -> None:
    """Send one real MARKET and die after Aster accepts it.

    This is intentionally a process boundary instead of an exception fake:
    SQLite must retain ``recovery_context`` + ``SENDING`` after SIGKILL, while
    the HTTP response and every in-memory object disappear.
    """
    from pathlib import Path

    import httpx

    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.aster.trading_api import AsterClient
    from copytradeaster.storage.repositories import NonceStore

    worker_settings = Settings()
    if "testnet" not in worker_settings.rest_base_url.lower():
        raise RuntimeError("PM-B1 SIGKILL worker is restricted to testnet")

    source_db = Database(worker_settings.db_path)
    await source_db.connect()
    worker_creds = SqliteCredentials(source_db, worker_settings.encryption_key_bytes())
    await worker_creds.load()
    if "main" not in worker_creds.account_ids():
        raise RuntimeError("main credentials are required")

    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        worker_settings.weight_limit_per_min,
        worker_settings.orders_limit_per_min,
        worker_settings.orders_limit_per_10s,
    )
    real = AsterClient(
        http,
        worker_settings.rest_base_url,
        limiter,
        worker_creds,
        worker_creds,
        NonceStore(source_db),
    )
    journal = Database(Path(journal_path))
    await journal.connect()
    await journal.migrate()
    hedge = await real.get_position_mode("main")

    class KillAfterAcceptedClient:
        async def position_risk(self, account_id, symbol=None):
            return await real.position_risk(account_id, symbol)

        async def place_order(self, account_id, req):
            await real.place_order(account_id, req)
            os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError("SIGKILL returned")

    wrapped = KillAfterAcceptedClient()
    executor = Executor(
        lambda _account_id: wrapped,
        CommandRepo(journal),
        OrderLinkRepo(journal),
        {},
        settings=SettingsRepo(journal),
    )
    payload = {
        "symbol": SYMBOL,
        "side": "BUY",
        "type": "MARKET",
        "quantity": format(QTY, "f"),
        "newOrderRespType": "RESULT",
    }
    if hedge:
        payload["positionSide"] = PosSide.LONG.value
    action = ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol=SYMBOL,
        payload=payload,
        client_order_id="pm-b1-sigkill",
        replaces_client_order_id=None,
        priority=1,
    )
    await executor.submit(ExecutionPlan(account_id="main", trade_id=None, actions=[action]))
    raise AssertionError("worker survived accepted MARKET")


async def test_market_response_lost_recovers_from_real_all_orders_once(clients, creds, tmp_path):
    """PM-B1 signed proof: Aster accepts MARKET, its response is discarded,
    current-order lookup is hidden, and Executor resolves the original COID
    through real ``allOrders`` without a second economic mutation."""
    from copytradeaster.aster.trading_api import AsterError

    if "main" not in creds.account_ids():
        pytest.skip("main credentials are required for signed UNKNOWN recovery")
    real = clients["main"]
    await _flatten_main(real)
    hedge = await _is_hedge(real)
    position_side = PosSide.LONG if hedge else PosSide.BOTH
    journal = Database(tmp_path / "unknown-recovery.sqlite3")
    await journal.connect()
    await journal.migrate()

    class LostResponseClient:
        def __init__(self):
            self.place_calls = 0
            self.accepted_coid = ""

        async def position_risk(self, account_id, symbol=None):
            return await real.position_risk(account_id, symbol)

        async def place_order(self, account_id, req):
            self.place_calls += 1
            accepted = await real.place_order(account_id, req)
            self.accepted_coid = accepted.client_order_id
            raise AsterError(-1000, "injected lost response", http_status=503)

        async def query_order(self, account_id, symbol, **kwargs):
            del account_id, symbol, kwargs
            return None

        async def query_order_history(self, account_id, symbol, *, orig_client_order_id):
            return await real.query_order_history(
                account_id, symbol, orig_client_order_id=orig_client_order_id
            )

    wrapped = LostResponseClient()
    executor = Executor(
        lambda _account_id: wrapped,
        CommandRepo(journal),
        OrderLinkRepo(journal),
        {},
        settings=SettingsRepo(journal),
    )
    payload = {
        "symbol": SYMBOL,
        "side": "BUY",
        "type": "MARKET",
        "quantity": format(QTY, "f"),
        "newOrderRespType": "RESULT",
    }
    if hedge:
        payload["positionSide"] = position_side.value
    action = ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol=SYMBOL,
        payload=payload,
        client_order_id="pm-b1-live",
        replaces_client_order_id=None,
        priority=1,
    )
    try:
        result = await executor.submit(
            ExecutionPlan(account_id="main", trade_id=None, actions=[action])
        )
        assert result[0].status == "CONFIRMED"
        assert wrapped.place_calls == 1
        row = await CommandRepo(journal).get(result[0].command_key)
        assert row is not None
        assert row["client_order_id"] == wrapped.accepted_coid
        assert "history:FILLED" in str(row["last_error"])
        history = await real.query_order_history(
            "main", SYMBOL, orig_client_order_id=wrapped.accepted_coid
        )
        assert history is not None and history.executed_qty == QTY
    finally:
        await _flatten_main(real)
        positions = await real.position_risk("main", SYMBOL)
        assert all(position.position_amt == 0 for position in positions)
        assert await real.open_orders("main", SYMBOL) == []
        await journal.close()


async def test_market_sigkill_sending_recovers_without_second_order(clients, creds, tmp_path):
    """PM-B1 signed crash proof over a fresh Python process and real Aster."""
    from copytradeaster.trading.executor import resolve_boot_unknowns

    if "main" not in creds.account_ids():
        pytest.skip("main credentials are required for signed SIGKILL recovery")
    real = clients["main"]
    await _flatten_main(real)
    journal_path = tmp_path / "unknown-sigkill.sqlite3"
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            __file__,
            "--pm-b1-sigkill-worker",
            str(journal_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=45)
        except TimeoutError:
            process.kill()
            await process.wait()
            pytest.fail("PM-B1 SIGKILL worker timed out")
        assert process.returncode == -signal.SIGKILL

        journal = Database(journal_path)
        await journal.connect()
        try:
            commands = CommandRepo(journal)
            in_flight = await commands.in_flight()
            assert len(in_flight) == 1
            row = in_flight[0]
            assert row["status"] == "SENDING"
            assert row["recovery_context"]
            coid = str(row["client_order_id"])

            history_before = await real.query_order_history(
                "main", SYMBOL, orig_client_order_id=coid
            )
            assert history_before is not None and history_before.executed_qty == QTY
            report = await resolve_boot_unknowns(
                lambda _account_id: real,
                commands,
                OrderLinkRepo(journal),
            )
            assert report.confirmed == 1 and report.unresolved == 0
            recovered = await commands.get(str(row["command_key"]))
            assert recovered is not None and recovered["status"] == "CONFIRMED"
            history_after = await real.query_order_history(
                "main", SYMBOL, orig_client_order_id=coid
            )
            assert history_after is not None and history_after.order_id == history_before.order_id
            positions = await real.position_risk("main", SYMBOL)
            assert sum(abs(position.position_amt) for position in positions) == QTY
        finally:
            await journal.close()
    finally:
        await _flatten_main(real)
        assert all(
            position.position_amt == 0 for position in await real.position_risk("main", SYMBOL)
        )
        assert await real.open_orders("main", SYMBOL) == []


async def test_close_position_contract_one_way_and_hedge(clients):
    """REAL-01: Aster accepts closePosition without quantity/reduceOnly in
    both position modes and reports the normalized exchange fields."""
    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side

    client = clients["main"]
    original_hedge = await _is_hedge(client)
    try:
        for hedge in (False, True):
            await _flatten_main(client)
            if await _is_hedge(client) != hedge:
                await client.set_position_mode("main", hedge)
            position_side = PosSide.LONG if hedge else PosSide.BOTH
            stamp = int(time.time() * 1000)
            await client.place_order(
                "main",
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.BUY,
                    type="MARKET",
                    quantity=QTY,
                    position_side=position_side,
                    new_client_order_id=f"cp-open-{int(hedge)}-{stamp}"[:36],
                    new_order_resp_type="RESULT",
                ),
            )
            position = next(
                p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0
            )
            info = await client.exchange_info()
            tick = info.tradable_filters()[SYMBOL].tick_size
            for order_type, level in (
                ("STOP_MARKET", position.entry_price - D("1500")),
                ("TAKE_PROFIT_MARKET", position.entry_price + D("1500")),
            ):
                trigger = (level / tick).to_integral_value() * tick
                coid = f"cp-{order_type[:2].lower()}-{int(hedge)}-{stamp}"[:36]
                placed = await client.place_order(
                    "main",
                    PlaceOrderRequest(
                        symbol=SYMBOL,
                        side=Side.SELL,
                        type=order_type,
                        quantity=None,
                        stop_price=trigger,
                        reduce_only=False,
                        close_position=True,
                        position_side=position_side,
                        working_type="MARK_PRICE",
                        new_client_order_id=coid,
                    ),
                )
                queried = await client.query_order(
                    "main", SYMBOL, orig_client_order_id=placed.client_order_id
                )
                assert queried is not None
                assert queried.close_position is True
                assert queried.orig_qty == 0
                assert queried.position_side == position_side
                # Observed Aster response normalization: reduceOnly is true
                # although the signed request correctly omitted it.
                assert queried.reduce_only is True
                await client.cancel_order(
                    "main", SYMBOL, orig_client_order_id=placed.client_order_id
                )
        await _flatten_main(client)
    finally:
        await _flatten_main(client)
        if await _is_hedge(client) != original_hedge:
            await client.set_position_mode("main", original_hedge)


async def test_emergency_close_all_twice_uses_distinct_real_generations(clients, creds, db):
    """REAL-04: two identical real follower episodes produce two signed
    closes, distinct persisted operation ids, and remain paused afterwards."""
    import contextlib

    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.cli.main import _emergency_close_all, _set_pause
    from copytradeaster.domain.models import PosSide, Side
    from copytradeaster.storage.repositories import SettingsRepo

    if "copyc1" not in creds.account_ids():
        pytest.skip("copyc1 is required for emergency signed proof")
    client = clients["main"]

    async def flatten_copy() -> None:
        for order in await client.open_orders("copyc1", SYMBOL):
            with contextlib.suppress(Exception):
                await client.cancel_order(
                    "copyc1", SYMBOL, orig_client_order_id=order.client_order_id
                )
        for position in await client.position_risk("copyc1", SYMBOL):
            if position.position_amt == 0:
                continue
            with contextlib.suppress(Exception):
                await client.place_order(
                    "copyc1",
                    PlaceOrderRequest(
                        symbol=SYMBOL,
                        side=Side.SELL if position.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(position.position_amt),
                        reduce_only=position.position_side == PosSide.BOTH,
                        position_side=position.position_side,
                        new_client_order_id=f"emg-clean-{time.time_ns()}"[:36],
                        new_order_resp_type="RESULT",
                    ),
                )

    async def open_same_qty() -> None:
        hedge = await client.get_position_mode("copyc1")
        await client.place_order(
            "copyc1",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=QTY,
                position_side=PosSide.LONG if hedge else PosSide.BOTH,
                new_client_order_id=f"emg-open-{time.time_ns()}"[:36],
                new_order_resp_type="RESULT",
            ),
        )

    started = time.time()
    try:
        await flatten_copy()
        await _set_pause(False)
        await open_same_qty()
        assert await _emergency_close_all() == 0
        assert all(p.position_amt == 0 for p in await client.position_risk("copyc1", SYMBOL))

        await _set_pause(False)
        await open_same_qty()
        assert await _emergency_close_all() == 0
        assert all(p.position_amt == 0 for p in await client.position_risk("copyc1", SYMBOL))

        rows = await db.fetchall(
            "SELECT operation_id, status FROM execution_commands"
            " WHERE account_id='copyc1' AND kind='MARKET_CLOSE'"
            " AND operation_id IS NOT NULL AND created_at>=?",
            (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)),),
        )
        operation_ids = {str(row["operation_id"]) for row in rows}
        assert len(operation_ids) == 2, operation_ids
        assert all(str(row["status"]) == "CONFIRMED" for row in rows)
        settings = SettingsRepo(db)
        assert await settings.get_flag("allow_new_entries") == "false"
        assert await settings.get_flag("emergency_active") == "true"
    finally:
        await flatten_copy()


async def test_boot_recovery_real_found_and_not_found(clients, db):
    """REAL-05: resolve a real resting conditional and an authoritative
    missing clientOrderId without any duplicate placement."""
    import json

    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.domain.models import PosSide, Side
    from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo
    from copytradeaster.trading.executor import resolve_boot_unknowns

    client = clients["main"]
    await _flatten_main(client)
    hedge = await _is_hedge(client)
    position_side = PosSide.LONG if hedge else PosSide.BOTH
    stamp = time.time_ns()
    try:
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=QTY,
                position_side=position_side,
                new_client_order_id=f"boot-open-{stamp}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        position = next(
            p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0
        )
        level = (position.entry_price - D("1800")).quantize(D("0.1"))
        found_coid = f"boot-found-{stamp}"[:36]
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                quantity=QTY,
                stop_price=level,
                reduce_only=not hedge,
                position_side=position_side,
                working_type="MARK_PRICE",
                new_client_order_id=found_coid,
            ),
        )
        commands = CommandRepo(db)
        found_key = f"boot-found-key-{stamp}"
        await commands.insert(
            found_key,
            "main",
            None,
            "PLACE_STOP",
            json.dumps({"symbol": SYMBOL}),
            1,
            found_coid,
        )
        await commands.set_status(found_key, "SENDING")
        missing_key = f"boot-missing-key-{stamp}"
        missing_coid = f"boot-missing-{stamp}"[:36]
        await commands.insert(
            missing_key,
            "main",
            None,
            "PLACE_STOP",
            json.dumps({"symbol": SYMBOL}),
            1,
            missing_coid,
        )
        await commands.set_status(missing_key, "UNKNOWN")

        before = len(await client.open_orders("main", SYMBOL))
        report = await resolve_boot_unknowns(
            lambda _account_id: client, commands, OrderLinkRepo(db)
        )
        after = len(await client.open_orders("main", SYMBOL))
        assert report.confirmed >= 1 and report.failed_not_found >= 1
        assert (await commands.get(found_key))["status"] == "CONFIRMED"
        assert (await commands.get(missing_key))["status"] == "FAILED"
        assert before == after
    finally:
        await _flatten_main(client)


async def test_real_app_ws_ready_reconnect_and_cleanup(clients, creds, settings):
    """REAL-06: actual App reaches READY after WS-gated resnapshot, receives a
    real master order event, reconnects after listenKey close, and stops with
    ready=false without duplicating follower protection."""
    import contextlib

    from copytradeaster.app import App
    from copytradeaster.aster.trading_api import PlaceOrderRequest
    from copytradeaster.cli.main import _emergency_close_all, _set_pause
    from copytradeaster.domain.models import PosSide, Side
    from copytradeaster.storage.repositories import SettingsRepo

    real_followers = [aid for aid in ("copyc1", "copyc2", "copyc3") if aid in creds.account_ids()]
    if not real_followers:
        pytest.skip("at least one imported follower is required")
    client = clients["main"]
    await _emergency_close_all()
    await _set_pause(False)
    await _flatten_main(client)
    app = App(settings)
    started = False

    async def wait_for(predicate, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await predicate():
                return
            await asyncio.sleep(1)
        raise TimeoutError(f"timed out waiting for {what}")

    try:
        await app.start()
        started = True
        assert app._ready and app._stream_connect_count >= 1
        assert await app.settings.get_flag("ready") == "true"
        hedge = await client.get_position_mode("main")
        position_side = PosSide.LONG if hedge else PosSide.BOTH
        stamp = time.time_ns()
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=QTY,
                position_side=position_side,
                new_client_order_id=f"app-open-{stamp}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        position = next(
            p for p in await client.position_risk("main", SYMBOL) if p.position_amt != 0
        )
        stop_level = (position.entry_price - D("1200")).quantize(D("0.1"))
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
                new_client_order_id=f"app-stop-{stamp}"[:36],
            ),
        )

        async def followers_protected() -> bool:
            for account_id in real_followers:
                positions = await client.position_risk(account_id, SYMBOL)
                if not any(p.position_amt != 0 for p in positions):
                    return False
                protections = [
                    order
                    for order in await client.open_orders(account_id, SYMBOL)
                    if order.client_order_id.startswith("ct") and order.type.startswith("STOP")
                ]
                if len(protections) != 1:
                    return False
            return True

        await wait_for(followers_protected, 60, "followers protected from real WS event")
        before = {
            aid: (
                sum(
                    abs(p.position_amt)
                    for p in await client.position_risk(aid, SYMBOL)
                    if p.position_amt != 0
                ),
                len(
                    [
                        o
                        for o in await client.open_orders(aid, SYMBOL)
                        if o.client_order_id.startswith("ct") and o.type.startswith("STOP")
                    ]
                ),
            )
            for aid in real_followers
        }

        await app.master_client.close_listen_key("main")

        async def reconnected() -> bool:
            return app._stream_reconnect_count >= 1

        await wait_for(reconnected, 60, "master user stream reconnect")
        await asyncio.sleep(2)
        after = {
            aid: (
                sum(
                    abs(p.position_amt)
                    for p in await client.position_risk(aid, SYMBOL)
                    if p.position_amt != 0
                ),
                len(
                    [
                        o
                        for o in await client.open_orders(aid, SYMBOL)
                        if o.client_order_id.startswith("ct") and o.type.startswith("STOP")
                    ]
                ),
            )
            for aid in real_followers
        }
        assert after == before

        await client.cancel_order("main", SYMBOL, orig_client_order_id=master_stop.client_order_id)
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=abs(position.position_amt),
                reduce_only=not hedge,
                position_side=position_side,
                new_client_order_id=f"app-close-{stamp}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        # REAL-07: the critical signal itself is synthetic (we do not induce a
        # liquidation), but its callback performs a REAL authoritative REST
        # rebuild after the master was closed directly on the exchange.
        await app._on_master_critical("signed-synthetic-autoclose")

        async def everybody_flat() -> bool:
            for account_id in ["main", *real_followers]:
                if any(p.position_amt != 0 for p in await client.position_risk(account_id, SYMBOL)):
                    return False
            return True

        await wait_for(everybody_flat, 60, "App-driven follower cleanup")
    finally:
        with contextlib.suppress(Exception):
            await _flatten_main(client)
        if started:
            with contextlib.suppress(Exception):
                await app.stop()
        else:
            with contextlib.suppress(Exception):
                await app.http.aclose()
            with contextlib.suppress(Exception):
                await app.db.close()
        with contextlib.suppress(Exception):
            await _emergency_close_all()

    audit = Database(settings.db_path)
    await audit.connect()
    try:
        assert await SettingsRepo(audit).get_flag("ready") == "false"
    finally:
        await audit.close()


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


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--pm-b1-sigkill-worker":
        raise SystemExit("test module is executable only as the PM-B1 SIGKILL worker")
    try:
        asyncio.run(_run_pm_b1_sigkill_worker(sys.argv[2]))
    except BaseException:  # pragma: no cover - parent asserts SIGKILL, never a soft failure
        # aiosqlite owns non-daemon worker threads; force a distinct immediate
        # failure instead of letting a pre-SIGKILL setup error hang in finalize.
        os._exit(70)
