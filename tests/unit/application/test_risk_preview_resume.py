"""S1.4 remainder: risk preview budgets and resume confirmation context.

The ruler screen switches server-computed budget rows without per-pixel
requests; the resume confirmation explains the proven outcome instead of
asserting a hardcoded master position. Both read cached-only facts: a
missing balance yields empty steps, an unavailable master read yields an
unknown context field, and neither fails the surrounding operation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.application.models import (
    AuthorizationError,
    Confirmation,
    ConfirmationAction,
    SnapshotFreshness,
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

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
USER_ID = 7
USER_ADDRESS = "0x1111111111111111111111111111111111111111"
FAR_FUTURE_MS = int((NOW + timedelta(days=30)).timestamp() * 1000)


class PreviewRuntime:
    """Cached snapshot plus controllable master exposure read."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.snapshot: dict | None = {"positions": {}, "available_usdt": "1240"}
        self.snapshot_at: datetime | None = NOW
        self.master_open: bool = True
        self.master_error: Exception | None = None

    async def cached_snapshot(self, account_id: str):
        del account_id
        if self.snapshot is None:
            return None
        return self.snapshot, self.snapshot_at

    async def account_version(self, account_id: str) -> str:
        del account_id
        return "v1"

    async def master_has_open_positions(self) -> bool:
        if self.master_error is not None:
            raise self.master_error
        return self.master_open


def _validation(**updates: object) -> AgentValidation:
    fields: dict[str, object] = {
        "signer_address": USER_ADDRESS,
        "can_read": True,
        "can_perp_trade": True,
        "can_withdraw": False,
        "expired_ms": FAR_FUTURE_MS,
        "ip_whitelist": "127.0.0.1/32",
        "status": "VALID",
        "checked_at": NOW.isoformat(),
    }
    fields.update(updates)
    return AgentValidation(**fields)  # type: ignore[arg-type]


@pytest.fixture
async def preview_env(tmp_path: Path):
    db = Database(tmp_path / "risk_preview.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("h1", "follower", "h1", USER_ADDRESS)
    await TelegramUserRepo(db).upsert(USER_ID, 700, account_id="h1")
    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=D("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        "h1",
    )
    runtime = PreviewRuntime(db)
    service = UserApplicationService(
        runtime,  # type: ignore[arg-type]
        admin_ids=[100],
        clock=lambda: NOW,
        testnet_check=True,
    )
    yield db, runtime, service
    await db.close()


async def test_preview_computes_every_allowed_step(preview_env):
    _, _, service = preview_env
    preview = await service.risk_preview(USER_ID)
    assert preview.available_usdt == D("1240")
    assert preview.freshness is SnapshotFreshness.FRESH
    assert preview.snapshot_captured_at == NOW
    assert len(preview.steps) == 10
    assert preview.steps[0].risk_percent == D("0.005")
    assert preview.steps[-1].risk_percent == D("0.05")
    two_percent = next(s for s in preview.steps if s.risk_percent == D("0.02"))
    assert two_percent.budget_usdt == D("24.80")
    assert all(isinstance(s.budget_usdt, type(D("1"))) for s in preview.steps)


async def test_preview_without_balance_yields_empty_steps(preview_env):
    _, runtime, service = preview_env
    runtime.snapshot = None
    preview = await service.risk_preview(USER_ID)
    assert preview.available_usdt is None and preview.steps == ()
    assert preview.freshness is SnapshotFreshness.UNAVAILABLE


async def test_preview_labels_stale_snapshot_but_still_computes(preview_env):
    _, runtime, service = preview_env
    runtime.snapshot_at = NOW - timedelta(hours=1)
    preview = await service.risk_preview(USER_ID)
    assert preview.freshness is SnapshotFreshness.STALE
    assert len(preview.steps) == 10


async def test_preview_rejects_foreign_user(preview_env):
    _, _, service = preview_env
    with pytest.raises(AuthorizationError):
        await service.risk_preview(999)


async def test_resume_confirmation_carries_proven_context(preview_env):
    db, _, service = preview_env
    await AgentValidationRepo(db).upsert("h1", validation=_validation())
    confirmation = await service.begin_confirmation(USER_ID, ConfirmationAction.RESUME)
    assert isinstance(confirmation, Confirmation)
    context = confirmation.resume_context
    assert context is not None
    assert context.master_in_position is True
    assert context.snapshot_freshness is SnapshotFreshness.FRESH
    assert context.permission_ok is True


async def test_resume_context_degrades_to_unknown_without_failing(preview_env):
    _, runtime, service = preview_env
    runtime.master_error = RuntimeError("master read failed")
    confirmation = await service.begin_confirmation(USER_ID, ConfirmationAction.RESUME)
    assert confirmation.resume_context is not None
    assert confirmation.resume_context.master_in_position is None
    assert confirmation.resume_context.permission_ok is None


async def test_resume_context_reports_denied_permission(preview_env):
    db, _, service = preview_env
    await AgentValidationRepo(db).upsert("h1", validation=_validation(can_withdraw=True))
    confirmation = await service.begin_confirmation(USER_ID, ConfirmationAction.RESUME)
    assert confirmation.resume_context is not None
    assert confirmation.resume_context.permission_ok is False


async def test_resume_context_reports_unproven_whitelist_as_not_ok(preview_env):
    db, _, service = preview_env
    await AgentValidationRepo(db).upsert("h1", validation=_validation(ip_whitelist=""))
    confirmation = await service.begin_confirmation(USER_ID, ConfirmationAction.RESUME)
    assert confirmation.resume_context is not None
    assert confirmation.resume_context.permission_ok is False


async def test_preview_serves_disabled_account(preview_env):
    db, _, service = preview_env
    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id='h1'")
    preview = await service.risk_preview(USER_ID)
    assert preview.available_usdt == D("1240")
    assert len(preview.steps) == 10


async def test_non_resume_confirmation_carries_no_context(preview_env):
    _, _, service = preview_env
    confirmation = await service.begin_confirmation(USER_ID, ConfirmationAction.DISCONNECT)
    assert confirmation.resume_context is None
