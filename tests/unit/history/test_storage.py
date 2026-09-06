from dataclasses import replace

import pytest

from copytradeaster.aster.models import IncomeDTO, OrderDTO, UserTradeDTO
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.history import HistoryConflictError
from tests.unit.aster.test_history_api import FILL, FLOW
from tests.unit.aster.test_trading_api import ORDER_RESPONSE


def read(rows):
    return WindowRead(tuple(rows), (Window(0, 100),), (), (), 1)


async def test_repeated_fills_exact_and_scoped(storage):
    db, repo, scope = storage
    fill = UserTradeDTO.model_validate(FILL)
    await repo.commit_fills(scope, "BTCUSDT", Window(0, 100), read([fill]), checked_at=101)
    equivalent = UserTradeDTO.model_validate({**FILL, "qty": "0.002000"})
    await repo.commit_fills(scope, "BTCUSDT", Window(0, 100), read([equivalent]), checked_at=102)
    assert await repo.fills(scope, "BTCUSDT", Window(0, 100)) == [fill]
    assert await repo.fills(replace(scope, account_id="b"), "BTCUSDT", Window(0, 100)) == []
    assert await repo.fills(replace(scope, environment="mainnet"), "BTCUSDT", Window(0, 100)) == []
    assert len(await db.fetchall("SELECT * FROM follower_fills")) == 1


async def test_conflict_rolls_back_facts_and_checkpoint(storage):
    db, repo, scope = storage
    first = UserTradeDTO.model_validate(FILL)
    await repo.commit_fills(scope, "BTCUSDT", Window(0, 100), read([first]), checked_at=101)
    second = UserTradeDTO.model_validate({**FILL, "id": 100})
    conflict = UserTradeDTO.model_validate({**FILL, "qty": "1"})
    with pytest.raises(HistoryConflictError):
        await repo.commit_fills(
            scope, "BTCUSDT", Window(0, 100), read([second, conflict]), checked_at=200
        )
    assert len(await db.fetchall("SELECT * FROM follower_fills")) == 1
    assert (await repo.windows(scope, "fills", "BTCUSDT"))[0]["checked_at"] == 101


async def test_pending_split_survives_reconnect(storage):
    db, repo, scope = storage
    result = WindowRead((), (Window(0, 49),), (Window(50, 100),), (), 2)
    await repo.commit_fills(scope, "BTCUSDT", Window(0, 100), result, checked_at=101)
    await db.close()
    await db.connect()
    states = await repo.windows(scope, "fills", "BTCUSDT")
    assert [row["state"] for row in states] == ["complete", "pending"]
    await repo.commit_fills(
        scope,
        "BTCUSDT",
        Window(50, 100),
        WindowRead((), (Window(50, 100),), (), (), 1),
        checked_at=102,
    )
    assert all(row["state"] == "complete" for row in await repo.windows(scope, "fills", "BTCUSDT"))


async def test_income_identity_includes_type(storage):
    db, repo, scope = storage
    flows = [
        IncomeDTO.model_validate(FLOW),
        IncomeDTO.model_validate({**FLOW, "incomeType": "COMMISSION"}),
    ]
    for _ in range(2):
        await repo.commit_income(scope, Window(0, 100), read(flows), checked_at=101)
    assert len(await db.fetchall("SELECT * FROM follower_cash_flows")) == 2


async def test_incomplete_partition_rejected_before_write(storage):
    db, repo, scope = storage
    with pytest.raises(ValueError, match="partition"):
        await repo.commit_fills(
            scope,
            "BTCUSDT",
            Window(0, 100),
            WindowRead((), (Window(0, 49),), (), (), 1),
            checked_at=101,
        )
    assert await db.fetchall("SELECT * FROM history_sync_state") == []


async def test_order_observations_update_in_place_ignore_older(storage):
    db, repo, scope = storage
    order = OrderDTO.model_validate(ORDER_RESPONSE)
    for time in (100, 101, 99):
        await repo.observe_order(
            scope, order, kind="MARKET_OPEN", command_key="cmd", observed_at=time
        )
    rows = await db.fetchall("SELECT * FROM follower_order_observations")
    assert len(rows) == 1
    assert rows[0]["observed_at"] == 101
    assert await db.fetchall("SELECT * FROM follower_fills") == []


async def test_synchronizer_resumes_pending_without_reimporting_completed_half(storage):
    from unittest.mock import AsyncMock

    from copytradeaster.history.sync import HistorySynchronizer

    db, repo, scope = storage
    await repo.commit_fills(
        scope,
        "BTCUSDT",
        Window(0, 100),
        WindowRead((), (Window(0, 49),), (Window(50, 100),), (), 2),
        checked_at=101,
    )
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((), (Window(50, 100),), (), (), 1)
    sync = HistorySynchronizer(reader, repo, clock_ms=lambda: 200)
    await sync.sync_fills(scope, "BTCUSDT", Window(0, 100))
    reader.fills.assert_awaited_once_with("a", "BTCUSDT", Window(50, 100), request_budget=8)
    assert all(row["state"] == "complete" for row in await repo.windows(scope, "fills", "BTCUSDT"))


