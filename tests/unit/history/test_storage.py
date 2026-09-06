from dataclasses import replace

import pytest

from copytradeaster.aster.models import IncomeDTO, OrderDTO, UserTradeDTO
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryConflictError, HistoryRepository, HistoryScope
from tests.unit.aster.test_history_api import FILL, FLOW
from tests.unit.aster.test_trading_api import ORDER_RESPONSE


@pytest.fixture
async def storage(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    await db.connect()
    await db.migrate()
    for name in ("a", "b"):
        await db.execute_write(
            "INSERT INTO accounts(id,role,label,user_address) VALUES (?, 'follower',?,?)",
            (name, name, name),
        )
    yield db, HistoryRepository(db), HistoryScope("a", "testnet")
    await db.close()


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
