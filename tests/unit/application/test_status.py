"""P2.5 System Status application regression tests.

Covers the authoritative read-only slice over ``UserApplicationService``:
fresh/stale/blocked verdicts, the stale snapshot lifecycle (no minted
timestamps, preserved positions), signed permission evidence handling, and
the exact precedence blocked → degraded → stale → ready.  Uses the real
SQLite repositories with a small controllable runtime fake — no new test
framework.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from copytradeaster.application.models import (
    AsterState,
    AuthorizationError,
    CopyState,
    PermissionsState,
    ServiceUnavailableError,
    StatusReason,
    StatusRemediation,
    StatusState,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    SettingsRepo,
    TelegramUserRepo,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
USER_ADDRESS = "0x1111111111111111111111111111111111111111"
SIGNER_ADDRESS = "0x2222222222222222222222222222222222222222"


def _valid_record() -> AgentValidation:
    return AgentValidation(
        signer_address=SIGNER_ADDRESS,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at=NOW.isoformat(),
    )


class FakeStatusCredentials:
    def identity(self, account_id: str):
        del account_id
        return SimpleNamespace(signer=SIGNER_ADDRESS)


class FakeStatusRuntime:
    """Controllable snapshot/permission evidence for status tests."""

    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.ready = True
        self.settings = SettingsRepo(db)
        self.credentials = FakeStatusCredentials()
        self.registered = {"tg-7"}
        self.cached: tuple | None = ({"positions": {}, "open_orders": []}, NOW)
        self.refreshed: tuple | None = ({"positions": {}, "open_orders": []}, NOW)
        self.refresh_error: Exception | None = None
        self.permission_record: AgentValidation | None = _valid_record()
        self.permission_error: Exception | None = None
        self.override_allowed = False
        self.permission_refresh_calls = 0
        self.snapshot_calls: list[str] = []

    async def cached_snapshot(self, account_id: str):
        self.snapshot_calls.append(f"cached:{account_id}")
        return self.cached

    async def refresh_snapshot(self, account_id: str):
        self.snapshot_calls.append(f"refresh:{account_id}")
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refreshed

    async def is_account_registered(self, account_id: str):
        return account_id in self.registered

    async def refresh_agent_permission(self, account_id: str):
        del account_id
        self.permission_refresh_calls += 1
        if self.permission_error is not None:
            raise self.permission_error
        assert self.permission_record is not None
        return self.permission_record

    async def permission_override_allowed(self, account_id: str):
        del account_id
        return self.override_allowed


@pytest.fixture
async def status_env(tmp_path: Path):
    db = Database(tmp_path / "status.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER_ADDRESS)
    await TelegramUserRepo(db).upsert(7, 700, account_id="tg-7")
    await TelegramUserRepo(db).upsert(8, 800)
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=Decimal("0.02"),
            enabled=True,
            allow_new_entries=False,
        ),
        "tg-7",
    )
    settings = SettingsRepo(db)
    await settings.set_flag("ready", "true")
    await settings.set_flag("allow_new_entries", "true")
    runtime = FakeStatusRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
    )
    yield db, runtime, service
    await db.close()


async def _seed_validation(db: Database, checked_at: datetime) -> None:
    await AgentValidationRepo(db).upsert(
        "tg-7",
        signer_address=SIGNER_ADDRESS,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at=checked_at.isoformat(),
    )


def _positions(count: int) -> dict:
    return {
        "positions": {f"key-{i}": {"qty": "1"} for i in range(count)},
        "open_orders": [],
    }


async def test_fresh_status_is_ready(status_env):
    _, runtime, service = status_env
    dto = await service.status(7, refresh=True)

    assert dto.state is StatusState.READY
    assert dto.system_state.value == "ready"
    assert dto.copy_state is CopyState.PAUSED
    assert dto.aster_state is AsterState.OK
    assert dto.permissions_state is PermissionsState.VALID
    assert dto.reason is StatusReason.NONE
    assert dto.remediation is None
    assert dto.positions_count == 0
    assert dto.account_masked and USER_ADDRESS not in dto.account_masked
    assert dto.signer_masked == "0x2222…2222"
    assert dto.snapshot_captured_at == NOW
    assert dto.last_check_at == NOW
    assert dto.age_seconds == 0
    assert runtime.permission_refresh_calls == 1


async def test_refresh_false_is_cheap_cached_read(status_env):
    db, runtime, service = status_env
    await _seed_validation(db, NOW - timedelta(seconds=10))

    dto = await service.status(7, refresh=False)

    assert dto.state is StatusState.READY
    assert dto.aster_state is AsterState.OK
    assert dto.permissions_state is PermissionsState.VALID
    assert runtime.permission_refresh_calls == 0
    assert not any(call.startswith("refresh:") for call in runtime.snapshot_calls)
    assert any(call.startswith("cached:") for call in runtime.snapshot_calls)


async def test_active_copy_matches_dashboard_and_stays_ready(status_env):
    db, runtime, service = status_env
    del runtime
    await SettingsRepo(db).patch_policy("tg-7", allow_new_entries=True)

    dto = await service.status(7, refresh=True)
    dashboard = await service.dashboard(7, refresh=True)

    assert dto.copy_state is CopyState.ACTIVE
    assert dto.copy_state is dashboard.copy_state
    assert dto.state is StatusState.READY


async def test_aster_failure_keeps_cached_snapshot_as_stale(status_env):
    _, runtime, service = status_env
    captured = NOW - timedelta(seconds=10)
    runtime.cached = (_positions(2), captured)
    runtime.refresh_error = RuntimeError("SECRET-BOOM-aster-is-down")

    dto = await service.status(7, refresh=True)

    assert dto.state is StatusState.STALE
    assert dto.aster_state is AsterState.STALE
    assert dto.reason is StatusReason.ASTER_UNAVAILABLE
    assert dto.remediation is StatusRemediation.REFRESH
    # The last useful snapshot survives with its ORIGINAL capture time.
    assert dto.positions_count == 2
    assert dto.snapshot_captured_at == captured
    # Conservative last check follows the oldest mandatory evidence.
    assert dto.last_check_at == captured
    assert dto.age_seconds == 10
    # Raw failure text never reaches the public contract.
    body = json.dumps(asdict(dto), default=str)
    assert "SECRET-BOOM" not in body


async def test_expired_snapshot_ttl_is_stale(status_env):
    db, runtime, service = status_env
    await _seed_validation(db, NOW)
    runtime.cached = (_positions(1), NOW - timedelta(seconds=120))

    dto = await service.status(7, refresh=False)

    assert dto.state is StatusState.STALE
    assert dto.aster_state is AsterState.STALE
    assert dto.reason is StatusReason.SNAPSHOT_STALE
    assert dto.remediation is StatusRemediation.REFRESH
    assert dto.positions_count == 1


async def test_missing_snapshot_is_never_ready(status_env):
    _, runtime, service = status_env
    runtime.cached = None
    runtime.refreshed = None

    dto = await service.status(7, refresh=True)

    assert dto.state is StatusState.STALE
    assert dto.aster_state is AsterState.UNKNOWN
    assert dto.reason is StatusReason.SNAPSHOT_MISSING
    assert dto.positions_count is None
    assert dto.snapshot_captured_at is None


async def test_transient_permission_failure_keeps_cached_snapshot_as_stale(status_env):
    db, runtime, service = status_env
    checked = NOW - timedelta(seconds=10)
    await _seed_validation(db, checked)
    runtime.permission_error = ServiceUnavailableError("signed read is down")

    dto = await service.status(7, refresh=True)

    assert dto.state is StatusState.STALE
    assert dto.permissions_state is PermissionsState.STALE
    assert dto.reason is StatusReason.PERMISSION_STALE
    assert dto.remediation is StatusRemediation.REFRESH
    # Last safe permission snapshot stays visible, but not as fresh proof.
    assert dto.permissions.can_read is True
    assert dto.permissions.can_withdraw is False


async def test_rejected_permission_refresh_is_blocked(status_env):
    _, runtime, service = status_env
    record = _valid_record()
    runtime.permission_record = AgentValidation(
        signer_address=record.signer_address,
        can_read=False,
        can_perp_trade=record.can_perp_trade,
        can_withdraw=record.can_withdraw,
        expired_ms=record.expired_ms,
        ip_whitelist=record.ip_whitelist,
        status=record.status,
        checked_at=record.checked_at,
    )

    dto = await service.status(7, refresh=True)

    assert dto.state is StatusState.BLOCKED
    assert dto.permissions_state is PermissionsState.BLOCKED
    assert dto.reason is StatusReason.PERMISSION_INVALID
    assert dto.remediation is StatusRemediation.OPEN_BOT


async def test_degraded_system_maps_to_degraded(status_env):
    db, _, service = status_env
    await SettingsRepo(db).set_flag("degraded_active", "true")

    dto = await service.status(7, refresh=True)
    dashboard = await service.dashboard(7, refresh=True)

    assert dto.copy_state is CopyState.DEGRADED
    assert dto.copy_state is dashboard.copy_state
    assert dto.state is StatusState.DEGRADED
    assert dto.reason is StatusReason.SYSTEM_DEGRADED
    assert dto.remediation is StatusRemediation.REFRESH


async def test_system_stale_and_unknown_are_not_ready(status_env):
    db, runtime, service = status_env
    await SettingsRepo(db).set_flag("ready", "true")
    await SettingsRepo(db).set_flag("heartbeat_at", str(int(NOW.timestamp() * 1000) - 600_000))

    stale = await service.status(7, refresh=True)
    assert stale.state is StatusState.STALE
    assert stale.reason is StatusReason.SYSTEM_STALE

    await SettingsRepo(db).set_flag("ready", "false")
    runtime.ready = False
    unknown = await service.status(7, refresh=True)
    assert unknown.state is StatusState.STALE
    assert unknown.reason is StatusReason.SYSTEM_NOT_READY


async def test_disconnected_and_unregistered_accounts_are_blocked(status_env):
    db, runtime, service = status_env
    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-7",))
    disabled = await service.status(7)
    assert disabled.state is StatusState.BLOCKED
    assert disabled.aster_state is AsterState.BLOCKED
    assert disabled.reason is StatusReason.ACCOUNT_DISCONNECTED
    assert disabled.remediation is StatusRemediation.OPEN_BOT

    await db.execute_write("UPDATE accounts SET enabled=1 WHERE id=?", ("tg-7",))
    runtime.registered.clear()
    missing = await service.status(7)
    assert missing.state is StatusState.BLOCKED
    assert missing.reason is StatusReason.ACCOUNT_DISCONNECTED

    orphan = await service.status(8)
    assert orphan.state is StatusState.BLOCKED
    assert orphan.reason is StatusReason.ACCOUNT_NOT_CONNECTED
    assert orphan.remediation is StatusRemediation.OPEN_BOT
    assert orphan.positions_count is None


async def test_copy_blocked_matches_dashboard(status_env):
    db, _, service = status_env
    await SettingsRepo(db).patch_policy("tg-7", enabled=False)

    dto = await service.status(7, refresh=True)
    dashboard = await service.dashboard(7, refresh=True)

    assert dto.copy_state is CopyState.BLOCKED
    assert dto.copy_state is dashboard.copy_state
    assert dto.state is StatusState.BLOCKED
    assert dto.reason is StatusReason.COPY_BLOCKED
    assert dto.remediation is StatusRemediation.OPEN_BOT


async def test_conservative_last_check_and_safe_facts(status_env):
    db, runtime, service = status_env
    runtime.cached = (_positions(0), NOW - timedelta(seconds=5))
    runtime.refreshed = (_positions(0), NOW - timedelta(seconds=5))
    await _seed_validation(db, NOW - timedelta(seconds=50))

    dto = await service.status(7, refresh=False)

    assert dto.last_check_at == NOW - timedelta(seconds=50)
    assert dto.age_seconds == 50
    # Masked identities only — never raw addresses, keys, or internals.
    body = json.dumps(asdict(dto), default=str)
    assert USER_ADDRESS not in body
    assert SIGNER_ADDRESS not in body
    for forbidden in ("private", "secret", "nonce", "traceback", "client_order"):
        assert forbidden not in body.lower()


async def test_auth_boundary_stays_fail_closed(status_env):
    _, _, service = status_env
    with pytest.raises(AuthorizationError):
        await service.status(999)