async def test_synchronizer_error_preserves_last_good_data(storage):
    from unittest.mock import AsyncMock

    from copytradeaster.history.sync import HistorySynchronizer

    db, repo, scope = storage
    reader = AsyncMock()
    reader.income.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await HistorySynchronizer(reader, repo).sync_income(scope, Window(0, 100))
    assert await repo.windows(scope, "income", "") == []


async def test_overlap_coverage_compacts_without_filling_gap(storage):
    db, repo, scope = storage
    for start in (0, 5, 10, 30):
        window = Window(start, start + 10)
        await repo.commit_fills(
            scope, "BTCUSDT", window, WindowRead((), (window,), (), (), 1), checked_at=100
        )
    rows = await repo.windows(scope, "fills", "BTCUSDT")
    assert [(r["start_ms"], r["end_ms"]) for r in rows] == [(0, 20), (30, 40)]


async def test_repair_missing_observation_then_skip_terminal_order(storage):
    import json
    from unittest.mock import AsyncMock

    from copytradeaster.history.sync import HistorySynchronizer
    from copytradeaster.storage.repositories import CommandRepo

    db, repo, scope = storage
    commands = CommandRepo(db)
    await commands.insert(
        "repair", "a", None, "MARKET_OPEN", json.dumps({"symbol": "BTCUSDT"}), 1, "ct-x"
    )
    await commands.set_status("repair", "CONFIRMED")
    reader = AsyncMock()
    reader.order.return_value = OrderDTO.model_validate(ORDER_RESPONSE)
    sync = HistorySynchronizer(reader, repo, clock_ms=lambda: 1000)
    await sync.repair_order(scope)
    await sync.repair_order(scope)
    reader.order.assert_awaited_once_with("a", "BTCUSDT", "ct-x")
    assert len(await db.fetchall("SELECT * FROM follower_order_observations")) == 1
    assert (await commands.get("repair"))["status"] == "CONFIRMED"


async def test_order_not_found_preserves_command_and_backs_off(storage):
    import json
    from unittest.mock import AsyncMock

    from copytradeaster.history.sync import HistorySynchronizer
    from copytradeaster.storage.repositories import CommandRepo

    db, repo, scope = storage
    commands = CommandRepo(db)
    await commands.insert(
        "missing", "a", None, "MARKET_OPEN", json.dumps({"symbol": "BTCUSDT"}), 1, "ct-x"
    )
    await commands.set_status("missing", "CONFIRMED")
    reader = AsyncMock()
    reader.order.return_value = None
    sync = HistorySynchronizer(reader, repo, clock_ms=lambda: 1000)
    await sync.repair_order(scope)
    await sync.repair_order(scope)
    assert reader.order.await_count == 1
    assert (await commands.get("missing"))["status"] == "CONFIRMED"
    assert await repo.repair_candidate(scope, 301001) is not None


async def test_capture_checkpoint_missing_upsert_and_validation(storage):
    db, repo, scope = storage
    assert await repo.capture_state(scope, "BTCUSDT") is None
    await repo.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=100, now_ms=200)
    state = await repo.capture_state(scope, "BTCUSDT")
    assert (state["cap_start_ms"], state["cap_end_ms"], state["last_error"]) == (0, 100, None)
    await repo.record_capture(
        scope, "BTCUSDT", start_ms=0, end_ms=100, now_ms=300, error="deferred"
    )
    state = await repo.capture_state(scope, "BTCUSDT")
    assert (state["cap_end_ms"], state["last_error"], state["updated_at"]) == (100, "deferred", 300)
    assert await repo.capture_state(replace(scope, account_id="b"), "BTCUSDT") is None
    with pytest.raises(ValueError):
        await repo.record_capture(scope, "BTCUSDT", start_ms=200, end_ms=100, now_ms=300)
    with pytest.raises(ValueError):
        await repo.record_capture(scope, "BTCUSDT", start_ms=-1, end_ms=100, now_ms=300)
    with pytest.raises(ValueError):
        await repo.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=100, now_ms=-5)


async def _proven_setup(db, repo, scope, *, kind="MARKET_OPEN", status="CONFIRMED", trade_id=7):
    import json

    from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo

    commands, links = CommandRepo(db), OrderLinkRepo(db)
    await db.execute_write(
        "INSERT INTO copy_trades(trade_id, symbol, pos_side, opened_at)"
        " VALUES (7,'BTCUSDT','BOTH','t')",
    )
    payload = json.dumps({"symbol": "BTCUSDT"})
    await commands.insert("cmd-1", scope.account_id, None, kind, payload, 1, "ct-1")
    await commands.set_status("cmd-1", status)
    order = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 11, "clientOrderId": "ct-1"})
    await repo.observe_order(scope, order, kind=kind, command_key="cmd-1", observed_at=50)
    await links.add(trade_id, scope.account_id, kind, "ct-1")
    return commands


