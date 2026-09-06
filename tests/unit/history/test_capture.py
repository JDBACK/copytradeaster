"""Boundary capture: proven snapshots bracket the import, then publish once."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from copytradeaster.aster.models import OrderDTO, PositionDTO
from copytradeaster.history.capture import HistoryCapture, read_boundary
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.history.reconcile import HistoryReconciliationError
from copytradeaster.history.snapshot import PositionBoundary
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo
from tests.unit.aster.test_trading_api import ORDER_RESPONSE
from tests.unit.history.test_projector import fill


def position(**updates):
    return PositionDTO.model_validate(
        {
            "symbol": "BTCUSDT",
            "positionAmt": "1",
            "entryPrice": "100",
            "markPrice": "110",
            "leverage": 10,
            "liquidationPrice": "0",
            "marginType": "cross",
            "positionSide": "BOTH",
            "unRealizedProfit": "10",
            "updateTime": 50,
            **updates,
        }
    )


def boundary(row=None, started=0, received=100):
    return PositionBoundary.from_positions(
        "BTCUSDT", [row or position()], request_started=started, response_received=received
    )


def after_boundary(row=None):
    return boundary(row, started=2200, received=2300)


def capture_for(db, history):
    return HistoryCapture(history, EpisodeRepository(db))


async def commit(history, scope, fills, window):
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(tuple(fills), (window,), (), (), 1), checked_at=3
    )


async def test_publish_zero_seed_open_episode_and_republish_is_stable(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
    )
    first = await capture.publish_reconciled(**kwargs)
    assert len(first) == 1
    episode = first[0].episode
    assert episode.quality == "complete" and episode.remaining_qty == D(1)
    assert episode.opened_at == 60 and episode.closed_at is None
    assert await capture.publish_reconciled(**kwargs) == first


async def test_changed_position_refuses_without_publishing(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    moved = after_boundary(position(positionAmt="2", updateTime=150))
    with pytest.raises(HistoryReconciliationError):
        await capture.publish_reconciled(
            scope, "BTCUSDT", window, before=boundary(), after=moved, cutoff=100
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_unbracketed_cutoff_refuses_without_publishing(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    early = boundary(started=0, received=100)
    late = replace(early, request_started=150, response_received=160)
    with pytest.raises(HistoryReconciliationError, match="bracketed"):
        await capture.publish_reconciled(
            scope, "BTCUSDT", window, before=early, after=late, cutoff=100
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_incomplete_coverage_refuses_without_publishing(storage):
    db, history, scope = storage
    narrow = Window(50, 70)
    await commit(history, scope, [fill(60, "BUY", "1")], narrow)
    capture = capture_for(db, history)
    with pytest.raises(HistoryReconciliationError, match="gap"):
        await capture.publish_reconciled(
            scope, "BTCUSDT", Window(0, 100), before=boundary(), after=after_boundary(), cutoff=100
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_nonzero_seed_keeps_stable_placeholder_ref(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "2")], window)
    before = boundary(position(positionAmt="5"))
    after = after_boundary(position(positionAmt="5"))
    capture = capture_for(db, history)
    kwargs = dict(
        scope=scope, symbol="BTCUSDT", window=window, before=before, after=after, cutoff=100
    )
    first = await capture.publish_reconciled(**kwargs)
    assert len(first) == 1
    episode = first[0].episode
    assert episode.quality == "partial_history" and episode.opened_at is None
    assert episode.remaining_qty == D(5)
    second = await capture.publish_reconciled(**kwargs)
    assert second == first
    assert second[0].episode.public_ref == episode.public_ref


async def test_ending_mismatch_refuses_without_erasing_prior(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    good = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
    )
    first = await capture.publish_reconciled(**good)
    wrong = after_boundary(position(positionAmt="9"))
    with pytest.raises(HistoryReconciliationError):
        await capture.publish_reconciled(
            scope, "BTCUSDT", window, before=boundary(), after=wrong, cutoff=100
        )
    repo = EpisodeRepository(db)
    assert await repo.get(scope, first[0].episode.public_ref) == first[0]


async def test_concurrent_captures_serialize_to_one_result(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
    )
    first, second = await asyncio.gather(
        capture.publish_reconciled(**kwargs), capture.publish_reconciled(**kwargs)
    )
    assert first == second


async def test_episode_is_owned_by_account_and_environment(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_for(db, history)
    (stored,) = await capture.publish_reconciled(
        scope, "BTCUSDT", window, before=boundary(), after=after_boundary(), cutoff=100
    )
    repo = EpisodeRepository(db)
    assert await repo.get(HistoryScope("b", "testnet"), stored.episode.public_ref) is None
    assert await repo.get(HistoryScope("a", "mainnet"), stored.episode.public_ref) is None


async def test_empty_symbol_rejected_before_exchange_read(storage):
    db, history, scope = storage
    capture = capture_for(db, history)
    with pytest.raises(ValueError):
        await read_boundary(AsyncMock(), scope, "")
    with pytest.raises(ValueError):
        await capture.publish_reconciled(
            scope, "", Window(0, 100), before=boundary(), after=after_boundary(), cutoff=100
        )


async def test_foreign_symbol_snapshot_cannot_certify_boundary(storage):
    _, _, scope = storage

    class Foreign:
        async def position_risk(self, account_id, symbol):
            assert account_id == "a"
            return [position(symbol="ETHUSDT")]

    with pytest.raises(HistoryReconciliationError):
        await read_boundary(Foreign(), scope, "BTCUSDT", clock_ms=lambda: 7)


class StaticSnapshots:
    def __init__(self, rows):
        self._rows = rows

    async def position_risk(self, account_id, symbol):
        assert account_id == "a"
        return list(self._rows)


def manual_clock(values):
    it = iter(values)
    return lambda: next(it)


async def test_capture_and_publish_wires_import_then_proven_publish(storage):
    db, history, scope = storage
    window = Window(0, 100)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)
    capture = capture_for(db, history)
    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([0, 100, 3000, 3100]),
    )
    assert published == window
    assert len(stored) == 1 and stored[0].episode.remaining_qty == D(1)
    assert len(await history.fills(scope, "BTCUSDT", window)) == 1


async def test_fast_import_waits_out_margin_then_publishes(storage):
    db, history, scope = storage
    window = Window(0, 100)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)
    capture = capture_for(db, history)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([0, 100, 150, 160, 2150, 2160]),
        sleep=fake_sleep,
    )
    assert published == window
    assert len(stored) == 1
    assert slept == [pytest.approx(1.95)]
    assert len(await history.fills(scope, "BTCUSDT", window)) == 1


async def test_short_coverage_defers_without_waiting(storage):
    db, history, scope = storage
    await commit(history, scope, [], Window(0, 50))
    window = Window(0, 100)
    reader = AsyncMock()
    capture = capture_for(db, history)
    reader.fills.return_value = WindowRead((), (Window(0, 50),), (Window(51, 100),), (), 1)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    with pytest.raises(HistoryReconciliationError):
        await capture.capture_and_publish(
            scope,
            "BTCUSDT",
            window,
            snapshots=StaticSnapshots([position()]),
            synchronizer=HistorySynchronizer(reader, history),
            clock_ms=manual_clock([0, 60, 70, 80]),
            sleep=fake_sleep,
        )
    assert slept == []
    assert len(await history.fills(scope, "BTCUSDT", window)) == 0
    assert await db.fetchall("SELECT * FROM follower_episodes") == []


async def test_stale_window_extends_to_live_edge_and_publishes(storage):
    db, history, scope = storage
    await commit(history, scope, [], Window(0, 1000))
    reader = AsyncMock()
    seen: list[Window] = []

    def _read(account_id, symbol, window, request_budget=8):
        seen.append(window)
        return WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)

    reader.fills.side_effect = _read
    capture = capture_for(db, history)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    stored, published = await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        Window(0, 1000),
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([1_000_000, 1_000_050, 1_000_100, 1_000_150, 1_002_100, 1_002_150]),
        sleep=fake_sleep,
    )
    assert seen == [Window(0, 1_000_050)]
    assert published == Window(0, 1_000_050)
    assert len(stored) == 1 and stored[0].episode.remaining_qty == D(1)
    assert slept == [pytest.approx(1.95)]


def capture_with_events(db, history):
    return HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))


async def test_publish_reconciled_emits_linked_open_event_once(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    capture = capture_with_events(db, history)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        window=window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
        recorded_at=5000,
        notify_eligible=True,
    )
    (stored,) = await capture.publish_reconciled(**kwargs)
    (row,) = await EventRepository(db).list_owned(scope)
    assert row.event.kind == "POSITION_OPENED"
    assert row.event.trade_ref == stored.episode.public_ref
    assert row.event.recorded_at == 5000 and row.event.notify_eligible is True
    await capture.publish_reconciled(**kwargs)
    assert len(await EventRepository(db).list_owned(scope)) == 1


async def test_capture_and_publish_stamps_record_time_on_events(storage):
    db, history, scope = storage
    window = Window(0, 100)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((fill(60, "BUY", "1"),), (window,), (), (), 1)
    capture = capture_with_events(db, history)
    await capture.capture_and_publish(
        scope,
        "BTCUSDT",
        window,
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        clock_ms=manual_clock([0, 100, 3000, 3100]),
        notify_eligible=True,
    )
    (row,) = await EventRepository(db).list_owned(scope)
    assert row.event.kind == "POSITION_OPENED" and row.event.recorded_at == 3100


async def commit_empty(history, scope, window):
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead((), (window,), (), (), 1), checked_at=3
    )


def scheduled_capture(db, history):
    return HistoryCapture(history, EpisodeRepository(db), events=EventRepository(db))


async def test_prepare_returns_none_without_complete_coverage(storage):
    db, history, scope = storage
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 100000) is None


async def test_prepare_defers_fresh_frontier_without_exchange_reads(storage):
    db, history, scope = storage
    await commit_empty(history, scope, Window(0, 1000))
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 2999) is None
    assert await capture.prepare_window(scope, "BTCUSDT", 3000) == Window(0, 1000)


async def test_prepare_skips_published_frontier_and_reuses_start(storage):
    db, history, scope = storage
    await commit_empty(history, scope, Window(0, 1000))
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=1000, now_ms=5000)
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 20000) is None
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=500, now_ms=6000)
    assert await capture.prepare_window(scope, "BTCUSDT", 20000) == Window(0, 1000)


async def test_prepare_clamps_span_deterministically(storage):
    db, history, scope = storage
    await commit_empty(history, scope, Window(0, 500_000_000))
    await commit_empty(history, scope, Window(500_000_000, 800_000_000))
    await history.record_capture(scope, "BTCUSDT", start_ms=0, end_ms=None, now_ms=1)
    capture = scheduled_capture(db, history)
    assert await capture.prepare_window(scope, "BTCUSDT", 900_000_000) == Window(
        195_200_000, 800_000_000
    )


async def test_settled_gate_publishes_close_of_unknown_open(storage):
    db, history, scope = storage
    flat = position(positionAmt="0")
    capture = scheduled_capture(db, history)
    window = Window(200, 300)
    await commit(history, scope, [fill(260, "SELL", "2", "100", "20")], window)
    (stored,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=boundary(flat, started=2400, received=2500),
        cutoff=300,
        recorded_at=5000,
        require_settled=True,
    )
    assert stored.episode.closed_at == 260 and stored.episode.quality == "partial_history"


async def test_settled_gate_refuses_mid_position_window(storage):
    db, history, scope = storage
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "2")], window)
    row = position(positionAmt="5")
    capture = scheduled_capture(db, history)
    with pytest.raises(HistoryReconciliationError, match="open position"):
        await capture.publish_reconciled(
            scope,
            "BTCUSDT",
            window,
            before=boundary(row),
            after=after_boundary(row),
            cutoff=100,
            recorded_at=5000,
            require_settled=True,
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []
    assert await EventRepository(db).list_owned(scope) == []


async def test_settled_gate_publishes_flat_start_and_flat_end(storage):
    db, history, scope = storage
    flat = position(positionAmt="0")
    capture = scheduled_capture(db, history)
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1"), fill(70, "SELL", "1", "100", "5")], window)
    (stored,) = await capture.publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(flat),
        after=after_boundary(flat),
        cutoff=100,
        recorded_at=5000,
        require_settled=True,
    )
    assert stored.episode.closed_at == 70
    opened, closed = await EventRepository(db).list_owned(scope)
    assert (opened.event.kind, closed.event.kind) == ("POSITION_OPENED", "POSITION_CLOSED")
    assert closed.event.trade_ref == stored.episode.public_ref


async def test_run_scheduled_publishes_and_records_checkpoint(storage):
    db, history, scope = storage
    window = Window(0, 1000)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((), (window,), (), (), 1)
    capture = scheduled_capture(db, history)
    published = await capture.run_scheduled(
        scope,
        "BTCUSDT",
        snapshots=StaticSnapshots([position()]),
        synchronizer=HistorySynchronizer(reader, history),
        now_ms=10000,
        clock_ms=manual_clock([0, 500, 4000, 4100]),
    )
    assert published is True
    state = await history.capture_state(scope, "BTCUSDT")
    assert (state["cap_start_ms"], state["cap_end_ms"], state["last_error"]) == (0, 1000, None)
    (row,) = await EventRepository(db).list_owned(scope)
    assert row.event.notify_eligible is True
    assert (
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=StaticSnapshots([position()]),
            synchronizer=HistorySynchronizer(reader, history),
            now_ms=20000,
            clock_ms=manual_clock([0, 500, 4000, 4100]),
        )
        is False
    )


async def test_run_scheduled_skips_without_coverage_leaving_no_trace(storage):
    db, history, scope = storage
    capture = scheduled_capture(db, history)
    assert (
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=StaticSnapshots([position()]),
            synchronizer=HistorySynchronizer(AsyncMock(), history),
            now_ms=10000,
        )
        is False
    )
    assert await history.capture_state(scope, "BTCUSDT") is None


async def test_run_scheduled_deferral_records_error_and_raises(storage):
    db, history, scope = storage
    window = Window(0, 1000)
    await commit(history, scope, [fill(60, "BUY", "2")], window)
    reader = AsyncMock()
    reader.fills.return_value = WindowRead((), (window,), (), (), 1)
    moved = position(positionAmt="5")
    capture = scheduled_capture(db, history)
    with pytest.raises(HistoryReconciliationError, match="open position"):
        await capture.run_scheduled(
            scope,
            "BTCUSDT",
            snapshots=StaticSnapshots([moved]),
            synchronizer=HistorySynchronizer(reader, history),
            now_ms=10000,
            clock_ms=manual_clock([0, 500, 4000, 4100]),
        )
    state = await history.capture_state(scope, "BTCUSDT")
    assert state["cap_start_ms"] == 0 and state["cap_end_ms"] is None
    assert state["last_error"] == "HistoryReconciliationError"


async def link_order(db, history, scope, order_id, coid, *, kind="MARKET_OPEN", payload=None):
    import json

    commands, links = CommandRepo(db), OrderLinkRepo(db)
    await db.execute_write(
        "INSERT INTO copy_trades(trade_id, symbol, pos_side, opened_at)"
        " VALUES (7,'BTCUSDT','BOTH','t')",
    )
    await commands.insert(
        f"cmd-{coid}",
        scope.account_id,
        None,
        kind,
        json.dumps(payload or {"symbol": "BTCUSDT"}),
        1,
        coid,
    )
    await commands.set_status(f"cmd-{coid}", "CONFIRMED")
    order = OrderDTO.model_validate({**ORDER_RESPONSE, "orderId": order_id, "clientOrderId": coid})
    await history.observe_order(scope, order, kind=kind, command_key=f"cmd-{coid}", observed_at=50)
    await links.add(7, scope.account_id, kind, coid)


def wired_capture(db, history):
    return HistoryCapture(
        history,
        EpisodeRepository(db),
        events=EventRepository(db),
        commands=CommandRepo(db),
    )


async def test_linked_fill_marks_episode_copy(storage):
    db, history, scope = storage
    await link_order(db, history, scope, 60, "ct-x")
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    (stored,) = await wired_capture(db, history).publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
        recorded_at=5000,
    )
    assert stored.episode.source == "copy"


async def test_partially_linked_window_stays_unknown(storage):
    db, history, scope = storage
    await link_order(db, history, scope, 60, "ct-x")
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1"), fill(61, "BUY", "1")], window)
    (stored,) = await wired_capture(db, history).publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(position(positionAmt="2")),
        after=after_boundary(position(positionAmt="2")),
        cutoff=100,
        recorded_at=5000,
    )
    assert stored.episode.source == "unknown"


async def test_confirmed_stop_observation_emits_protection_set(storage):
    db, history, scope = storage
    await link_order(
        db,
        history,
        scope,
        70,
        "ct-stop-1",
        kind="PLACE_STOP",
        payload={"symbol": "BTCUSDT", "stopPrice": "61900"},
    )
    await db.execute_write(
        "UPDATE follower_order_observations SET observed_at=60"
        " WHERE account_id=? AND client_order_id='ct-stop-1'",
        (scope.account_id,),
    )
    window = Window(0, 100)
    await commit(history, scope, [fill(60, "BUY", "1")], window)
    await wired_capture(db, history).publish_reconciled(
        scope,
        "BTCUSDT",
        window,
        before=boundary(),
        after=after_boundary(),
        cutoff=100,
        recorded_at=5000,
    )
    kinds = [row.event.kind for row in await EventRepository(db).list_owned(scope)]
    assert kinds == ["POSITION_OPENED", "PROTECTION_SET"]
    protection = (await EventRepository(db).list_owned(scope))[1].event
    assert protection.trade_ref is None and protection.occurred_at == 60
    assert protection.facts == {"symbol": "BTCUSDT", "slot": "stop", "new_price": "61900"}
