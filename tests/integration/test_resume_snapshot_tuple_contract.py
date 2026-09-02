"""Integration regression: resume & dashboard paths must unpack the
``(snapshot, captured_at)`` tuple returned by the production
``AppStage2RuntimeAdapter``.

The soft unit-test fake returns loose mappings and never validates the
snapshot type, so a tuple passed straight into ``validate_resume_safety``
used to slip through unit tests while breaking the production adapter with
``ValueError: resume snapshot is invalid`` (masked by the service boundary as
``ConfirmationExpiredError``).  These tests wire the *real* adapter into
``UserApplicationService`` and assert the tuple contract end-to-end.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from copytradeaster.application.adapters import AppStage2RuntimeAdapter
from copytradeaster.application.models import (
    ConfirmationAction,
    CopyState,
    ServiceUnavailableError,
    StaleOperationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.config import Settings
from copytradeaster.domain.models import AccountState, PositionKey, PositionState, PosSide
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidationRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.unit.aster.conftest import KEY, SIGNER, USER


class _Registry:
    def __init__(self, runtimes: dict[str, object] | None = None) -> None:
        self.runtimes = runtimes or {}

    def get(self, account_id: str) -> object | None:
        return self.runtimes.get(account_id)


class _Coordinator:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.admission_open = True

    async def close_generation_admission(self) -> None:
        self.events.append("close")
        self.admission_open = False

    async def open_generation_admission(self) -> None:
        self.events.append("open")
        self.admission_open = True


class _FakeLeverageManager:
    async def prepare_account(self, account_id, symbols):
        from copytradeaster.trading.leverage import PrepareReport

        del account_id
        return PrepareReport(confirmed={s: 200 for s in symbols}, failed={})


class _App:
    """Production-shaped App seam: only attributes the adapter touches."""

    def __init__(self, db: Database, cfg: Settings) -> None:
        self.db = db
        self.cfg = cfg
        self.credentials = SimpleNamespace(
            identity=lambda account_id: SimpleNamespace(user=USER, signer=SIGNER)
        )
        self.follower_registry = _Registry({})
        self.coordinator = _Coordinator()
        self.settings = SettingsRepo(db)
        self.leverage = _FakeLeverageManager()
        self.ready = True
        self.reconcile_calls: list[str] = []
        # master_has_open_positions reads the projector state.
        self.projector = SimpleNamespace(state=SimpleNamespace(positions={}))

    async def snapshot_follower(self, account_id: str) -> AccountState:
        del account_id
        return getattr(self, "snapshot_value", AccountState("tg-7", Decimal(0), {}, []))

    async def reconcile_follower(self, account_id: str, *, reason: str) -> None:
        del reason
        self.reconcile_calls.append(account_id)

    def follower_lifecycle(self, account_id: str):
        del account_id

        @asynccontextmanager
        async def lifecycle():
            yield object()

        return lifecycle()


def _settings() -> Settings:
    from pydantic import SecretStr

    return Settings(
        encryption_key=SecretStr("11" * 32),
        aster_egress_ips=["127.0.0.1/32"],
        dry_run=True,
    )


async def _seed(db: Database) -> None:
    await AccountRepo(db).upsert(
        "tg-7",
        "follower",
        "tg-7",
        USER,
    )
    await TelegramUserRepo(db).upsert(7, 700, account_id="tg-7")
    await AgentValidationRepo(db).upsert(
        "tg-7",
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC).isoformat(),
    )
    from copytradeaster.domain.models import RiskPolicy

    await SettingsRepo(db).upsert_policy(
        RiskPolicy(
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=Decimal("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        "tg-7",
    )
    from copytradeaster.credentials.service import SqliteCredentials

    creds = SqliteCredentials(db, bytes.fromhex("11" * 32))
    await creds.import_identity(
        "tg-7",
        user=USER,
        signer_private_key_hex=KEY,
        role="follower",
        label="tg-7",
    )
    settings = SettingsRepo(db)
    await settings.set_flag("ready", "true")
    await settings.set_flag("allow_new_entries", "true")


class _FreshClock:
    """Clock pinned inside the permission TTL and snapshot TTL windows."""

    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, 12, 0, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
async def composition(tmp_path: Path):
    db = Database(tmp_path / "resume-tuple.sqlite3")
    await db.connect()
    await db.migrate()
    await _seed(db)
    app = _App(db, _settings())
    app.follower_registry.runtimes["tg-7"] = SimpleNamespace(
        account_id="tg-7", execution_mode="REAL"
    )
    clock = _FreshClock()
    adapter = AppStage2RuntimeAdapter(app, clock=lambda: clock.value)
    service = UserApplicationService(
        adapter,
        admin_ids=[100],
        clock=clock,
    )
    yield db, app, adapter, clock, service
    await db.close()


async def test_resume_passes_pure_account_state_to_real_adapter(composition):
    """P0 regression: _snapshot() tuple must be unpacked before
    validate_resume_safety; the real adapter rejects anything that is not an
    AccountState (and would raise ValueError('resume snapshot is invalid'))."""
    db, app, adapter, clock, service = composition

    # Seed the adapter cache so resume's refresh_snapshot returns the tuple.
    refreshed = await adapter.refresh_snapshot("tg-7")
    assert isinstance(refreshed, tuple) and len(refreshed) == 2
    assert isinstance(refreshed[0], AccountState)
    assert isinstance(refreshed[1], datetime)

    await service.pause(7)
    assert (await SettingsRepo(db).get_policy("tg-7")).allow_new_entries is False

    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    resumed = await service.confirm_action(7, confirmation)
    assert resumed.copy_state is CopyState.ACTIVE
    # pause_account -> close+open+reconcile, resume -> leverage+open,
    # confirm -> resume_account+reconcile_account.
    assert "open" in app.coordinator.events
    assert "tg-7" in app.reconcile_calls


async def test_dashboard_cache_fallback_passes_pure_snapshot(composition):
    """The dashboard fallback read must hand _dashboard_for a snapshot
    object, not the (snapshot, captured_at) tuple, so _position_count and
    row access keep working with the real adapter."""
    _, app, adapter, _, service = composition

    await adapter.refresh_snapshot("tg-7")
    dashboard = await service.dashboard(7)
    # The real adapter cached a flat snapshot; the fallback path must parse
    # it and not surface the tuple as a position count error.
    assert dashboard.open_positions == 0
    assert dashboard.system_state.value == "ready"


async def test_positions_use_real_adapter_snapshot_tuple(composition):
    """positions() consumes the same tuple-typed adapter contract and must
    surface a fresh, non-stale empty page when the adapter cache is warm."""
    _, app, adapter, clock, service = composition

    snapshot, captured_at = await adapter.refresh_snapshot("tg-7")
    del snapshot
    page = await service.positions(7, refresh=False)
    assert page.count == 0
    assert page.is_stale is False
    assert page.snapshot_captured_at == captured_at


async def test_resume_rejects_manual_position_via_real_adapter_validation(composition):
    """End-to-end negative control: the real validate_resume_safety must
    still receive a true AccountState.  With a manual position and no sync
    basis the adapter must block the resume (proving the snapshot reached the
    adapter intact rather than as a tuple)."""
    db, app, adapter, clock, service = composition

    key = PositionKey("BTCUSDT", PosSide.BOTH)
    position = PositionState(
        key=key,
        qty=Decimal("1"),
        entry_price=100,
        leverage=3,
        mark_price=100,
        liquidation_price=0,
    )
    app.snapshot_value = AccountState("tg-7", Decimal(0), {key: position}, [])
    await adapter.refresh_snapshot("tg-7")

    await service.pause(7)
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    # The adapter raises RuntimeError('manual position blocks resume'); the
    # service boundary must surface a safe typed error, not a masked
    # confirmation expiry (which used to hide the tuple bug).
    with pytest.raises((StaleOperationError, ServiceUnavailableError)):
        await service.confirm_action(7, confirmation)


async def test_positions_stale_after_ttl_expiry(composition):
    """A cached snapshot older than max(2 x reconcile_interval_s, 30s) must
    surface as stale with risk_to_stop_usdt=None even while the system
    heartbeat stays READY."""
    _, app, adapter, clock, service = composition

    key = PositionKey("BTCUSDT", PosSide.BOTH)
    position = PositionState(
        key=key,
        qty=Decimal("0.002"),
        entry_price=Decimal("60000"),
        leverage=200,
        mark_price=Decimal("61000"),
        liquidation_price=Decimal("50000"),
        unrealized_profit=Decimal("2"),
    )
    app.snapshot_value = AccountState("tg-7", Decimal(0), {key: position}, [])
    await adapter.refresh_snapshot("tg-7")

    # Fresh within TTL.
    fresh_page = await service.positions(7, refresh=False)
    assert fresh_page.is_stale is False
    assert fresh_page.snapshot_captured_at is not None
    # No stop attached: risk stays None regardless of freshness.
    assert fresh_page.positions[0].risk_to_stop_usdt is None

    # Advance the clock beyond max(2 x 15s, 30s) = 30s.
    from datetime import timedelta

    clock.value = clock.value + timedelta(seconds=45)
    expired_page = await service.positions(7, refresh=False)
    assert expired_page.is_stale is True
    assert expired_page.positions[0].risk_to_stop_usdt is None
