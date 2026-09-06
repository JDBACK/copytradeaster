"""Package A: pre-mainnet UNKNOWN live matrix (marker `testnet`).

Live-only qualification of the executor's ambiguous-send policy over the REAL
Aster Testnet (``fapi.asterdex-testnet.com``) with real ``wallet.txt`` accounts.
Every test drives real signed mutations through a real ``AsterClient``; fault
injection only wraps the real client around the UNKNOWN window (discarded
responses / unavailable recovery reads) and NEVER substitutes Shadow/Fake
state. Each test counts real economic ``place_order`` sends and ends flat: the
``finally`` block flattens the main BTCUSDT leg and re-audits flat position +
zero open orders with independent signed reads. Skipped unless ``CT_TESTNET=1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest

from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import (
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PosSide,
    Side,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CommandRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
)
from copytradeaster.trading.executor import Executor, command_key_of, resolve_boot_unknowns

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
        log_path=tmp_path_factory.mktemp("pre-mainnet-unknown-logs") / "copytradeaster.log",
    )


@pytest.fixture
async def creds(settings):
    """Real credentials, read from the provisioned store; never written here."""
    if not settings.db_path.exists():
        pytest.skip("no accounts imported - run import-wallet first")
    source = Database(settings.db_path)
    await source.connect()
    try:
        c = SqliteCredentials(source, settings.encryption_key_bytes())
        await c.load()
        if not c.account_ids():
            pytest.skip("no accounts imported - run import-wallet first")
        yield c
    finally:
        await source.close()


@pytest.fixture
async def journal(tmp_path):
    """Scratch executor journal; the provisioned store is never used for this."""
    database = Database(tmp_path / "pre-mainnet-unknown.sqlite3")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def clients(settings, creds, journal):
    import httpx

    settings.network_profile.require_testnet()
    from copytradeaster.aster.rate_limits import RateLimits

    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        settings.weight_limit_per_min, settings.orders_limit_per_min, settings.orders_limit_per_10s
    )
    main = AsterClient(http, settings.rest_base_url, limiter, creds, creds, NonceStore(journal))
    try:
        yield {"main": main}
    finally:
        await http.aclose()


async def _is_hedge(client: AsterClient, aid: str = "main") -> bool:
    return bool(await client.get_position_mode(aid))


async def _flatten_main(client: AsterClient, aid: str = "main", symbol: str = SYMBOL) -> None:
    """Best-effort flatten position and clear protections (REAL-05 convention)."""
    for o in await client.open_orders(aid, symbol):
        with contextlib.suppress(Exception):
            await client.cancel_order(aid, symbol, orig_client_order_id=o.client_order_id)
    for p in await client.position_risk(aid, symbol):
        if p.symbol == symbol and p.position_amt != 0:
            with contextlib.suppress(Exception):
                await client.place_order(
                    aid,
                    PlaceOrderRequest(
                        symbol=symbol,
                        side=Side.SELL if p.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(p.position_amt),
                        reduce_only=p.position_side.value == "BOTH",
                        position_side=p.position_side,
                        new_client_order_id=f"tn-flat-{int(time.time() * 1000)}",
                        new_order_resp_type="RESULT",
                    ),
                )


async def _audit_flat(client: AsterClient, aid: str = "main", symbol: str = SYMBOL) -> None:
    """Independent signed audit: flat position and zero open orders."""
    positions = await client.position_risk(aid, symbol)
    assert all(p.position_amt == 0 for p in positions)
    assert await client.open_orders(aid, symbol) == []


async def _btc_position(client: AsterClient, aid: str = "main", symbol: str = SYMBOL) -> D:
    for p in await client.position_risk(aid, symbol):
        if p.position_side.value in ("BOTH", "LONG"):
            return p.position_amt
    return D(0)


async def _open_long_real(
    client: AsterClient, hedge: bool, qty: D, tag: str, aid: str = "main"
) -> None:
    await client.place_order(
        aid,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=qty,
            position_side=PosSide.LONG if hedge else PosSide.BOTH,
            new_client_order_id=f"{tag}-{int(time.time() * 1000)}"[:36],
            new_order_resp_type="RESULT",
        ),
    )


def _open_action(
    planner_coid: str, hedge: bool, revision: int = 0, *, symbol: str = SYMBOL, qty: D = QTY
) -> ExecutionAction:
    payload = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": format(qty, "f"),
        "newOrderRespType": "RESULT",
    }
    if hedge:
        payload["positionSide"] = PosSide.LONG.value
    return ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol=symbol,
        payload=payload,
        client_order_id=planner_coid,
        replaces_client_order_id=None,
        priority=1,
        revision=revision,
    )


def _close_action(planner_coid: str, qty: D, hedge: bool, revision: int = 0) -> ExecutionAction:
    payload = {
        "symbol": SYMBOL,
        "side": "SELL",
        "type": "MARKET",
        "quantity": format(qty, "f"),
        "newOrderRespType": "RESULT",
    }
    if hedge:
        payload["positionSide"] = PosSide.LONG.value
    else:
        payload["reduceOnly"] = "true"
    return ExecutionAction(
        kind=ActionKind.MARKET_CLOSE,
        symbol=SYMBOL,
        payload=payload,
        client_order_id=planner_coid,
        replaces_client_order_id=None,
        priority=1,
        revision=revision,
    )


def _executor(client, journal) -> Executor:
    return Executor(
        lambda _account_id: client,
        CommandRepo(journal),
        OrderLinkRepo(journal),
        {},
        settings=SettingsRepo(journal),
    )


async def test_close_response_lost_resolves_via_real_history_once(clients, creds, journal):
    """A1: MARKET_CLOSE accepted but response lost resolves via real allOrders.

    Covers the history-FOUND branch of ``_resolve_exchange_outcome``: the
    current-order read is hidden, so the original COID must be confirmed from
    authoritative ``allOrders`` history with exactly one economic send.
    """
    if "main" not in creds.account_ids():
        pytest.skip("main credentials are required for signed UNKNOWN recovery")
    real = clients["main"]
    await _flatten_main(real)
    hedge = await _is_hedge(real)
    stamp = time.time_ns()
    await _open_long_real(real, hedge, QTY, f"a1-open-{stamp}"[-20:])
    assert await _btc_position(real) == QTY

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
    executor = _executor(wrapped, journal)
    plan = ExecutionPlan(
        account_id="main",
        trade_id=None,
        actions=[_close_action(f"a1-close-{stamp}", QTY, hedge, 1)],
    )
    try:
        result = await executor.submit(plan)
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
        assert await _btc_position(real) == 0
    finally:
        await _flatten_main(real)
        await _audit_flat(real)


async def test_open_all_recovery_reads_down_holds_unknown_without_retry(clients, creds, journal):
    """A2: accepted OPEN with every recovery read down holds UNKNOWN, no retry.

    Covers the UNRESOLVED branch of ``_resolve_exchange_outcome``: current
    order, order history, and position reads all fail, so the command must stay
    UNKNOWN under its original COID with zero follow-up sends (fail closed,
    even though the exchange really accepted the order).
    """
    if "main" not in creds.account_ids():
        pytest.skip("main credentials are required for signed UNKNOWN recovery")
    real = clients["main"]
    await _flatten_main(real)
    hedge = await _is_hedge(real)
    stamp = time.time_ns()

    class DarkRecoveryClient:
        def __init__(self):
            self.place_calls = 0
            self.accepted_coid = ""
            self._armed = False

        async def position_risk(self, account_id, symbol=None):
            if self._armed:
                raise AsterError(-1006, "injected position outage")
            return await real.position_risk(account_id, symbol)

        async def place_order(self, account_id, req):
            self.place_calls += 1
            accepted = await real.place_order(account_id, req)
            self.accepted_coid = accepted.client_order_id
            self._armed = True
            raise AsterError(-1000, "injected lost response", http_status=503)

        async def query_order(self, account_id, symbol, **kwargs):
            del account_id, symbol, kwargs
            raise AsterError(-1000, "injected current-order outage", http_status=503)

        async def query_order_history(self, account_id, symbol, *, orig_client_order_id):
            del account_id, symbol, orig_client_order_id
            raise AsterError(-1006, "injected history outage")

    wrapped = DarkRecoveryClient()
    executor = _executor(wrapped, journal)
    action = _open_action(f"a2-open-{stamp}", hedge, 2)
    plan = ExecutionPlan(account_id="main", trade_id=None, actions=[action])
    expected_coid = "ct" + command_key_of(plan, action)
    try:
        result = await executor.submit(plan)
        assert result[0].status == "UNKNOWN"
        assert wrapped.place_calls == 1
        row = await CommandRepo(journal).get(result[0].command_key)
        assert row is not None
        assert row["status"] == "UNKNOWN"
        assert row["client_order_id"] == expected_coid == wrapped.accepted_coid
        assert "unavailable" in str(row["last_error"])
        changed = replace(
            action, revision=action.revision + 1, payload={**action.payload, "quantity": "0.003"}
        )
        changed_plan = replace(plan, actions=[changed])
        blocked = await executor.submit(changed_plan)
        assert blocked[0].status == "BLOCKED"
        assert wrapped.place_calls == 1
        assert (await CommandRepo(journal).get(row["command_key"]))["status"] == "UNKNOWN"
        # Fail-closed proof: the exchange really holds the order, yet no
        # second economic send was issued under any COID.
        assert (
            await real.query_order_history(
                "main", SYMBOL, orig_client_order_id=wrapped.accepted_coid
            )
            is not None
        )
        # Heal through the same production resolver, with independent signed history.
        recovery = await resolve_boot_unknowns(
            lambda _: real, CommandRepo(journal), OrderLinkRepo(journal)
        )
        assert recovery.unresolved == 0
        assert (await CommandRepo(journal).get(row["command_key"]))["status"] == "CONFIRMED"
        assert wrapped.place_calls == 1
    finally:
        await _flatten_main(real)
        await _audit_flat(real)


async def test_partial_close_response_lost_confirms_by_real_position_delta(clients, creds, journal):
    """A3: a fully filled order reducing part of a position confirms from its full delta.

    Covers the position-progress FOUND branch of ``_resolve_exchange_outcome``:
    both order lookups are hidden, so the only evidence is the real
    ``positionRisk`` delta. The single accepted send must leave exactly the
    residual leg; no synthetic position is ever presented as evidence.
    """
    if "main" not in creds.account_ids():
        pytest.skip("main credentials are required for signed UNKNOWN recovery")
    real = clients["main"]
    await _flatten_main(real)
    hedge = await _is_hedge(real)
    stamp = time.time_ns()
    await _open_long_real(real, hedge, QTY, f"a3-open-{stamp}"[-20:])
    assert await _btc_position(real) == QTY

    class HiddenLookupClient:
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
            del account_id, symbol, orig_client_order_id
            raise AsterError(-1006, "injected history outage")

    wrapped = HiddenLookupClient()
    executor = _executor(wrapped, journal)
    plan = ExecutionPlan(
        account_id="main",
        trade_id=None,
        actions=[_close_action(f"a3-pclose-{stamp}", QTY_RED, hedge, 3)],
    )
    try:
        result = await executor.submit(plan)
        assert result[0].status == "CONFIRMED"
        assert wrapped.place_calls == 1
        row = await CommandRepo(journal).get(result[0].command_key)
        assert row is not None
        assert "position-progress" in str(row["last_error"])
        # Only the residual leg may remain: the partial send did the work, and
        # no continuation order flattened the rest.
        assert await _btc_position(real) == QTY - QTY_RED
    finally:
        await _flatten_main(real)
        await _audit_flat(real)


async def test_proven_not_executed_retries_exactly_once_with_new_coid(clients, creds, journal):
    """A4: runtime PROVEN_NOT_EXECUTED allows exactly one retry with a new COID.

    The first send never reaches the exchange (pre-send transport loss); real
    current-order, history, and position reads prove absence, so the executor
    may resend once under a fresh COID and must then stop.

    Runs on ``copyc3``/ETHUSDT: every BTCUSDT ``allOrders`` window is now
    saturated (1000/1000 on all accounts), so order-history absence is
    unprovable there by design (fail-closed UNRESOLVED); ETHUSDT windows are
    near-empty and admit real negative proof. Same contract, honest shaping.
    """
    aid = "copyc3"
    symbol = "ETHUSDT"
    qty = D("0.02")
    if aid not in creds.account_ids():
        pytest.skip(f"{aid} credentials are required for signed UNKNOWN recovery")
    real = clients["main"]
    await _flatten_main(real, aid, symbol)
    hedge = await _is_hedge(real, aid)
    stamp = time.time_ns()

    class DropFirstSendClient:
        def __init__(self):
            self.send_attempts = 0
            self.real_sends = 0

        async def position_risk(self, account_id, symbol=None):
            return await real.position_risk(account_id, symbol)

        async def place_order(self, account_id, req):
            self.send_attempts += 1
            if self.send_attempts == 1:
                raise TimeoutError("injected pre-send transport loss")
            self.real_sends += 1
            return await real.place_order(account_id, req)

        async def query_order(self, account_id, symbol, **kwargs):
            return await real.query_order(account_id, symbol, **kwargs)

        async def query_order_history(self, account_id, symbol, *, orig_client_order_id):
            return await real.query_order_history(
                account_id, symbol, orig_client_order_id=orig_client_order_id
            )

    wrapped = DropFirstSendClient()
    executor = _executor(wrapped, journal)
    action = _open_action(f"a4-open-{stamp}", hedge, 4, symbol=symbol, qty=qty)
    plan = ExecutionPlan(account_id=aid, trade_id=None, actions=[action])
    first_coid = "ct" + command_key_of(plan, action)
    try:
        result = await executor.submit(plan)
        assert result[0].status == "CONFIRMED"
        assert wrapped.send_attempts == 2
        assert wrapped.real_sends == 1
        row = await CommandRepo(journal).get(result[0].command_key)
        assert row is not None
        assert row["attempts"] == 2
        final_coid = str(row["client_order_id"])
        assert final_coid == first_coid + "r2"
        # The retried COID filled on the exchange; the dropped first COID
        # provably never existed there.
        history = await real.query_order_history(aid, symbol, orig_client_order_id=final_coid)
        assert history is not None and history.executed_qty == qty
        assert await real.query_order_history(aid, symbol, orig_client_order_id=first_coid) is None
        assert await _btc_position(real, aid, symbol) == qty
    finally:
        await _flatten_main(real, aid, symbol)
        await _audit_flat(real, aid, symbol)


async def _run_a5_sigkill_worker(journal_path: str) -> None:
    """Send one real MARKET and die inside the UNKNOWN window (no resolution)."""
    if os.getenv("CT_TESTNET") != "1":
        raise RuntimeError("signed worker requires CT_TESTNET=1")

    from pathlib import Path

    import httpx

    from copytradeaster.aster.rate_limits import RateLimits
    from copytradeaster.aster.trading_api import AsterClient
    from copytradeaster.storage.repositories import NonceStore

    worker_settings = Settings()
    if "testnet" not in worker_settings.rest_base_url.lower():
        raise RuntimeError("A5 SIGKILL worker is restricted to testnet")
    source_db = Database(worker_settings.db_path)
    await source_db.connect()
    try:
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

        class DieAfterAcceptedClient:
            async def position_risk(self, account_id, symbol=None):
                return await real.position_risk(account_id, symbol)

            async def place_order(self, account_id, req):
                await real.place_order(account_id, req)
                os.kill(os.getpid(), signal.SIGKILL)
                raise AssertionError("SIGKILL returned")

        executor = Executor(
            lambda _account_id: DieAfterAcceptedClient(),
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
            client_order_id="a5-sigkill",
            replaces_client_order_id=None,
            priority=1,
            revision=5,
        )
        await executor.submit(ExecutionPlan(account_id="main", trade_id=None, actions=[action]))
    finally:
        await source_db.close()
    raise AssertionError("worker survived accepted MARKET")


async def test_sigkill_in_unknown_window_boot_resolves_without_second_order(
    clients, creds, tmp_path
):
    """A5: SIGKILL inside the UNKNOWN window resolves at boot with no resend.

    The worker's real MARKET is accepted by Aster, then the process dies before
    any resolution; the boot resolver must CONFIRM the persisted SENDING row
    from real exchange reads without issuing a second economic order.
    """
    if "main" not in creds.account_ids():
        pytest.skip("main credentials are required for signed SIGKILL recovery")
    real = clients["main"]
    await _flatten_main(real)
    journal_path = tmp_path / "a5-unknown-sigkill.sqlite3"
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            __file__,
            "--a5-sigkill-worker",
            str(journal_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=60)
        except TimeoutError:
            process.kill()
            await process.wait()
            pytest.fail("A5 SIGKILL worker timed out")
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
            # Deterministic worker COID: earlier A5 runs share it, and the
            # saturated allOrders window keeps them. The worker already sent
            # before dying, so boot must add ZERO new rows with this COID.
            matches_before = [
                o
                for o in await real.all_orders("main", SYMBOL, limit=1000)
                if o.client_order_id == coid
            ]
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
            assert history_after is not None
            assert history_after.order_id == history_before.order_id
            matches = [
                o
                for o in await real.all_orders("main", SYMBOL, limit=1000)
                if o.client_order_id == coid
            ]
            assert len(matches) == len(matches_before)
            assert await _btc_position(real) == QTY
        finally:
            await journal.close()
    finally:
        await _flatten_main(real)
        await _audit_flat(real)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--a5-sigkill-worker":
        asyncio.run(_run_a5_sigkill_worker(sys.argv[2]))
    else:
        raise SystemExit(
            "usage: test_pre_mainnet_unknown_live.py --a5-sigkill-worker <journal_path>"
        )


async def test_partial_position_progress_with_hidden_history_stays_unknown(clients, creds, journal):
    """A6: real open plus concurrent real reduction leaves insufficient position evidence.

    This deliberately creates two full fills, NOT a fabricated partial fill.
    With both order lookups unavailable, half the requested position delta is
    insufficient proof that the original command is complete.
    """
    if "main" not in creds.account_ids():
        pytest.skip("main credentials required")
    real = clients["main"]
    await _audit_flat(real)
    hedge = await _is_hedge(real)

    class HiddenOrders:
        def __init__(self):
            self.opens = 0

        async def position_risk(self, aid, symbol=None):
            return await real.position_risk(aid, symbol)

        async def query_order(self, *args, **kwargs):
            return None

        async def query_order_history(self, *args, **kwargs):
            raise AsterError(-1006, "injected history outage")

        async def place_order(self, aid, req):
            self.opens += 1
            await real.place_order(aid, req)
            await real.place_order(
                aid,
                PlaceOrderRequest(
                    symbol=SYMBOL,
                    side=Side.SELL,
                    type="MARKET",
                    quantity=QTY_RED,
                    position_side=PosSide.LONG if hedge else PosSide.BOTH,
                    reduce_only=not hedge,
                    new_client_order_id=f"a6-external-{time.time_ns()}"[:36],
                    new_order_resp_type="RESULT",
                ),
            )
            raise AsterError(-1000, "injected lost open response", http_status=503)

    wrapper = HiddenOrders()
    executor = _executor(wrapper, journal)
    action = _open_action(f"a6-{time.time_ns()}", hedge)
    plan = ExecutionPlan("main", None, [action])
    try:
        result = (await executor.submit(plan))[0]
        assert await _btc_position(real) == QTY - QTY_RED
        assert result.status == "UNKNOWN"
        blocked = await executor.submit(replace(plan, actions=[replace(action, revision=1)]))
        assert blocked[0].status == "BLOCKED" and wrapper.opens == 1
        recovered = await resolve_boot_unknowns(
            lambda _: real, CommandRepo(journal), OrderLinkRepo(journal)
        )
        assert recovered.unresolved == 0
        assert (await CommandRepo(journal).get(result.command_key))["status"] == "CONFIRMED"
        assert wrapper.opens == 1
    finally:
        await _flatten_main(real)
        await _audit_flat(real)
