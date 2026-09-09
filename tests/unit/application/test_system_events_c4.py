"""C4 system facts: entry-skip dedup, connectivity transitions, best-effort control ops."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from copytradeaster.application.models import RiskUpdateInput
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    SettingsRepo,
    TelegramUserRepo,
)

ACCOUNT = "tg-7"
SCOPE = HistoryScope(ACCOUNT, "testnet")


@pytest.fixture
async def service(tmp_path):
    db = Database(tmp_path / "system.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert(
        ACCOUNT, "follower", ACCOUNT, "0x1111111111111111111111111111111111111111"
    )
    await TelegramUserRepo(db).upsert(7, 700, account_id=ACCOUNT)
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=Decimal("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        ACCOUNT,
    )
    await SettingsRepo(db).set_flag("ready", "true")
    await SettingsRepo(db).set_flag("allow_new_entries", "true")
    runtime = SimpleNamespace(db=db, testnet=True)
    svc = UserApplicationService(runtime, admin_ids=[100])
    yield db, runtime, svc
    await db.close()


async def kinds(db, *wanted):
    rows = await EventRepository(db).list_owned(SCOPE, limit=200)
    return [row.event.kind for row in rows if not wanted or row.event.kind in wanted]


async def test_entry_skipped_twice_for_same_attempt_is_one_event(service):
    _, _, svc = service
    attempt = "trade-7"
    await svc._record_entry_skipped(
        ACCOUNT, symbol="BTCUSDT", reason="ENTRY_DEVIATION", attempt_id=attempt, pos_side="LONG"
    )
    await svc._record_entry_skipped(
        ACCOUNT, symbol="BTCUSDT", reason="ENTRY_DEVIATION", attempt_id=attempt, pos_side="LONG"
    )
    assert await kinds(service[0], "ENTRY_SKIPPED") == ["ENTRY_SKIPPED"]
    (stored,) = await EventRepository(service[0]).list_owned(SCOPE, kinds=["ENTRY_SKIPPED"])
    assert stored.event.trade_ref is None
    assert stored.event.facts["symbol"] == "BTCUSDT"
    assert stored.event.facts["reason"] == "ENTRY_DEVIATION"
    assert stored.event.facts["attempt_id"] == attempt
    assert stored.event.facts["pos_side"] == "LONG"


async def test_entry_skipped_new_attempt_is_a_new_event(service):
    db, _, svc = service
    await svc._record_entry_skipped(
        ACCOUNT, symbol="BTCUSDT", reason="STALE_MASTER", attempt_id="trade-7"
    )
    await svc._record_entry_skipped(
        ACCOUNT, symbol="BTCUSDT", reason="STALE_MASTER", attempt_id="trade-8"
    )
    assert await kinds(db, "ENTRY_SKIPPED") == ["ENTRY_SKIPPED", "ENTRY_SKIPPED"]


async def test_entry_skipped_rejects_unstable_identity(service):
    _, _, svc = service
    with pytest.raises(ValueError, match="attempt identity"):
        await svc._record_entry_skipped(
            ACCOUNT, symbol="BTCUSDT", reason="STALE_MASTER", attempt_id=""
        )
    with pytest.raises(ValueError, match="reason"):
        await svc._record_entry_skipped(ACCOUNT, symbol="BTCUSDT", reason="", attempt_id="trade-7")


async def test_entry_skipped_write_failure_never_fails_caller(service, monkeypatch):
    db, _, svc = service

    async def boom(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(svc._user_events, "record", boom)
    await svc._record_entry_skipped(
        ACCOUNT, symbol="BTCUSDT", reason="STALE_MASTER", attempt_id="trade-7"
    )
    assert await kinds(db, "ENTRY_SKIPPED") == []


async def test_connectivity_transition_emits_once_per_state(service):
    db, runtime, svc = service

    async def failing(account_id):
        raise AsterError(-1006, "timeout")

    runtime.refresh_snapshot = failing
    with pytest.raises(AsterError):
        await svc._snapshot(ACCOUNT, refresh=True)
    with pytest.raises(AsterError):
        await svc._snapshot(ACCOUNT, refresh=True)
    assert await kinds(db, "CONNECTIVITY_LOST") == ["CONNECTIVITY_LOST"]

    async def healthy(account_id):
        return ({"positions": {}}, datetime(2026, 9, 1, tzinfo=UTC))

    runtime.refresh_snapshot = healthy
    await svc._snapshot(ACCOUNT, refresh=True)
    await svc._snapshot(ACCOUNT, refresh=True)
    assert await kinds(db) == ["CONNECTIVITY_LOST", "CONNECTIVITY_RESTORED"]
    (lost, restored) = await EventRepository(db).list_owned(
        SCOPE, kinds=["CONNECTIVITY_LOST", "CONNECTIVITY_RESTORED"]
    )
    assert lost.event.facts == {"reachable": False}
    assert restored.event.facts == {"reachable": True}


async def test_connectivity_ignores_non_transport_errors(service):
    db, runtime, svc = service

    async def buggy(account_id):
        raise ValueError("local bug, not an outage")

    runtime.refresh_snapshot = buggy
    with pytest.raises(ValueError):
        await svc._snapshot(ACCOUNT, refresh=True)
    assert await kinds(db) == []

    async def forbidden(account_id):
        raise AsterError(-2015, "invalid API key", 401)

    runtime.refresh_snapshot = forbidden
    with pytest.raises(AsterError):
        await svc._snapshot(ACCOUNT, refresh=True)
    assert await kinds(db) == []


async def test_update_risk_success_survives_writer_failure(service, monkeypatch):
    db, _, svc = service
    assert (await SettingsRepo(db).get_policy(ACCOUNT)).risk_percent == Decimal("0.01")

    async def boom(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(svc._user_events, "record", boom)
    result = await svc.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02")))
    assert result.risk_percent == Decimal("0.02")
    assert (await SettingsRepo(db).get_policy(ACCOUNT)).risk_percent == Decimal("0.02")
    assert await kinds(db, "RISK_CHANGED") == []


async def test_update_risk_records_previous_percent_after_commit(service):
    db, _, svc = service
    result = await svc.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02")))
    assert result.risk_percent == Decimal("0.02")
    (stored,) = await EventRepository(db).list_owned(SCOPE, kinds=["RISK_CHANGED"])
    assert stored.event.facts["risk_percent"] == "0.02"
    assert stored.event.facts["previous_percent"] == "0.01"


async def test_record_connectivity_rejects_bad_input(service):
    _, _, svc = service
    with pytest.raises(ValueError, match="account"):
        await svc._record_connectivity("", reachable=True)
    with pytest.raises(ValueError, match="boolean"):
        await svc._record_connectivity(ACCOUNT, reachable="yes")