async def test_proven_order_sources_require_command_link_and_trade(storage):
    db, repo, scope = storage
    await _proven_setup(db, repo, scope)
    assert await repo.proven_order_sources(scope, "BTCUSDT") == {11: "copy"}


async def test_proven_order_sources_exclude_unproven_rows(storage):
    import json

    from copytradeaster.storage.repositories import OrderLinkRepo

    db, repo, scope = storage
    commands = await _proven_setup(db, repo, scope)
    payload = json.dumps({"symbol": "BTCUSDT"})
    await commands.insert("cmd-2", scope.account_id, None, "MARKET_OPEN", payload, 1, "ct-2")
    await commands.set_status("cmd-2", "FAILED")
    order = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 12, "clientOrderId": "ct-2"})
    await repo.observe_order(scope, order, kind="MARKET_OPEN", command_key="cmd-2", observed_at=51)
    await OrderLinkRepo(db).add(None, scope.account_id, "MARKET_OPEN", "ct-2")
    await commands.insert("cmd-3", scope.account_id, None, "PLACE_STOP", payload, 1, "ct-3")
    await commands.set_status("cmd-3", "CONFIRMED")
    stop = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 13, "clientOrderId": "ct-3"})
    await repo.observe_order(scope, stop, kind="PLACE_STOP", command_key="cmd-3", observed_at=52)
    await OrderLinkRepo(db).add(7, scope.account_id, "PLACE_STOP", "ct-3")
    assert await repo.proven_order_sources(scope, "BTCUSDT") == {11: "copy"}
    assert await repo.proven_order_sources(scope, "ETHUSDT") == {}
    with pytest.raises(ValueError):
        await repo.proven_order_sources(scope, "")


async def test_protection_observation_window_and_baseline(storage):
    db, repo, scope = storage
    first = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 21, "clientOrderId": "ct-s1"})
    second = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 22, "clientOrderId": "ct-s2"})
    taken = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 23, "clientOrderId": "ct-t1"})
    await repo.observe_order(scope, first, kind="PLACE_STOP", command_key="k1", observed_at=10)
    await repo.observe_order(scope, second, kind="PLACE_STOP", command_key="k2", observed_at=60)
    await repo.observe_order(scope, taken, kind="PLACE_TP", command_key="k3", observed_at=70)
    rows = await repo.protection_observations(scope, "BTCUSDT", 50, 80)
    assert [(row["client_order_id"], row["kind"]) for row in rows] == [
        ("ct-s2", "PLACE_STOP"),
        ("ct-t1", "PLACE_TP"),
    ]
    baseline = await repo.latest_protection_places(scope, "BTCUSDT", 50)
    assert {(row["kind"], row["client_order_id"]) for row in baseline} == {("PLACE_STOP", "ct-s1")}
    assert await repo.protection_canceled_before(scope, "BTCUSDT", "ct-s1", 50) is False
    with pytest.raises(ValueError):
        await repo.protection_observations(scope, "BTCUSDT", 80, 50)


async def test_unconfirmed_cancel_does_not_erase_baseline(storage):
    import json

    from copytradeaster.storage.repositories import CommandRepo

    db, repo, scope = storage
    commands = CommandRepo(db)
    payload = json.dumps({"symbol": "BTCUSDT", "origClientOrderId": "ct-s1"})
    await commands.insert("cmd-x", scope.account_id, None, "CANCEL_ORDER", payload, 1, "ct-x")
    unsigned = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": 29, "clientOrderId": "ct-x"})
    await repo.observe_order(
        scope, unsigned, kind="CANCEL_ORDER", command_key="cmd-x", observed_at=40
    )
    assert await repo.protection_canceled_before(scope, "BTCUSDT", "ct-s1", 50) is False
    await commands.set_status("cmd-x", "CONFIRMED")
    assert await repo.protection_canceled_before(scope, "BTCUSDT", "ct-s1", 50) is True


async def test_command_lookup_by_client_order_id(storage):
    import json

    from copytradeaster.storage.repositories import CommandRepo

    db, _, scope = storage
    commands = CommandRepo(db)
    assert await commands.by_client_order_id(scope.account_id, "ct-missing") is None
    await commands.insert(
        "cmd-a", scope.account_id, None, "PLACE_STOP", json.dumps({"stopPrice": "100"}), 1, "ct-a"
    )
    row = await commands.by_client_order_id(scope.account_id, "ct-a")
    assert row is not None and row["command_key"] == "cmd-a"
    assert row["status"] == "PENDING"
    with pytest.raises(ValueError):
        await commands.by_client_order_id("", "ct-a")
