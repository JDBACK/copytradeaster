"""LeverageManager control-plane: candidate catalog, POST-as-validation,
singleflight preparation, downshift invalidation and bounded background work.
All network surface is faked; no HTTP."""

import asyncio

from copytradeaster.aster.trading_api import AsterError
from copytradeaster.trading.leverage import (
    LeverageManager,
    PrepareReport,
)


class FakeLevClient:
    """AsterClient duck-type covering only the leverage control-plane calls."""

    def __init__(self, *, account_max: int = 200, reject_first_post: bool = False) -> None:
        self.account_max = account_max
        self.reject_first_post = reject_first_post
        self.bracket_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self._posts = 0

    async def set_leverage(self, account_id: str, symbol: str, leverage: int) -> None:
        self._posts += 1
        if self.reject_first_post and self._posts == 1:
            raise AsterError(-4028, "invalid leverage")
        self.set_calls.append((account_id, symbol, leverage))

    async def leverage_bracket(self, account_id: str, symbol: str):
        from copytradeaster.aster.models import LeverageBracketResponse

        self.bracket_calls.append((account_id, symbol))
        return LeverageBracketResponse.model_validate(
            {
                "symbol": symbol,
                "brackets": [{"bracket": 1, "initialLeverage": self.account_max}],
            }
        )


def make_manager(clients: dict[str, FakeLevClient], *, concurrency: int = 2, **kw):
    return LeverageManager(
        client_for=lambda account_id: clients[account_id],
        symbols=kw.pop("symbols", ["BTCUSDT", "ETHUSDT"]),
        accounts=kw.pop("accounts", lambda: []),
        catalog_account="main",
        concurrency=concurrency,
        pacing_s=0.0,
        **kw,
    )


async def test_prepare_uses_candidate_post_without_bracket_get():
    clients = {"main": FakeLevClient(), "copyc1": FakeLevClient()}
    manager = make_manager(clients)
    await manager.refresh_catalog()

    leverage = await manager.prepare_symbol("copyc1", "BTCUSDT")

    assert leverage == 200
    assert clients["copyc1"].set_calls == [("copyc1", "BTCUSDT", 200)]
    # POST-as-validation succeeded: no follower-specific bracket read at all
    assert clients["copyc1"].bracket_calls == []
    assert manager.is_ready("copyc1", "BTCUSDT")
    assert manager.confirmed("copyc1", "BTCUSDT") == 200


async def test_rejected_post_falls_back_to_account_bracket_and_lower_post():
    clients = {
        "main": FakeLevClient(account_max=200),
        # exchange capped this follower to 100
        "charlie": FakeLevClient(account_max=100, reject_first_post=True),
    }
    charlie = clients["charlie"]
    manager = make_manager(clients, symbols=["BTCUSDT"])
    await manager.refresh_catalog()

    leverage = await manager.prepare_symbol("charlie", "BTCUSDT")

    assert leverage == 100
    assert charlie.bracket_calls == [("charlie", "BTCUSDT")]
    assert charlie.set_calls[-1] == ("charlie", "BTCUSDT", 100)
    assert manager.is_ready("charlie", "BTCUSDT")


async def test_concurrent_prepare_same_key_sends_single_network_sequence():
    clients = {"main": FakeLevClient(), "copyc1": FakeLevClient()}
    manager = make_manager(clients, symbols=["BTCUSDT"])
    await manager.refresh_catalog()
    original = clients["copyc1"].set_leverage
    gate = asyncio.Event()

    async def slow_set(account_id, symbol, leverage):
        await gate.wait()
        await original(account_id, symbol, leverage)

    clients["copyc1"].set_leverage = slow_set  # type: ignore[method-assign]

    async def run_five():
        return await asyncio.gather(
            *(manager.prepare_symbol("copyc1", "BTCUSDT") for _ in range(5))
        )

    task = asyncio.create_task(run_five())
    # Let every caller capture its ticket and queue on the singleflight lock
    # behind the in-flight POST before releasing it.
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.wait_for(task, timeout=5)

    assert results == [200] * 5
    assert clients["copyc1"].set_calls == [("copyc1", "BTCUSDT", 200)]


async def test_catalog_downshift_invalidates_readiness_until_reprepared():
    clients: dict[str, FakeLevClient] = {
        "main": FakeLevClient(account_max=200),
        "copyc1": FakeLevClient(account_max=200),
    }
    manager = make_manager(clients, symbols=["BTCUSDT"])
    await manager.refresh_catalog()
    await manager.prepare_symbol("copyc1", "BTCUSDT")
    assert manager.is_ready("copyc1", "BTCUSDT")

    # Exchange lowers the maximum: new generation -> confirmation is stale.
    clients["main"].account_max = 100
    await manager.refresh_catalog()
    assert not manager.is_ready("copyc1", "BTCUSDT")

    # Re-preparation at the lower candidate restores readiness.
    leverage = await manager.prepare_symbol("copyc1", "BTCUSDT")
    assert leverage == 100
    assert clients["copyc1"].set_calls == [
        ("copyc1", "BTCUSDT", 200),
        ("copyc1", "BTCUSDT", 100),
    ]
    assert manager.is_ready("copyc1", "BTCUSDT")


async def test_failed_prepare_invalidates_and_reports_safe_category():
    clients = {"main": FakeLevClient(), "copyc1": FakeLevClient()}

    async def always_fail(account_id, symbol, leverage):
        raise AsterError(-2019, "margin insufficient")

    clients["copyc1"].set_leverage = always_fail  # type: ignore[method-assign]
    manager = make_manager(clients, symbols=["BTCUSDT"])
    await manager.refresh_catalog()

    report = await manager.prepare_account("copyc1", ["BTCUSDT"])

    assert isinstance(report, PrepareReport)
    assert report.confirmed == {}
    assert report.failed == {"BTCUSDT": "aster:-2019"}
    assert not manager.is_ready("copyc1", "BTCUSDT")
    assert manager.confirmed("copyc1", "BTCUSDT") is None


async def test_observe_restores_confirmed_state_after_restart_without_writes():
    # Fresh process: no prepare has ever run here. The authoritative snapshot
    # (positionRisk) says BTCUSDT=200 for this account.
    clients = {"main": FakeLevClient(), "copyc1": FakeLevClient()}
    manager = make_manager(clients, symbols=["BTCUSDT"])
    await manager.refresh_catalog()

    manager.observe("copyc1", {"BTCUSDT": 200})

    assert manager.is_ready("copyc1", "BTCUSDT")
    assert manager.confirmed("copyc1", "BTCUSDT") == 200
    # Reconstruction is a pure RAM fold of an already-paid-for read.
    assert clients["copyc1"].set_calls == []


async def test_bulk_prepare_respects_bounded_concurrency():
    inflight = {"n": 0, "max": 0}

    class CountingClient(FakeLevClient):
        async def set_leverage(self, account_id, symbol, leverage):
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            await asyncio.sleep(0.001)
            inflight["n"] -= 1
            self.set_calls.append((account_id, symbol, leverage))

    clients = {"main": CountingClient(), **{f"acc{i}": CountingClient() for i in range(20)}}
    manager = make_manager(clients, symbols=[f"SYM{i}" for i in range(5)], concurrency=2)

    report = await manager.prepare_account("acc0", [f"SYM{i}" for i in range(5)])

    assert report.failed == {}
    assert inflight["max"] <= 2


async def test_worker_processes_pending_and_stops_cleanly():
    clients = {"main": FakeLevClient(), "copyc1": FakeLevClient()}
    manager = make_manager(clients, symbols=["BTCUSDT"], maintenance_interval_s=0.01)
    stop_event = asyncio.Event()
    worker = asyncio.create_task(manager.run(stop_event))
    try:
        for _ in range(400):
            if manager._candidate.get("BTCUSDT") is not None:  # noqa: SLF001
                break
            await asyncio.sleep(0.005)
        manager.request_prepare("copyc1", "BTCUSDT")
        for _ in range(200):
            if manager.is_ready("copyc1", "BTCUSDT"):
                break
            await asyncio.sleep(0.01)
        assert manager.is_ready("copyc1", "BTCUSDT")
    finally:
        stop_event.set()
        await asyncio.wait_for(worker, timeout=5)
    assert worker.done() and not worker.cancelled()
