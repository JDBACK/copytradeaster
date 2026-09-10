from __future__ import annotations

import inspect
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import copytradeaster.application.service as service_module
from copytradeaster.application.models import (
    AccessRequestStatus,
    ApplicationProtocol,
    AsterState,
    AuthorizationError,
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    CopyState,
    DirectCredentialsInput,
    GlobalEmergencyActiveError,
    GlobalPauseActiveError,
    OnboardingApplicationError,
    PermissionsState,
    PositionProtectionState,
    PositionSide,
    RiskUpdateInput,
    ServiceUnavailableError,
    StaleOperationError,
    StatusReason,
    StatusRemediation,
    StatusState,
    SystemState,
    UserRole,
    ValidationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import DirectOnboardingResult
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    NotificationOutboxRepo,
    SettingsRepo,
    TelegramUserRepo,
)


class FakeRegistry:
    def __init__(self, account_ids: set[str]):
        self.account_ids = set(account_ids)

    def get(self, account_id: str):
        return object() if account_id in self.account_ids else None


class FakeCredentials:
    def identity(self, account_id: str):
        return SimpleNamespace(signer="0x2222222222222222222222222222222222222222")


class FakeOnboarding:
    def __init__(self):
        self.connected = []
        self.replaced = []
        self.events = []
        self.fail_replace = False

    async def onboard(self, request):
        self.connected.append(request)
        return DirectOnboardingResult(
            account_id=f"tg-{request.telegram_user_id}",
            masked_user_address="0x1111…1111",
            masked_signer_address="0x2222…2222",
            position_mode="ONE_WAY",
            balance_assets=(),
            position_count=0,
            available_usdt=Decimal(0),
            paused=True,
        )

    async def preflight(self, request):
        self.events.append("preflight")
        return SimpleNamespace(request=request)

    async def replace(self, request, *, preflight):
        self.events.append("replace")
        self.replaced.append(request)
        if self.fail_replace:
            raise RuntimeError("replace failed")
        return DirectOnboardingResult(
            account_id=f"tg-{request.telegram_user_id}",
            masked_user_address="0x1111…1111",
            masked_signer_address="0x2222…2222",
            position_mode="ONE_WAY",
            balance_assets=(),
            position_count=0,
            available_usdt=Decimal(0),
            paused=True,
        )


class FakeLifecycle:
    def __init__(self, runtime):
        self.runtime = runtime
        self.safe = True
        self.published = []
        self.unpublished = False

    async def assert_safe_to_remove(self):
        self.runtime.events.append("safe")
        if not self.safe:
            raise RuntimeError("account is not safe")

    async def publish_replacement(self, replacement):
        self.runtime.events.append("publish")
        self.published.append(replacement)

    async def unpublish_after_commit(self):
        self.runtime.events.append("unpublish")
        self.unpublished = True


class FakeEmergencyController:
    def __init__(self):
        self.account_calls = []
        self.global_calls = []
        self.success = True

    async def emergency_account(self, account_id, *, operation_id=None):
        self.account_calls.append((account_id, operation_id))
        return SimpleNamespace(success=self.success)

    async def emergency_global(self, account_ids, *, operation_id=None):
        self.global_calls.append((tuple(account_ids), operation_id))
        return SimpleNamespace(success=self.success)


class FakeRuntime:
    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.ready = True
        self.settings = SettingsRepo(db)
        self.follower_registry = FakeRegistry({"tg-7"})
        self.credentials = FakeCredentials()
        self.actions: list[tuple] = []
        self.master_has_open_positions = False
        self.disconnect_called = False
        self.emergency_controller = FakeEmergencyController()
        self.lifecycle = FakeLifecycle(self)
        self.fail_resume = False
        self.fail_resume_safety = None
        self.permission_refresh_calls = 0
        self.permission_refresh_error = None
        self.events: list[str] = []
        self.admission_open = True
        self.control_report_success = True
        self.permission_override = False
        self.account_version_value = "credentials-v1"
        self.recover_runtime = False

    async def snapshot_follower(self, account_id: str):
        self.actions.append(("snapshot", account_id))
        return {"positions": {}, "open_orders": []}

    async def cached_snapshot(self, account_id: str):
        self.actions.append(("cached_snapshot", account_id))
        return {"positions": {}, "open_orders": []}

    async def refresh_snapshot(self, account_id: str):
        self.actions.append(("refresh_snapshot", account_id))
        return {"positions": {}, "open_orders": []}

    async def is_account_registered(self, account_id: str):
        return self.follower_registry.get(account_id) is not None

    async def account_version(self, account_id: str):
        del account_id
        return self.account_version_value

    async def recover_account_runtime(
        self, account_id: str, user_address: str, signer_address: str
    ):
        self.actions.append(("recover_runtime", account_id, user_address, signer_address))
        if not self.recover_runtime:
            return False
        self.follower_registry.account_ids.add(account_id)
        return True

    async def validate_resume_safety(self, account_id: str, snapshot):
        self.actions.append(("resume_safety", account_id))
        if self.fail_resume_safety is not None:
            raise self.fail_resume_safety

    async def refresh_agent_permission(self, account_id: str):
        self.permission_refresh_calls += 1
        if self.permission_refresh_error is not None:
            raise self.permission_refresh_error
        return AgentValidation(
            signer_address="0x2222222222222222222222222222222222222222",
            can_read=True,
            can_perp_trade=True,
            can_withdraw=False,
            expired_ms=2_000_000_000_000,
            ip_whitelist="127.0.0.1/32",
            status="VALID",
            checked_at="2026-08-23T12:00:00+00:00",
        )

    async def permission_override_allowed(self, account_id: str):
        return self.permission_override

    async def reconcile_follower(self, account_id: str):
        self.actions.append(("reconcile", account_id))

    async def pause_account(self, account_id: str):
        self.actions.append(("pause", account_id))

    async def resume_account(self, account_id: str):
        self.actions.append(("resume", account_id))
        if self.fail_resume:
            raise RuntimeError("resume failed")

    async def pause_entries(self, timeout_s: float = 5.0):
        self.actions.append(("pause_entries",))
        self.admission_open = True

    async def resume_entries(self):
        self.actions.append(("resume_entries",))
        self.admission_open = True

    async def reopen_entries_while_paused(self):
        self.actions.append(("reopen_entries_while_paused",))
        self.admission_open = True

    async def reconcile_account(self, account_id: str):
        self.actions.append(("reconcile_account", account_id))

    async def reconcile_all(self):
        self.actions.append(("reconcile_all",))

    async def emergency_account(self, account_id: str, operation_id: str):
        self.actions.append(("emergency", account_id, operation_id))

    async def emergency_all(self, operation_id: str):
        self.actions.append(("global_emergency", operation_id))

    async def disconnect_account(self, account_id: str):
        self.disconnect_called = True
        self.actions.append(("disconnect", account_id))
        await self.db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", (account_id,))

    def follower_lifecycle(self, account_id: str):
        return self._lifecycle_context()

    @asynccontextmanager
    async def _lifecycle_context(self):
        yield self.lifecycle

    async def commit_disconnect(self, account_id: str):
        self.disconnect_called = True
        self.actions.append(("disconnect", account_id))
        await self.db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", (account_id,))
        await self.db.execute_write(
            "UPDATE copy_settings SET enabled=0, allow_new_entries=0 WHERE account_id=?",
            (account_id,),
        )

    async def build_replacement_runtime(self, account_id, request, preflight):
        return object()


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.value


@pytest.fixture
async def environment(tmp_path: Path):
    db = Database(tmp_path / "application.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert(
        "tg-7",
        "follower",
        "tg-7",
        "0x1111111111111111111111111111111111111111",
    )
    await TelegramUserRepo(db).upsert(7, 700, account_id="tg-7")
    await TelegramUserRepo(db).upsert(99, 990, role="ADMIN")
    await TelegramUserRepo(db).upsert(100, 1000, role="ADMIN")
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
    now = datetime(2026, 8, 23, 11, 59, tzinfo=UTC).isoformat()
    await AgentValidationRepo(db).upsert(
        "tg-7",
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at=now,
    )
    settings = SettingsRepo(db)
    await settings.set_flag("ready", "true")
    await settings.set_flag("allow_new_entries", "true")
    runtime = FakeRuntime(db)
    onboarding = FakeOnboarding()
    clock = Clock()
    service = UserApplicationService(
        runtime,
        admin_ids=[100],
        onboarding=onboarding,
        emergency_controller=runtime.emergency_controller,
        clock=clock,
    )
    yield db, runtime, onboarding, clock, service
    await db.close()


async def test_authorization_is_private_numeric_and_env_admin_authoritative(environment):
    _, _, _, _, service = environment

    allowed = await service.authorize(7, 700, "private")
    assert allowed.allowed and allowed.role is UserRole.USER
    assert not (await service.authorize(7, 701, "private")).allowed
    assert not (await service.authorize(7, 700, "group")).allowed
    assert not (await service.authorize(True, 700, "private")).allowed
    assert not (await service.authorize(99, 990, "private")).allowed

    admin = await service.authorize(100, 1000, "private")
    assert admin.allowed and admin.role is UserRole.ADMIN
    assert not (await service.authorize(100, 1001, "private")).allowed
    assert not (await service.authorize(100, 1000, "group")).allowed

    with pytest.raises(AuthorizationError):
        await service.dashboard(99)


async def test_access_request_is_durable_and_env_admin_approval_opens_menu_access(environment):
    _, _, _, _, service = environment

    assert await service.access_request(8) is None
    pending = await service.submit_access_request(
        8,
        800,
        username="bad-name",
        display_name="  Second\n Account  ",
    )
    duplicate = await service.submit_access_request(
        8,
        800,
        username="ignored",
        display_name="Ignored",
    )
    dashboard = await service.admin_dashboard(100)
    page = await service.admin_access_requests(100)

    assert pending.status is AccessRequestStatus.PENDING
    assert pending.created
    assert duplicate.request_id == pending.request_id
    assert not duplicate.created
    assert pending.username is None
    assert pending.display_name == "Second Account"
    assert dashboard.pending_access_requests == 1
    assert page.items == (pending,)
    with pytest.raises(AuthorizationError):
        await service.admin_decide_access(99, pending.request_id, approve=True)

    approved = await service.admin_decide_access(100, pending.request_id, approve=True)
    repeated = await service.admin_decide_access(100, pending.request_id, approve=False)
    context = await service.authorize(8, 800, "private")

    assert approved.status is AccessRequestStatus.APPROVED
    assert approved.decision_changed
    assert repeated.status is AccessRequestStatus.APPROVED
    assert not repeated.decision_changed
    assert context.allowed and context.role is UserRole.USER
    assert (await service.admin_dashboard(100)).pending_access_requests == 0


async def test_dashboard_status_risk_activity_and_notifications_are_safe(environment):
    db, runtime, _, _, service = environment
    await EventRepository(db).record(
        HistoryScope("tg-7", "testnet"),
        kind="POSITION_OPENED",
        occurred_at=1_700_000_000_000,
        recorded_at=1_700_000_000_000,
        facts={
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry_qty": "0.016",
            "entry_price": "62400",
        },
        dedup_key="evt:dashboard:opened",
        trade_ref="ref-1",
        notify_eligible=False,
    )
    await NotificationOutboxRepo(db).enqueue(
        "event-1",
        7,
        700,
        "status",
        "INFO",
        "safe text",
        account_id="tg-7",
    )

    dashboard = await service.dashboard(7)
    assert dashboard.copy_state is CopyState.PAUSED
    assert dashboard.account_masked != "0x1111111111111111111111111111111111111111"
    assert dashboard.signer_masked == "0x2222…2222"
    assert dashboard.risk_percent == Decimal("0.01")

    status = await service.status(7, refresh=True)
    assert status.positions_count == 0
    assert status.state is StatusState.READY
    assert status.aster_state is AsterState.OK
    assert status.permissions_state is PermissionsState.VALID
    assert status.reason is StatusReason.NONE
    assert status.remediation is None
    assert any(action[0] == "refresh_snapshot" for action in runtime.actions)

    risk = await service.risk(7)
    assert risk.risk_percent == Decimal("0.01")
    assert risk.min == Decimal("0.005")
    assert risk.max == Decimal("0.05")
    assert risk.step == Decimal("0.005")
    assert risk.presets == (
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.03"),
        Decimal("0.05"),
    )
    assert risk.risk_base == "available_balance"
    assert not hasattr(risk, "max_leverage")
    assert not hasattr(risk, "max_entry_deviation_pct")
    activity = await service.activity(7)
    assert activity.items[0].title.splitlines()[0] == "Открыта BTCUSDT · LONG"
    assert activity.items[0].category.value == "trade"
    assert activity.items[0].status.value == "completed"
    assert activity.items[0].remediation is None
    assert activity.filter.value == "all"
    assert "never-display" not in activity.items[0].title

    notifications = await service.notification_settings(7)
    assert notifications.pending == 1
    assert notifications.last_delivery_at is None


async def test_positions_service_mapping_and_safety_checks(environment):
    db, runtime, _, _, service = environment
    from copytradeaster.domain.models import (
        PositionKey,
        PositionState,
        PosSide,
        ProtectiveOrder,
        Side,
    )

    # Mock snapshot with a variety of positions:
    # 1. ONE_WAY BTC LONG with valid stop and TP
    btc_key = PositionKey(symbol="BTCUSDT", pos_side=PosSide.BOTH)
    btc_pos = PositionState(
        key=btc_key,
        qty=Decimal("0.002"),
        entry_price=Decimal("60000.0"),
        mark_price=Decimal("61000.0"),
        liquidation_price=Decimal("50000.0"),
        leverage=200,
        stop=ProtectiveOrder(
            client_order_id="btc-stop-coid",
            order_id=12345,
            type="STOP_MARKET",
            side=Side.SELL,
            stop_price=Decimal("59000.0"),
            reduce_only=True,
            close_position=True,
            working_type="CONTRACT_PRICE",
            orig_qty=Decimal("0"),
        ),
        take_profit=ProtectiveOrder(
            client_order_id="btc-tp-coid",
            order_id=12346,
            type="TAKE_PROFIT_MARKET",
            side=Side.SELL,
            stop_price=Decimal("65000.0"),
            reduce_only=True,
            close_position=True,
            working_type="CONTRACT_PRICE",
            orig_qty=Decimal("0"),
        ),
        unrealized_profit=Decimal("2.0"),
    )

    # 2. HEDGE ETH SHORT with missing stop
    eth_key = PositionKey(symbol="ETHUSDT", pos_side=PosSide.SHORT)
    eth_pos = PositionState(
        key=eth_key,
        qty=Decimal("0.05"),
        entry_price=Decimal("3000.0"),
        mark_price=Decimal("2950.0"),
        liquidation_price=Decimal("3500.0"),
        leverage=100,
        stop=None,
        take_profit=None,
        unrealized_profit=Decimal("2.5"),
    )

    # 3. SOL with qty=0 (should be excluded)
    sol_key = PositionKey(symbol="SOLUSDT", pos_side=PosSide.LONG)
    sol_pos = PositionState(
        key=sol_key,
        qty=Decimal("0.0"),
        entry_price=Decimal("150.0"),
        mark_price=Decimal("150.0"),
        liquidation_price=Decimal("0.0"),
        leverage=50,
    )

    mock_snapshot = {
        "positions": {btc_key: btc_pos, eth_key: eth_pos, sol_key: sol_pos},
        "open_orders": [],
    }
    runtime.refresh_snapshot = lambda account_id: mock_snapshot
    runtime.cached_snapshot = lambda account_id: mock_snapshot

    res = await service.positions(7, refresh=True)
    assert res.count == 2
    assert len(res.positions) == 2

    # Sorted by symbol: BTCUSDT, ETHUSDT
    btc_dto = res.positions[0]
    assert btc_dto.symbol == "BTCUSDT"
    assert btc_dto.side == PositionSide.LONG
    assert btc_dto.qty == Decimal("0.002")
    assert btc_dto.entry_price == Decimal("60000.0")
    assert btc_dto.mark_price == Decimal("61000.0")
    assert btc_dto.unrealized_profit == Decimal("2.0")
    assert btc_dto.leverage == 200
    assert btc_dto.protection_state == PositionProtectionState.PROTECTED
    assert btc_dto.stop_price == Decimal("59000.0")
    assert btc_dto.take_profit_price == Decimal("65000.0")
    # Distance = |61000 - 59000| = 2000, per unit loss = 2000 * 1.001 = 2002.
    # Risk = 0.002 * 2002 = 4.004
    assert btc_dto.risk_to_stop_usdt == Decimal("4.004")

    # Raw secrets/identifiers check
    raw_str = repr(res)
    assert "btc-stop-coid" not in raw_str
    assert "12345" not in raw_str
    assert "tg-7" not in raw_str

    eth_dto = res.positions[1]
    assert eth_dto.symbol == "ETHUSDT"
    assert eth_dto.side == PositionSide.SHORT
    assert eth_dto.protection_state == PositionProtectionState.MISSING_STOP
    assert eth_dto.risk_to_stop_usdt is None


async def test_positions_freshness_ttl_and_refresh_failure_fallback(environment):
    """Fresh / expired TTL / refresh-failure fallback coverage for positions().

    - A refresh read (fresh capture) is not stale and may expose risk.
    - The same cached snapshot served after the TTL window must flip stale
      and suppress risk_to_stop_usdt even while the system stays READY.
    - A failed refresh falls back to the cached snapshot as stale data with
      risk_to_stop_usdt=None and a human-readable message.
    """
    from datetime import timedelta

    from copytradeaster.domain.models import (
        PositionKey,
        PositionState,
        PosSide,
        ProtectiveOrder,
        Side,
    )

    _, runtime, _, clock, service = environment

    btc_key = PositionKey(symbol="BTCUSDT", pos_side=PosSide.BOTH)
    btc_pos = PositionState(
        key=btc_key,
        qty=Decimal("0.002"),
        entry_price=Decimal("60000.0"),
        mark_price=Decimal("61000.0"),
        liquidation_price=Decimal("50000.0"),
        leverage=200,
        stop=ProtectiveOrder(
            client_order_id="btc-stop-coid",
            order_id=12345,
            type="STOP_MARKET",
            side=Side.SELL,
            stop_price=Decimal("59000.0"),
            reduce_only=True,
            close_position=True,
            working_type="CONTRACT_PRICE",
            orig_qty=Decimal("0"),
        ),
        unrealized_profit=Decimal("2.0"),
    )
    mock_snapshot = {"positions": {btc_key: btc_pos}, "open_orders": []}

    state: dict[str, object] = {"snapshot": mock_snapshot, "captured_at": None}

    async def refresh_snapshot(account_id):
        del account_id
        captured = clock()
        state["captured_at"] = captured
        return state["snapshot"], captured

    async def cached_snapshot(account_id):
        del account_id
        return (state["snapshot"], state["captured_at"])

    runtime.refresh_snapshot = refresh_snapshot
    runtime.cached_snapshot = cached_snapshot

    # Fresh: refresh returns a just-captured snapshot.
    fresh = await service.positions(7, refresh=True)
    assert fresh.is_stale is False
    assert fresh.positions[0].risk_to_stop_usdt == Decimal("4.004")

    # Expired: the same cache served past the TTL window must be stale.
    clock.value = clock.value + timedelta(seconds=45)
    expired = await service.positions(7, refresh=False)
    assert expired.is_stale is True
    assert expired.snapshot_captured_at is not None
    assert expired.positions[0].risk_to_stop_usdt is None

    # Refresh failure falls back to the cached snapshot as stale with no risk.
    clock.value = clock.value - timedelta(seconds=45)

    async def failing_refresh(account_id):
        del account_id
        raise RuntimeError("aster transport is down")

    runtime.refresh_snapshot = failing_refresh
    fallback = await service.positions(7, refresh=True)
    assert fallback.is_stale is True
    assert fallback.message is not None
    assert fallback.positions[0].risk_to_stop_usdt is None
    # The fallback page still exposes the position itself for context.
    assert fallback.positions[0].symbol == "BTCUSDT"


async def test_positions_stop_validation_strict_side_and_coverage(environment):
    """Stop orders that do not strictly oppose the position, or carry an
    invalid quantity for a quantity-based stop, must never be PROTECTED."""
    from copytradeaster.domain.models import (
        PositionKey,
        PositionState,
        PosSide,
        ProtectiveOrder,
        Side,
    )

    _, runtime, _, _, service = environment
    btc_key = PositionKey(symbol="BTCUSDT", pos_side=PosSide.BOTH)

    def make_position(stop: ProtectiveOrder) -> PositionState:
        return PositionState(
            key=btc_key,
            qty=Decimal("0.002"),
            entry_price=Decimal("60000.0"),
            mark_price=Decimal("61000.0"),
            liquidation_price=Decimal("50000.0"),
            leverage=200,
            stop=stop,
            unrealized_profit=Decimal("0"),
        )

    def make_stop(
        *,
        side=Side.SELL,
        reduce_only=True,
        close_position=False,
        orig_qty=Decimal("0.002"),
        stop_price=Decimal("59000.0"),
    ) -> ProtectiveOrder:
        return ProtectiveOrder(
            client_order_id="stop-coid",
            order_id=1,
            type="STOP_MARKET",
            side=side,
            stop_price=stop_price,
            reduce_only=reduce_only,
            close_position=close_position,
            working_type="CONTRACT_PRICE",
            orig_qty=orig_qty,
        )

    # Same-direction stop (LONG protected by a BUY stop) must be UNCONFIRMED.
    wrong_side = make_position(make_stop(side=Side.BUY))

    cases = [
        (wrong_side, PositionProtectionState.UNCONFIRMED),
        # Quantity-based stop with zero qty: UNCONFIRMED (not full protection).
        (
            make_position(make_stop(close_position=False, orig_qty=Decimal("0"))),
            PositionProtectionState.UNCONFIRMED,
        ),
        # Quantity-based stop covering only part of the position: PARTIAL_STOP.
        (
            make_position(make_stop(close_position=False, orig_qty=Decimal("0.001"))),
            PositionProtectionState.PARTIAL_STOP,
        ),
        # Quantity-based stop covering the whole position: PROTECTED.
        (
            make_position(make_stop(close_position=False, orig_qty=Decimal("0.002"))),
            PositionProtectionState.PROTECTED,
        ),
    ]

    for position, expected in cases:
        snap = {"positions": {btc_key: position}, "open_orders": []}
        runtime.refresh_snapshot = lambda account_id, snap=snap: snap
        runtime.cached_snapshot = lambda account_id, snap=snap: snap
        page = await service.positions(7, refresh=True)
        dto = page.positions[0]
        assert dto.protection_state is expected, (
            f"expected {expected}, got {dto.protection_state} for stop={position.stop}"
        )
        if expected is not PositionProtectionState.PROTECTED:
            assert dto.risk_to_stop_usdt is None


async def test_user_views_reflect_global_pause_even_with_active_account_policy(environment):
    db, _, _, _, service = environment
    await SettingsRepo(db).patch_policy("tg-7", allow_new_entries=True)
    await SettingsRepo(db).set_flag("allow_new_entries", "false")

    dashboard = await service.dashboard(7)
    status = await service.status(7)

    assert dashboard.copy_state is CopyState.PAUSED
    assert dashboard.message == "global pause is active"
    assert status.copy_state is CopyState.PAUSED


async def test_status_cache_and_refresh_boundaries_are_explicit(environment):
    _, runtime, _, _, service = environment
    runtime.actions.clear()

    await service.status(7, refresh=False)
    assert any(action[0] == "cached_snapshot" for action in runtime.actions)
    assert not any(action[0] == "refresh_snapshot" for action in runtime.actions)
    assert runtime.permission_refresh_calls == 0

    await service.status(7, refresh=True)
    assert any(action[0] == "refresh_snapshot" for action in runtime.actions)
    assert runtime.permission_refresh_calls == 1


async def test_direct_connect_replacement_and_secret_safe_failures(environment):
    _, _, onboarding, _, service = environment
    credentials = DirectCredentialsInput(
        user_address="0x1111111111111111111111111111111111111111",
        signer_address="0x2222222222222222222222222222222222222222",
        signer_private_key="private-key-value",
    )
    with pytest.raises(ValidationError):
        await service.connect_direct(7, credentials)
    assert not onboarding.connected

    result = await service.replace_direct(7, credentials)
    assert result.paused
    assert onboarding.replaced[0].signer_private_key == "private-key-value"
    assert "private-key-value" not in repr(result)


@pytest.mark.parametrize(
    "code",
    [
        OnboardingErrorCode.INVALID_INPUT,
        OnboardingErrorCode.AGENT_NOT_FOUND,
        OnboardingErrorCode.AGENT_SIGNER_MISMATCH,
        OnboardingErrorCode.AGENT_READ_PERMISSION,
        OnboardingErrorCode.AGENT_PERP_PERMISSION,
        OnboardingErrorCode.AGENT_WITHDRAW_PERMISSION,
        OnboardingErrorCode.AGENT_EXPIRED,
        OnboardingErrorCode.AGENT_IP_WHITELIST,
        OnboardingErrorCode.SIGNED_READ_FAILED,
        OnboardingErrorCode.ACCOUNT_NOT_FLAT,
        OnboardingErrorCode.OPEN_ORDERS_PRESENT,
        OnboardingErrorCode.POSITION_MODE_MISMATCH,
        OnboardingErrorCode.COMMIT_FAILED,
        OnboardingErrorCode.ACTIVATION_FAILED,
    ],
)
async def test_direct_connect_preserves_typed_onboarding_code_before_commit(environment, code):
    db, _, onboarding, _, service = environment
    await TelegramUserRepo(db).upsert(8, 800, role="USER")

    async def reject(request):
        del request
        raise OnboardingError(code)

    onboarding.onboard = reject
    with pytest.raises(OnboardingApplicationError) as exc_info:
        await service.connect_direct(
            8,
            DirectCredentialsInput(
                "0x3333333333333333333333333333333333333333",
                "0x4444444444444444444444444444444444444444",
                "fresh-secret",
            ),
        )

    assert exc_info.value.code is code
    assert onboarding.connected == []
    assert await AccountRepo(db).get("tg-8") is None
    assert "fresh-secret" not in repr(exc_info.value)


async def test_connect_audit_keeps_typed_rejection_and_credentials_out_of_logs(environment, caplog):
    db, _, onboarding, _, service = environment
    await TelegramUserRepo(db).upsert(8, 800, role="USER")
    user_address = "0x3333333333333333333333333333333333333333"
    signer_address = "0x4444444444444444444444444444444444444444"
    secret = "connect-audit-private-secret"

    async def reject(request):
        del request
        raise OnboardingError(OnboardingErrorCode.AGENT_NOT_FOUND)

    onboarding.onboard = reject
    with caplog.at_level("INFO"), pytest.raises(OnboardingApplicationError):
        await service.connect_direct(
            8,
            DirectCredentialsInput(user_address, signer_address, secret),
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "onboarding_audit event=attempt mode=connect user_id=8 account_id=tg-8" in message
        and "paused=unknown" in message
        for message in messages
    )
    assert any(
        "onboarding_audit event=rejected mode=connect" in message
        and "category=validation" in message
        and "code=agent_not_found" in message
        and "result=rejected" in message
        and "paused=unknown" in message
        for message in messages
    )
    joined = "\n".join(messages)
    assert user_address not in joined
    assert signer_address not in joined
    assert secret not in joined


async def test_connect_replace_and_recovery_audits_are_distinct_and_paused(environment, caplog):
    db, runtime, _, _, service = environment
    await TelegramUserRepo(db).upsert(8, 800, role="USER")
    credentials = DirectCredentialsInput(
        "0x3333333333333333333333333333333333333333",
        "0x4444444444444444444444444444444444444444",
        "fresh-audit-secret",
    )

    with caplog.at_level("INFO"):
        connected = await service.connect_direct(8, credentials)
    assert connected.paused is True
    assert any(
        "event=success mode=connect" in record.getMessage()
        and "account_id=tg-8" in record.getMessage()
        and "paused=true" in record.getMessage()
        for record in caplog.records
    )

    caplog.clear()
    runtime.follower_registry.account_ids.clear()
    runtime.recover_runtime = True
    recovery_credentials = DirectCredentialsInput(
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "recovery-audit-secret",
    )
    with caplog.at_level("INFO"):
        recovered = await service.connect_direct(7, recovery_credentials)
    assert recovered.paused is True
    assert any(
        "event=recovered mode=connect" in record.getMessage()
        and "result=recovered" in record.getMessage()
        and "account_id=tg-7" in record.getMessage()
        and "paused=true" in record.getMessage()
        for record in caplog.records
    )

    caplog.clear()
    with caplog.at_level("INFO"):
        replaced = await service.replace_direct(7, recovery_credentials)
    assert replaced.paused is True
    assert any(
        "event=attempt mode=replace" in record.getMessage()
        and "account_id=tg-7" in record.getMessage()
        and "paused=unknown" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        "event=success mode=replace" in record.getMessage()
        and "account_id=tg-7" in record.getMessage()
        and "paused=true" in record.getMessage()
        for record in caplog.records
    )


async def test_unexpected_onboarding_failure_audit_has_type_without_payload(environment, caplog):
    _, _, onboarding, _, service = environment
    payload_marker = "exchange raw payload should not be logged"

    async def fail(request, *, preflight):
        del request, preflight
        raise RuntimeError(payload_marker)

    onboarding.fail_replace = True
    onboarding.replace = fail
    with caplog.at_level("INFO"), pytest.raises(ServiceUnavailableError):
        await service.replace_direct(
            7,
            DirectCredentialsInput(
                "0x1111111111111111111111111111111111111111",
                "0x2222222222222222222222222222222222222222",
                "unexpected-audit-secret",
            ),
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=rejected mode=replace" in message
        and "category=unexpected" in message
        and "exception_type=RuntimeError" in message
        and "paused=unknown" in message
        for message in messages
    )
    joined = "\n".join(messages)
    assert payload_marker not in joined
    assert "unexpected-audit-secret" not in joined


@pytest.mark.asyncio
async def test_replacement_preflight_error_keeps_old_account_and_typed_code(environment):
    db, runtime, onboarding, _, service = environment

    async def reject(request):
        del request
        raise OnboardingError(OnboardingErrorCode.AGENT_NOT_FOUND)

    onboarding.preflight = reject
    with pytest.raises(OnboardingApplicationError) as exc_info:
        await service.replace_direct(
            7,
            DirectCredentialsInput(
                "0x1111111111111111111111111111111111111111",
                "0x2222222222222222222222222222222222222222",
                "replacement-secret",
            ),
        )

    assert exc_info.value.code is OnboardingErrorCode.AGENT_NOT_FOUND
    account = await AccountRepo(db).get("tg-7")
    assert account is not None and int(account["enabled"]) == 1
    assert runtime.events == []
    assert onboarding.replaced == []


async def test_direct_connect_recovers_committed_account_without_runtime(environment):
    _, runtime, onboarding, _, service = environment
    runtime.follower_registry.account_ids.clear()
    runtime.recover_runtime = True
    credentials = DirectCredentialsInput(
        user_address="0x1111111111111111111111111111111111111111",
        signer_address="0x2222222222222222222222222222222222222222",
        signer_private_key="not-recommitted",
    )

    result = await service.connect_direct(7, credentials)

    assert result.paused is True
    assert result.message == "account runtime recovered paused"
    assert not onboarding.connected
    assert runtime.follower_registry.get("tg-7") is not None


async def test_decimal_validation_and_pause_resume_confirmation(environment):
    _, runtime, _, _, service = environment
    with pytest.raises(ValidationError):
        await service.update_risk(
            7,
            RiskUpdateInput(risk_percent=0.1),  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("1.1")))

    paused = await service.pause(7)
    assert paused.copy_state is CopyState.PAUSED
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    resumed = await service.confirm_action(7, confirmation)
    assert resumed.copy_state is CopyState.ACTIVE
    assert ("reconcile_account", "tg-7") in runtime.actions
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(7, confirmation)


async def test_control_audit_logs_safe_control_round_trips_and_states(environment, caplog):
    _, _, _, _, service = environment
    caplog.clear()
    with caplog.at_level("INFO"):
        with pytest.raises(ValidationError):
            await service.update_risk(
                7,
                RiskUpdateInput(
                    risk_percent=Decimal("0.02"),
                    max_leverage=5,
                    max_entry_deviation_pct=Decimal("0.03"),
                ),
            )
        await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.04")))
        await service.set_notifications(7, False)
        await service.set_notifications(7, True)
        await service.pause(7)
        confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
        await service.confirm_action(7, confirmation)

    messages = [record.getMessage() for record in caplog.records]
    risk_success = [
        message
        for message in messages
        if "control_audit action=update_risk result=success" in message
    ]
    assert len(risk_success) == 1
    assert "changed_fields=risk_percent" in risk_success[0]
    assert all(value not in "\n".join(risk_success) for value in ("0.04",))
    notification_success = [
        message
        for message in messages
        if "control_audit action=set_notifications result=success" in message
    ]
    assert any("state=disabled" in message for message in notification_success)
    assert any("state=enabled" in message for message in notification_success)
    assert any(
        "control_audit action=pause result=success" in message and "state=paused" in message
        for message in messages
    )
    assert any(
        "control_audit action=confirm_resume result=success" in message
        and "state=resumed" in message
        for message in messages
    )


async def test_admin_control_audit_logs_terminal_state_and_safe_targets(environment, caplog):
    db, _, _, _, service = environment
    admin_id = next(iter(service._admin_ids))
    target_user_id = max(service._admin_ids) + 1000
    approved_user_id = target_user_id + 1
    rejected_user_id = target_user_id + 2
    account_id = str((await AccountRepo(db).get_all(role="follower"))[0]["id"])

    approved_request = await service.submit_access_request(
        approved_user_id,
        approved_user_id * 10,
        username=None,
        display_name=None,
    )
    rejected_request = await service.submit_access_request(
        rejected_user_id,
        rejected_user_id * 10,
        username=None,
        display_name=None,
    )

    caplog.clear()
    with caplog.at_level("INFO"):
        await service.admin_allow_user(admin_id, target_user_id)
        await service.admin_set_user_access(admin_id, target_user_id, False)
        await service.admin_set_user_access(admin_id, target_user_id, True)
        await service.admin_decide_access(admin_id, approved_request.request_id, approve=True)
        await service.admin_decide_access(admin_id, approved_request.request_id, approve=False)
        await service.admin_decide_access(admin_id, rejected_request.request_id, approve=False)
        await service.admin_reconcile(admin_id, account_id)
        await service.admin_reconcile(admin_id)

        pause = await service.begin_confirmation(admin_id, ConfirmationAction.GLOBAL_PAUSE)
        await service.admin_pause(admin_id, pause)
        resume = await service.begin_confirmation(admin_id, ConfirmationAction.GLOBAL_RESUME)
        await service.admin_resume(admin_id, resume)
        emergency = await service.begin_confirmation(
            admin_id,
            ConfirmationAction.GLOBAL_EMERGENCY,
        )
        await service.admin_emergency(admin_id, emergency)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "control_audit action=admin_allow_user result=success" in message
        and f"target_user_id={target_user_id}" in message
        and "changed_fields=access_enabled" in message
        and "state=enabled" in message
        for message in messages
    )
    set_success = [
        message
        for message in messages
        if "control_audit action=admin_set_user_access result=success" in message
    ]
    assert any(
        f"target_user_id={target_user_id}" in message and "state=disabled" in message
        for message in set_success
    )
    assert any(
        f"target_user_id={target_user_id}" in message and "state=enabled" in message
        for message in set_success
    )
    decision_success = [
        message
        for message in messages
        if "control_audit action=admin_decide_access result=success" in message
    ]
    approved_success = [
        message for message in decision_success if f"target_user_id={approved_user_id}" in message
    ]
    rejected_success = [
        message for message in decision_success if f"target_user_id={rejected_user_id}" in message
    ]
    assert len(approved_success) == 2
    assert any(
        "state=approved" in message and "changed_fields=status,access_enabled" in message
        for message in approved_success
    )
    assert any(
        "state=approved" in message and "changed_fields=" not in message
        for message in approved_success
    )
    assert len(rejected_success) == 1
    assert "changed_fields=status state=rejected" in rejected_success[0]
    decide_attempts = [
        message
        for message in messages
        if "control_audit action=admin_decide_access result=attempt" in message
    ]
    assert len(decide_attempts) == 3
    assert all("target_user_id=" not in message for message in decide_attempts)
    assert all(
        f"target_user_id={request_id}" not in message
        for message in decide_attempts
        for request_id in (approved_request.request_id, rejected_request.request_id)
    )
    reconcile_success = [
        message
        for message in messages
        if "control_audit action=admin_reconcile result=success" in message
    ]
    assert len(reconcile_success) == 2
    assert any("state=account" in message for message in reconcile_success)
    assert any("state=all" in message for message in reconcile_success)
    account_address = (await AccountRepo(db).get(account_id))["user_address"]
    assert all(
        account_id not in message and account_address not in message and "0x" not in message
        for message in reconcile_success
    )
    assert any(
        "control_audit action=admin_pause result=success" in message
        and "changed_fields=allow_new_entries" in message
        and "state=paused" in message
        for message in messages
    )
    assert any(
        "control_audit action=admin_resume result=success" in message
        and "changed_fields=allow_new_entries" in message
        and "state=resumed" in message
        for message in messages
    )
    assert any(
        "control_audit action=admin_emergency result=success" in message
        and "changed_fields=allow_new_entries,emergency_active" in message
        and "state=emergency" in message
        for message in messages
    )

    caplog.clear()
    with caplog.at_level("WARNING"), pytest.raises(ValidationError):
        await service.admin_set_user_access(admin_id, target_user_id, "opaque token")  # type: ignore[arg-type]
    rejected = [
        record.getMessage()
        for record in caplog.records
        if "control_audit action=admin_set_user_access result=rejected" in record.getMessage()
    ]
    assert rejected
    assert any("category=validation code=invalid_input" in message for message in rejected)
    assert all("opaque token" not in message for message in rejected)


async def test_control_audit_rejected_is_safe_and_logging_cannot_change_semantics(
    environment, caplog, monkeypatch
):
    _, _, _, _, service = environment
    payload = "raw exchange payload private_key=never-log"

    async def fail_patch(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(payload)

    monkeypatch.setattr(service._settings, "patch_policy", fail_patch)
    with caplog.at_level("INFO"), pytest.raises(ServiceUnavailableError):
        await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02")))

    rejected = [
        record.getMessage()
        for record in caplog.records
        if "control_audit action=update_risk result=rejected" in record.getMessage()
    ]
    assert rejected
    assert any(
        "category=service_unavailable code=service_unavailable" in message for message in rejected
    )
    assert payload not in "\n".join(rejected)

    def broken_audit(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("audit sink failure")

    monkeypatch.setattr(service_module, "log_control_audit", broken_audit)
    result = await service.set_notifications(7, False)
    assert result.enabled is False
    with pytest.raises(ValidationError):
        await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("1.1")))


async def test_confirmation_is_ttl_bound_to_user_action_and_account(environment):
    _, _, _, clock, service = environment
    with pytest.raises(AuthorizationError):
        await service.begin_confirmation(7, ConfirmationAction.EMERGENCY)
    with pytest.raises(AuthorizationError):
        await service.confirm_action(
            7,
            Confirmation("forged", ConfirmationAction.EMERGENCY, "tg-7"),
        )
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    forged = type(confirmation)(
        token=confirmation.token,
        action=ConfirmationAction.DISCONNECT,
        account_id=confirmation.account_id,
        expires_at=confirmation.expires_at,
    )
    with pytest.raises(AuthorizationError):
        await service.confirm_action(7, forged)

    clock.value += timedelta(seconds=61)
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(7, confirmation)


async def test_confirmation_is_invalidated_by_credential_replacement(environment):
    _, runtime, _, _, service = environment
    confirmation = await service.begin_confirmation(7, ConfirmationAction.DISCONNECT)
    runtime.account_version_value = "credentials-v2"

    with pytest.raises(ConfirmationExpiredError, match="account has changed"):
        await service.confirm_action(7, confirmation)


async def test_emergency_disconnect_and_admin_controls_are_real_effects(environment):
    db, runtime, _, _, service = environment
    with pytest.raises(AuthorizationError):
        await service.begin_confirmation(7, ConfirmationAction.EMERGENCY)
    assert not runtime.emergency_controller.account_calls
    assert await SettingsRepo(db).get_flag("emergency_active") is None

    disconnect_confirmation = await service.begin_confirmation(7, ConfirmationAction.DISCONNECT)
    disconnected = await service.confirm_action(7, disconnect_confirmation)
    assert runtime.disconnect_called
    assert disconnected.copy_state is CopyState.EMPTY

    admin = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_EMERGENCY)
    await service.admin_emergency(100, admin)
    assert ("pause_entries",) in runtime.actions
    assert ("reopen_entries_while_paused",) in runtime.actions
    assert runtime.emergency_controller.global_calls
    with pytest.raises(AuthorizationError):
        await service.begin_confirmation(7, ConfirmationAction.GLOBAL_PAUSE)


async def test_missing_destructive_adapter_fails_closed(environment):
    db, runtime, _, _, _ = environment

    class NoEmergencyRuntime(FakeRuntime):
        emergency_controller = None

    no_effect_runtime = NoEmergencyRuntime(db)
    no_effect_runtime.follower_registry = runtime.follower_registry
    no_effect_runtime.settings = runtime.settings
    no_effect_runtime.emergency_controller = None
    service = UserApplicationService(no_effect_runtime, admin_ids=[100])
    with pytest.raises(AuthorizationError):
        await service.begin_confirmation(7, ConfirmationAction.EMERGENCY)
    assert not any(action[0] == "emergency" for action in no_effect_runtime.actions)


async def test_confirmation_token_has_presenter_budget_and_is_single_use(environment):
    _, _, _, _, service = environment

    confirmations = [
        await service.begin_confirmation(7, action)
        for action in (ConfirmationAction.RESUME, ConfirmationAction.DISCONNECT)
    ]
    assert all(len(item.token.encode("utf-8")) <= 16 for item in confirmations)
    assert all(item.token.isascii() for item in confirmations)
    assert all("private" not in repr(item).casefold() for item in confirmations)

    await service.confirm_action(7, confirmations[0])
    # A second attempt cannot replay a consumed token.
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(7, confirmations[0])


async def test_stale_permission_refresh_is_signed_adapter_result_and_persisted(environment):
    db, runtime, _, clock, service = environment
    stale = AgentValidation(
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at="2026-08-23T11:00:00+00:00",
    )
    await AgentValidationRepo(db).upsert("tg-7", validation=stale)
    clock.value = datetime(2026, 8, 23, 12, 2, tzinfo=UTC)

    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    await service.confirm_action(7, confirmation)

    assert runtime.permission_refresh_calls == 1
    row = await AgentValidationRepo(db).get("tg-7")
    assert row is not None
    assert row["checked_at"] == "2026-08-23T12:02:00+00:00"


async def test_pause_missing_barrier_does_not_mutate_policy(environment):
    db, runtime, _, _, service = environment
    runtime.pause_account = None

    with pytest.raises(ServiceUnavailableError):
        await service.pause(7)
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False


async def test_global_pause_resume_are_durable_and_reopen_admission(environment):
    db, runtime, _, _, service = environment

    pause_confirmation = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_PAUSE)
    await service.admin_pause(100, pause_confirmation)
    assert await SettingsRepo(db).get_flag("allow_new_entries") == "false"
    assert runtime.admission_open
    pause_index = runtime.actions.index(("pause_entries",))
    reopen_index = runtime.actions.index(("reopen_entries_while_paused",))
    assert pause_index < reopen_index
    assert ("resume_entries",) not in runtime.actions

    resume_confirmation = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_RESUME)
    await service.admin_resume(100, resume_confirmation)
    assert await SettingsRepo(db).get_flag("allow_new_entries") == "true"
    assert runtime.admission_open
    assert ("resume_entries",) in runtime.actions


async def test_resume_requires_confirmation_even_when_master_has_no_positions(environment):
    db, runtime, _, _, service = environment
    runtime.master_has_positions = False
    await service.pause(7)
    assert (await SettingsRepo(db).get_policy("tg-7")).allow_new_entries is False

    with pytest.raises(ConfirmationExpiredError):
        await service.resume(7)

    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    dashboard = await service.resume(7, confirmation=confirmation)
    assert dashboard.copy_state is CopyState.ACTIVE
    assert (await SettingsRepo(db).get_policy("tg-7")).allow_new_entries is True


async def test_resume_safety_rejects_manual_or_external_state_without_reopen(environment):
    db, runtime, _, _, service = environment
    runtime.fail_resume_safety = StaleOperationError("manual state")
    await service.pause(7)
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)

    with pytest.raises(StaleOperationError):
        await service.confirm_action(7, confirmation)
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False
    assert not any(action[0] == "resume" for action in runtime.actions)


async def test_resume_rechecks_global_pause_at_mutation_boundary(environment):
    db, runtime, _, _, service = environment
    await service.pause(7)

    async def global_pause_race(account_id: str, snapshot):
        del account_id, snapshot
        await SettingsRepo(db).set_flag("allow_new_entries", "false")

    runtime.validate_resume_safety = global_pause_race
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)

    with pytest.raises(ServiceUnavailableError, match="global pause"):
        await service.confirm_action(7, confirmation)
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False
    assert not any(action[0] == "resume" for action in runtime.actions)


async def test_resume_rejects_latched_global_gates_with_distinct_codes(environment):
    db, runtime, _, _, service = environment
    del runtime
    await service.pause(7)

    await SettingsRepo(db).set_flag("emergency_active", "true")
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(GlobalEmergencyActiveError) as emergency_info:
        await service.confirm_action(7, confirmation)
    assert emergency_info.value.public_code == "global_emergency_active"

    await SettingsRepo(db).set_flag("emergency_active", "false")
    await SettingsRepo(db).set_flag("allow_new_entries", "false")
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(GlobalPauseActiveError) as pause_info:
        await service.confirm_action(7, confirmation)
    assert pause_info.value.public_code == "global_pause_active"


async def test_resume_rejects_credential_change_after_signed_checks(environment):
    db, runtime, _, _, service = environment
    await service.pause(7)

    async def replacement_race(account_id: str, snapshot):
        del account_id, snapshot
        runtime.account_version_value = "credentials-v2"

    runtime.validate_resume_safety = replacement_race
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)

    with pytest.raises(StaleOperationError, match="credentials changed"):
        await service.confirm_action(7, confirmation)
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False
    assert not any(action[0] == "resume" for action in runtime.actions)


async def test_valid_testnet_permission_override_requires_explicit_adapter_flag(environment):
    db, runtime, _, clock, service = environment
    override = AgentValidation(
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="",
        status="VALID_OVERRIDE",
        checked_at=clock.value.isoformat(),
    )
    await AgentValidationRepo(db).upsert("tg-7", validation=override)
    runtime.permission_override = False
    await service.pause(7)
    denied = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(StaleOperationError):
        await service.confirm_action(7, denied)

    runtime.permission_override = True
    allowed = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    result = await service.confirm_action(7, allowed)
    assert result.copy_state is CopyState.ACTIVE


async def test_resume_uses_bounded_testnet_override_cache_on_unknown_agent_read(environment):
    db, runtime, _, clock, service = environment
    override = AgentValidation(
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="",
        status="VALID_OVERRIDE",
        checked_at=(clock.value - timedelta(minutes=2)).isoformat(),
    )
    await AgentValidationRepo(db).upsert("tg-7", validation=override)
    runtime.permission_override = True
    runtime.permission_refresh_error = ServiceUnavailableError("Aster unavailable")
    await service.pause(7)

    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    result = await service.resume(7, confirmation)

    assert result.copy_state is CopyState.ACTIVE
    assert runtime.permission_refresh_calls == 1


async def test_resume_rejects_revoked_agent_without_override_fallback(environment):
    db, runtime, _, clock, service = environment
    override = AgentValidation(
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="",
        status="VALID_OVERRIDE",
        checked_at=(clock.value - timedelta(minutes=2)).isoformat(),
    )
    await AgentValidationRepo(db).upsert("tg-7", validation=override)
    runtime.permission_override = True
    runtime.permission_refresh_error = AsterError(-2015, "agent not found")
    await service.pause(7)
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)

    with pytest.raises(StaleOperationError):
        await service.resume(7, confirmation)

    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False


async def test_resume_rejects_expired_testnet_override_cache_on_unknown_agent_read(environment):
    db, runtime, _, clock, service = environment
    override = AgentValidation(
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="",
        status="VALID_OVERRIDE",
        checked_at=(clock.value - timedelta(days=2)).isoformat(),
    )
    await AgentValidationRepo(db).upsert("tg-7", validation=override)
    runtime.permission_override = True
    runtime.permission_refresh_error = ServiceUnavailableError("Aster unavailable")
    await service.pause(7)
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)

    with pytest.raises(ServiceUnavailableError):
        await service.resume(7, confirmation)

    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False


async def test_disconnect_safety_failure_keeps_account_enabled_and_credentials(environment):
    db, runtime, _, _, service = environment
    runtime.lifecycle.safe = False
    confirmation = await service.begin_confirmation(7, ConfirmationAction.DISCONNECT)

    with pytest.raises(ServiceUnavailableError):
        await service.confirm_action(7, confirmation)
    account = await AccountRepo(db).get("tg-7")
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert account is not None and int(account["enabled"]) == 1
    assert policy is not None and policy.allow_new_entries is False
    assert not runtime.disconnect_called
    assert not runtime.lifecycle.unpublished


async def test_replacement_preflight_pause_safe_commit_and_runtime_cas(environment):
    db, runtime, onboarding, _, service = environment
    result = await service.replace_direct(
        7,
        DirectCredentialsInput(
            user_address="0x1111111111111111111111111111111111111111",
            signer_address="0x2222222222222222222222222222222222222222",
            signer_private_key="private-key-value",
        ),
    )

    assert result.paused
    assert onboarding.events == ["preflight", "replace"]
    assert runtime.lifecycle.published
    assert runtime.events.index("safe") < runtime.events.index("publish")
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False


async def test_replacement_failure_leaves_old_runtime_and_account_usable(environment):
    db, runtime, onboarding, _, service = environment
    onboarding.fail_replace = True

    with pytest.raises(ServiceUnavailableError):
        await service.replace_direct(
            7,
            DirectCredentialsInput(
                user_address="0x1111111111111111111111111111111111111111",
                signer_address="0x2222222222222222222222222222222222222222",
                signer_private_key="private-key-value",
            ),
        )
    account = await AccountRepo(db).get("tg-7")
    assert account is not None and int(account["enabled"]) == 1
    assert not runtime.lifecycle.published
    assert not runtime.lifecycle.unpublished


async def test_connect_fresh_account_and_recovery_rejection_are_explicit(environment):
    db, runtime, onboarding, _, service = environment
    await TelegramUserRepo(db).upsert(8, 800, role="USER")
    credentials = DirectCredentialsInput(
        user_address="0x3333333333333333333333333333333333333333",
        signer_address="0x4444444444444444444444444444444444444444",
        signer_private_key="fresh-secret",
    )
    result = await service.connect_direct(8, credentials)
    assert result.account_id == "tg-8"
    assert onboarding.connected[-1].telegram_user_id == 8

    runtime.follower_registry.account_ids.clear()
    runtime.recover_runtime = False
    with pytest.raises(ServiceUnavailableError):
        await service.connect_direct(
            7,
            DirectCredentialsInput(
                user_address="0x1111111111111111111111111111111111111111",
                signer_address="0x2222222222222222222222222222222222222222",
                signer_private_key="retry-secret",
            ),
        )


async def test_connect_and_replace_validate_inputs_and_authority(environment):
    _, _, onboarding, _, service = environment
    with pytest.raises(AuthorizationError):
        await service.connect_direct(404, DirectCredentialsInput("user", "signer", "key"))
    admin_result = await service.connect_direct(
        100,
        DirectCredentialsInput(
            user_address="0x4444444444444444444444444444444444444444",
            signer_address="0x5555555555555555555555555555555555555555",
            signer_private_key="admin-key",
        ),
    )
    assert admin_result.account_id == "tg-100"
    with pytest.raises(ValidationError):
        await service.connect_direct(7, object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await service.replace_direct(
            7,
            DirectCredentialsInput(
                user_address="0x3333333333333333333333333333333333333333",
                signer_address="0x2222222222222222222222222222222222222222",
                signer_private_key="key",
            ),
        )
    assert [request.telegram_user_id for request in onboarding.connected] == [100]


@pytest.mark.parametrize(
    "values",
    [
        RiskUpdateInput(risk_percent=Decimal("0")),
        RiskUpdateInput(risk_percent=Decimal("1.01")),
        RiskUpdateInput(risk_percent=Decimal("0.004")),
        RiskUpdateInput(risk_percent=Decimal("0.06")),
        RiskUpdateInput(risk_percent=Decimal("0.012")),
        RiskUpdateInput(max_leverage=True),  # type: ignore[arg-type]
        RiskUpdateInput(max_leverage=0),
        RiskUpdateInput(max_entry_deviation_pct=Decimal("-0.1")),
        RiskUpdateInput(max_entry_deviation_pct=Decimal("1.1")),
        RiskUpdateInput(risk_percent=Decimal("NaN")),
    ],
)
async def test_risk_update_rejects_invalid_values(environment, values):
    _, _, _, _, service = environment
    with pytest.raises(ValidationError):
        await service.update_risk(7, values)


async def test_risk_update_empty_patch_and_missing_policy_are_safe(environment):
    db, _, _, _, service = environment
    with pytest.raises(ValidationError):
        await service.update_risk(7, RiskUpdateInput())
    await db.execute_write("DELETE FROM copy_settings WHERE account_id=?", ("tg-7",))
    with pytest.raises(ServiceUnavailableError, match="risk"):
        await service.risk(7)


async def test_dashboard_status_degraded_disconnected_and_refresh_failure(environment):
    db, runtime, _, _, service = environment
    await SettingsRepo(db).set_flag("degraded_active", "true")
    degraded = await service.dashboard(7)
    assert degraded.copy_state is CopyState.DEGRADED
    assert degraded.message == "runtime requires attention"

    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-7",))
    disconnected = await service.status(7)
    assert disconnected.copy_state is CopyState.EMPTY
    assert disconnected.state is StatusState.BLOCKED
    assert disconnected.reason is StatusReason.ACCOUNT_DISCONNECTED
    assert disconnected.remediation is StatusRemediation.OPEN_BOT

    await db.execute_write("UPDATE accounts SET enabled=1 WHERE id=?", ("tg-7",))
    await SettingsRepo(db).set_flag("degraded_active", "false")

    async def broken_refresh(account_id: str):
        del account_id
        raise RuntimeError("exchange down")

    runtime.refresh_snapshot = broken_refresh
    refreshed = await service.status(7, refresh=True)
    assert refreshed.state is StatusState.STALE
    assert refreshed.reason is StatusReason.ASTER_UNAVAILABLE
    assert refreshed.remediation is StatusRemediation.REFRESH
    # The last useful snapshot is preserved without a minted timestamp.
    assert refreshed.positions_count == 0
    assert refreshed.snapshot_captured_at is None
    assert refreshed.last_check_at is not None


async def test_routine_master_reverification_is_not_a_user_facing_degradation(environment):
    """The scheduled boundary must not read to a user as a fault.

    ``_establish_master_boundary`` fail-closes on schedule (every few sweeps),
    rebuilds the master against REST and proves convergence again within
    seconds. Showing that as DEGRADED made Dashboard/Positions/Status flash
    "requires attention" several times a minute for routine work. Only the
    trading gate should care; every genuine fault stays visible.
    """
    db, _, _, _, service = environment
    settings = SettingsRepo(db)
    await settings.set_flag("degraded", "periodic-snapshot")
    await settings.set_flag("ready", "false")
    await settings.set_flag("heartbeat_at", str(int(time.time() * 1000)))

    dashboard = await service.dashboard(7)
    assert dashboard.system_state is SystemState.READY
    assert dashboard.copy_state is not CopyState.DEGRADED
    positions = await service.positions(7)
    assert positions.is_stale is False
    status = await service.status(7)
    assert status.system_state is SystemState.READY

    # A real fault is reported exactly as before.
    await settings.set_flag("degraded", "sweep-failed:tg-7:timeout")
    faulted = await service.dashboard(7)
    assert faulted.system_state is SystemState.DEGRADED
    assert faulted.copy_state is CopyState.DEGRADED
    assert faulted.message == "runtime requires attention"

    # The latch still rules when no routine reason explains it.
    await settings.set_flag("degraded", "")
    assert (await service.dashboard(7)).system_state is SystemState.DEGRADED


async def test_master_stream_blip_is_not_a_user_facing_fault(environment):
    """Reconnect churn is engine work, not a user-visible incident.

    A dropped master stream re-listens within seconds and the reconciler keeps
    REST snapshots flowing, so painting "requires attention" for the blip would
    jitter every screen; the recovery reasons inherit the same treatment.
    """
    db, _, _, _, service = environment
    settings = SettingsRepo(db)
    now_ms = int(time.time() * 1000)
    for reason in (
        "stream-disconnected",
        "stream-reconnecting",
        "stream-startup-timeout",
        "master-changed-before-ready",
        "master-boundary-not-quiescent",
        "master-recovery:master-dirty:timeout",
    ):
        await settings.set_flag("degraded", reason)
        await settings.set_flag("degraded_since", str(now_ms))
        await settings.set_flag("ready", "false")
        await settings.set_flag("heartbeat_at", str(now_ms))
        dashboard = await service.dashboard(7)
        assert dashboard.system_state is SystemState.READY, reason


async def test_aged_verification_evidence_does_not_paint_the_status_stale(environment):
    """Verification *age* is not a fault, and the evidence TTL is one minute.

    A cached triple (read/perp/withdraw) stays the last proven truth; the row
    carries its age (`Проверка: 13:28`) and the screen re-verifies itself on
    open. Collapsing the overall verdict for it told a healthy account "data is
    stale" on every look. An attempted verification that fails is different and
    must still be reported.
    """
    _, runtime, _, clock, service = environment
    baseline = await service.status(7, refresh=True)
    assert baseline.state is StatusState.READY
    assert baseline.permissions_state is PermissionsState.VALID

    # An hour later the evidence TTL (60 s) has long expired while the cached
    # snapshot stays inside its own bound, exactly as in a real process.
    async def cached_with_stamp(account_id: str):
        return {"positions": {}, "open_orders": []}, datetime(2026, 8, 23, 12, 59, 45, tzinfo=UTC)

    runtime.cached_snapshot = cached_with_stamp
    clock.value = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
    aged = await service.status(7)
    assert aged.permissions_state is PermissionsState.STALE
    assert aged.state is StatusState.READY
    assert aged.reason is StatusReason.NONE
    assert aged.remediation is None

    def failing_refresh(account_id: str):
        raise ServiceUnavailableError("signed permission read failed")

    runtime.refresh_agent_permission = failing_refresh
    failed = await service.status(7, refresh=True)
    assert failed.state is StatusState.STALE
    assert failed.reason is StatusReason.PERMISSION_STALE


async def test_sustained_routine_window_finally_reads_as_degraded(environment):
    """Churn that does not close stops being churn.

    The same reason that is hidden for seconds must surface once the engine has
    been fail-closed long enough that it is stuck rather than self-repairing.
    """
    db, _, _, _, service = environment
    settings = SettingsRepo(db)
    now_ms = int(time.time() * 1000)
    await settings.set_flag("degraded", "periodic-snapshot")
    await settings.set_flag("degraded_since", str(now_ms - 30_000))
    await settings.set_flag("ready", "false")
    await settings.set_flag("heartbeat_at", str(now_ms))

    dashboard = await service.dashboard(7)
    assert dashboard.system_state is SystemState.DEGRADED
    assert dashboard.copy_state is CopyState.DEGRADED
    assert (await service.status(7)).system_state is SystemState.DEGRADED


async def test_degraded_episode_clock_survives_rearming_but_not_recovery(environment):
    """One episode keeps one start time; a cleared episode starts over."""
    db, _, _, _, _ = environment
    settings = SettingsRepo(db)
    now_ms = int(time.time() * 1000)

    await settings.set_flag("degraded", "periodic-snapshot")
    assert int(str(await settings.get_flag("degraded_since"))) > now_ms - 5_000
    # A re-arm inside the same episode (the next boundary, or a stream blip)
    # must not restart the clock, or a stuck engine could hide forever.
    await settings.set_flag("degraded_since", str(now_ms - 60_000))
    await settings.set_flag("degraded", "master-dirty")
    assert str(await settings.get_flag("degraded_since")) == str(now_ms - 60_000)

    await settings.clear_degraded()
    assert await settings.get_flag("degraded_since") == ""
    await settings.set_flag("degraded", "master-dirty")
    assert int(str(await settings.get_flag("degraded_since"))) > now_ms - 5_000


async def test_system_state_handles_stale_bad_heartbeat_and_unknown_runtime(environment):
    db, runtime, _, _, service = environment
    await SettingsRepo(db).set_flag("ready", "true")
    await SettingsRepo(db).set_flag("heartbeat_at", "not-a-number")
    stale = await service.dashboard(7)
    assert stale.system_state.value == "stale"

    await SettingsRepo(db).set_flag("ready", "mystery")
    runtime.ready = False
    unknown = await service.dashboard(7)
    assert unknown.system_state.value == "unknown"

    await SettingsRepo(db).set_flag("ready", "false")
    assert (await service.dashboard(7)).system_state.value == "unknown"


async def test_activity_paging_validation_and_notification_toggle(environment):
    db, _, _, clock, service = environment
    events = EventRepository(db)
    scope = HistoryScope("tg-7", "testnet")
    base_ms = int(clock().timestamp() * 1000) - 600_000
    for index in range(11):
        await events.record(
            scope,
            kind="POSITION_OPENED",
            occurred_at=base_ms + index,
            recorded_at=base_ms + index,
            facts={"symbol": "BTCUSDT", "side": "LONG", "entry_qty": "0.1"},
            dedup_key=f"evt:page:{index}",
            trade_ref="ref-1",
            notify_eligible=False,
        )
    first = await service.activity(7)
    second = await service.activity(7, page=1)
    assert first.has_next is True
    assert second.has_previous is True
    assert second.page == 1
    assert len(first.items) == 10
    assert len(second.items) == 1
    # The adapter reads confirmed user events: titles come from the shared
    # presenter and the status surface is always the completed fact.
    assert first.items[0].category.value == "trade"
    assert first.items[0].title.splitlines()[0] == "Открыта BTCUSDT · LONG"
    assert first.items[0].status.value == "completed"
    assert first.items[0].symbol == "BTCUSDT"
    with pytest.raises(ValidationError):
        await service.activity(7, page=-1)
    with pytest.raises(ValidationError):
        await service.activity(7, page=True)
    with pytest.raises(ValidationError):
        await service.activity(7, filter="invalid_filter")  # type: ignore[arg-type]

    disabled = await service.set_notifications(7, False)
    assert disabled.enabled is False
    assert (await service.notification_settings(7)).enabled is False
    with pytest.raises(ValidationError):
        await service.set_notifications(7, "false")  # type: ignore[arg-type]


async def test_activity_safe_mapping_filters_and_sorts(environment):
    """The activity view-model never leaks journal/event internals to the UI.

    The legacy adapter now reads the user-event feed: trade kinds map to the
    trade category, system kinds to system, titles come from the shared
    presenter, and ordering is ``occurred_at DESC`` (newest first).  The test
    then asserts:

    * all forbidden fields stay out of the DTO;
    * categories split cleanly into trade / system;
    * filtering by ``ActivityFilter`` returns exactly the expected subset;
    * ordering is newest first;
    * every confirmed event is a completed fact with no remediation copy.
    """
    from copytradeaster.application.models import ActivityCategory

    db, _, _, clock, service = environment
    events = EventRepository(db)
    scope = HistoryScope("tg-7", "testnet")
    now_ms = int(clock().timestamp() * 1000)
    base = now_ms - 1_800_000
    specs: list[tuple[str, str, dict[str, str], str | None]] = [
        (
            "trade-open",
            "POSITION_OPENED",
            {"symbol": "BTCUSDT", "side": "LONG", "entry_qty": "0.016", "entry_price": "62400"},
            "ref-open",
        ),
        (
            "trade-stop",
            "PROTECTION_SET",
            {"symbol": "BTCUSDT", "slot": "stop", "new_price": "61500"},
            "ref-open",
        ),
        (
            "trade-close",
            "POSITION_CLOSED",
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "entry_price": "2600",
                "exit_price": "2550",
                "realized_gross": "5.5",
            },
            "ref-close",
        ),
        (
            "sys-risk",
            "RISK_CHANGED",
            {"risk_percent": "0.02", "previous_percent": "0.01"},
            None,
        ),
        ("sys-pause", "COPY_PAUSED", {"symbol": "BTCUSDT"}, None),
        (
            "sys-skip",
            "ENTRY_SKIPPED",
            {"symbol": "SOLUSDT", "reason": "ENTRY_DEVIATION"},
            None,
        ),
    ]
    for position, (name, kind, facts, ref) in enumerate(specs):
        await events.record(
            scope,
            kind=kind,
            occurred_at=base + position * 60_000,
            recorded_at=base + position * 60_000,
            facts=facts,
            dedup_key=f"evt:safe:{name}",
            trade_ref=ref,
            notify_eligible=False,
        )

    # --- default (all) -------------------------------------------------------
    all_page = await service.activity(7)
    assert all_page.filter.value == "all"
    assert all_page.page == 0
    assert all_page.has_next is False
    titles = [item.title.splitlines()[0] for item in all_page.items]
    # Newest first by ``occurred_at``: the entry skip is newest, the open oldest.
    assert titles == [
        "Вход SOLUSDT пропущен: ENTRY_DEVIATION",
        "Копирование приостановлено. Открытые позиции и защита продолжают работать.",
        "Риск на сделку изменён: 1,0% → 2,0%",
        "Закрыта ETHUSDT · SHORT",
        "Стоп BTCUSDT установлен: 61 500",
        "Открыта BTCUSDT · LONG",
    ]

    # --- filter trade --------------------------------------------------------
    trade_page = await service.activity(7, filter="trade")
    trade_categories = {item.category for item in trade_page.items}
    assert trade_categories == {ActivityCategory.TRADE}
    assert all(item.symbol for item in trade_page.items)
    assert trade_page.filter.value == "trade"

    # --- filter system -------------------------------------------------------
    system_page = await service.activity(7, filter="system")
    assert {item.category for item in system_page.items} == {ActivityCategory.SYSTEM}

    # --- filter=str variants -------------------------------------------------
    by_string = await service.activity(7, filter="trade")
    assert [item.title for item in by_string.items] == [item.title for item in trade_page.items]

    # --- confirmed events only: no pending/failed remediation copy -----------
    for item in all_page.items:
        assert item.status.value == "completed"
        assert item.remediation is None

    # --- forbidden fields never appear on the DTO ----------------------------
    for item in all_page.items:
        for forbidden in (
            "ref-open",
            "ref-close",
            "evt:safe:",
            "dedup_key",
            "tg-7",
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
            "client_order_id",
            "nonce",
        ):
            assert forbidden not in (item.title or "")


def _as_iso_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def test_activity_pagination_boundary(environment):
    """Pagination is stable: pages slice the same feed cursor in order."""
    from copytradeaster.application.models import ActivityCategory

    db, _, _, clock, service = environment
    events = EventRepository(db)
    scope = HistoryScope("tg-7", "testnet")
    base_ms = int(clock().timestamp() * 1000) - 3_600_000
    for index in range(25):
        await events.record(
            scope,
            kind="POSITION_OPENED",
            occurred_at=base_ms + index,
            recorded_at=base_ms + index,
            facts={"symbol": "BTCUSDT", "side": "LONG", "entry_qty": "0.1"},
            dedup_key=f"evt:pg:{index:02d}",
            trade_ref="ref-1",
            notify_eligible=False,
        )

    page0 = await service.activity(7)
    page1 = await service.activity(7, page=1)
    page2 = await service.activity(7, page=2)
    page3 = await service.activity(7, page=3)

    assert page0.has_next is True
    assert page1.has_next is True
    assert page2.has_next is False
    assert page3.has_next is False
    assert page0.has_previous is False
    assert page1.has_previous is True
    assert page2.has_previous is True
    assert page3.has_previous is True

    assert len(page0.items) == 10
    assert len(page1.items) == 10
    assert len(page2.items) == 5
    assert page3.items == ()

    # Each item is a trade event with the expected safe title.
    seen_symbols = []
    for page in (page0, page1, page2):
        for item in page.items:
            assert item.category is ActivityCategory.TRADE
            assert item.status.value == "completed"
            assert item.symbol == "BTCUSDT"
            assert item.title.splitlines()[0] == "Открыта BTCUSDT · LONG"
            seen_symbols.append(item.symbol)
    assert seen_symbols == ["BTCUSDT"] * 25


async def test_activity_category_filter_paginates_the_filtered_set(environment):
    """The trade/system filter must run in SQL, before LIMIT.

    Regression for a P1 pagination bug, kept for the feed read path: the
    kind predicate is applied by the repository SQL *before* the LIMIT
    window.  If it were applied in Python after truncation:

    * a ``system`` page could come back **empty** with ``has_next=False``
      although matching rows existed further down the feed;
    * page N would offset the **general** feed, so rows were silently skipped.

    The feed here is laid out so every system row sits outside the first
    11 general rows: 14 system rows (older) followed by 14 trade rows
    (newest), each with a unique strictly-ordered ``occurred_at``.
    """
    from copytradeaster.application.models import ActivityCategory

    db, _, _, clock, service = environment
    events = EventRepository(db)
    scope = HistoryScope("tg-7", "testnet")
    base_ms = int(clock().timestamp() * 1000) - 7_200_000

    # Oldest -> newest: system rows first, then trade rows on top.
    specs: list[tuple[str, str]] = [(f"sys-{i:02d}", "ENTRY_SKIPPED") for i in range(14)]
    specs += [(f"trd-{i:02d}", "POSITION_OPENED") for i in range(14)]

    for position, (name, kind) in enumerate(specs):
        facts: dict[str, str] = {"symbol": "BTCUSDT"}
        trade_ref: str | None = "ref-1"
        if kind == "ENTRY_SKIPPED":
            # Distinct reasons keep every system title unique.
            facts["reason"] = f"ENTRY_DEVIATION_{position:02d}"
            trade_ref = None
        else:
            facts.update({"side": "LONG", "entry_qty": "0.1"})
        await events.record(
            scope,
            kind=kind,
            occurred_at=base_ms + position * 60_000,
            recorded_at=base_ms + position * 60_000,
            facts=facts,
            dedup_key=f"evt:cat:{name}",
            trade_ref=trade_ref,
            notify_eligible=False,
        )

    def identity(item: object) -> tuple[str, object]:
        return (item.title, item.occurred_at)  # type: ignore[attr-defined]

    async def collect(flt: str) -> list[object]:
        collected: list[object] = []
        page = 0
        while True:
            result = await service.activity(7, page=page, filter=flt)
            if flt == "trade":
                assert {i.category for i in result.items} <= {ActivityCategory.TRADE}
            elif flt == "system":
                assert {i.category for i in result.items} <= {ActivityCategory.SYSTEM}
            collected.extend(result.items)
            if not result.has_next:
                return collected
            page += 1
            assert page < 20, "pagination did not terminate"

    all_items = await collect("all")
    trade_items = await collect("trade")
    system_items = await collect("system")

    # --- the exact failure the old code produced ----------------------------
    # The first 11 general rows were all trade, so a Python-side filter left
    # the first system page empty and reported has_next=False.
    first_system = await service.activity(7, page=0, filter="system")
    assert len(first_system.items) == 10
    assert first_system.has_next is True
    second_system = await service.activity(7, page=1, filter="system")
    assert len(second_system.items) == 4
    assert second_system.has_next is False
    assert second_system.has_previous is True

    first_trade = await service.activity(7, page=0, filter="trade")
    assert len(first_trade.items) == 10
    assert first_trade.has_next is True
    second_trade = await service.activity(7, page=1, filter="trade")
    assert len(second_trade.items) == 4
    assert second_trade.has_next is False

    # --- completeness: no gaps, no duplicates -------------------------------
    assert len(all_items) == 28
    assert len(trade_items) == 14
    assert len(system_items) == 14

    all_keys = [identity(i) for i in all_items]
    trade_keys = {identity(i) for i in trade_items}
    system_keys = {identity(i) for i in system_items}
    assert len(set(all_keys)) == 28, "duplicate rows across pages"
    assert len(trade_keys) == 14, "duplicate trade rows across pages"
    assert len(system_keys) == 14, "duplicate system rows across pages"

    # trade + system is an exact partition of the unfiltered journal.
    assert trade_keys | system_keys == set(all_keys)
    assert not trade_keys & system_keys

    # --- category purity ----------------------------------------------------
    assert {i.category for i in trade_items} == {ActivityCategory.TRADE}
    assert {i.category for i in system_items} == {ActivityCategory.SYSTEM}
    assert all(i.title.splitlines()[0] == "Открыта BTCUSDT · LONG" for i in trade_items)
    assert all(i.title.startswith("Вход BTCUSDT пропущен") for i in system_items)

    # --- ordering holds across page boundaries ------------------------------
    for items in (all_items, trade_items, system_items):
        stamps = [i.occurred_at for i in items]  # type: ignore[attr-defined]
        assert stamps == sorted(stamps, reverse=True), "order broken across pages"


async def test_permission_refresh_failures_and_expiry_fail_closed(environment):
    db, runtime, _, _, service = environment
    await service.pause(7)
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)

    runtime.refresh_agent_permission = lambda account_id: AgentValidation(
        signer_address="",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at="2026-08-23T12:00:00+00:00",
    )
    await AgentValidationRepo(db).upsert(
        "tg-7",
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at="2026-08-23T11:00:00+00:00",
    )
    with pytest.raises(StaleOperationError):
        await service.confirm_action(7, confirmation)

    await AgentValidationRepo(db).upsert(
        "tg-7",
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=1,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at="2026-08-23T12:00:00+00:00",
    )
    runtime.refresh_agent_permission = lambda account_id: (_ for _ in ()).throw(
        ServiceUnavailableError("signed read unavailable")
    )
    next_confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(ServiceUnavailableError):
        await service.confirm_action(7, next_confirmation)


async def test_resume_runtime_failure_rolls_policy_back(environment):
    db, runtime, _, _, service = environment
    await service.pause(7)
    runtime.fail_resume = True
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(ServiceUnavailableError):
        await service.confirm_action(7, confirmation)
    policy = await SettingsRepo(db).get_policy("tg-7")
    assert policy is not None and policy.allow_new_entries is False


async def test_resume_rejects_account_disabled_or_runtime_removed_during_checks(environment):
    db, runtime, _, _, service = environment
    await service.pause(7)

    async def disable(account_id: str, snapshot):
        del snapshot
        await db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", (account_id,))

    runtime.validate_resume_safety = disable
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(AuthorizationError, match="disconnected"):
        await service.confirm_action(7, confirmation)

    await db.execute_write("UPDATE accounts SET enabled=1 WHERE id=?", ("tg-7",))
    runtime.follower_registry.account_ids.add("tg-7")

    async def remove_runtime(account_id: str, snapshot):
        del account_id, snapshot
        runtime.follower_registry.account_ids.clear()

    runtime.validate_resume_safety = remove_runtime
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(StaleOperationError, match="runtime changed"):
        await service.confirm_action(7, confirmation)


async def test_confirmation_shape_owner_action_and_token_errors(environment):
    _, _, _, _, service = environment
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(7, object())  # type: ignore[arg-type]
    malformed = type(confirmation)("", confirmation.action, confirmation.account_id)
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(7, malformed)
    oversized = type(confirmation)("x" * 17, confirmation.action, confirmation.account_id)
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(7, oversized)
    wrong_owner = await service.begin_confirmation(7, ConfirmationAction.DISCONNECT)
    with pytest.raises(AuthorizationError):
        await service.confirm_action(8, wrong_owner)
    wrong_scope = type(wrong_owner)(
        wrong_owner.token,
        wrong_owner.action,
        "tg-other",
        wrong_owner.expires_at,
    )
    with pytest.raises(AuthorizationError):
        await service.confirm_action(7, wrong_scope)


async def test_admin_reconcile_and_confirmation_required_paths(environment):
    db, runtime, _, _, service = environment
    dashboard = await service.admin_dashboard(100)
    assert dashboard.accounts_count == 1
    await service.admin_reconcile(100, "tg-7")
    assert ("reconcile_account", "tg-7") in runtime.actions
    await service.admin_reconcile(100)
    assert ("reconcile_all",) in runtime.actions
    with pytest.raises(ValidationError):
        await service.admin_reconcile(100, "not-follower")
    with pytest.raises(ConfirmationExpiredError):
        await service.admin_pause(100)
    with pytest.raises(ConfirmationExpiredError):
        await service.admin_resume(100)
    with pytest.raises(ConfirmationExpiredError):
        await service.admin_emergency(100)

    runtime.emergency_controller.success = False
    confirmation = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_EMERGENCY)
    with pytest.raises(ServiceUnavailableError):
        await service.admin_emergency(100, confirmation)
    assert await SettingsRepo(db).get_flag("allow_new_entries") == "false"


async def test_admin_user_and_follower_pages_are_bounded_and_idempotent(environment):
    db, runtime, _, _, service = environment
    users = TelegramUserRepo(db)
    for target in range(200, 212):
        await users.upsert(target, target)

    first = await service.admin_users(100)
    second = await service.admin_users(100, page=1)
    assert len(first.items) == 10 and first.has_next
    assert second.has_previous

    await service.admin_allow_user(100, 999)
    await service.admin_allow_user(100, 999)
    assert (await users.get(999))["role"] == "USER"
    await service.admin_set_user_access(100, 999, False)
    assert not (await service.authorize(999, 999, "private")).allowed
    assert (await users.get(999))["notifications_enabled"] == 1
    await service.admin_set_user_access(100, 999, True)
    assert (await service.authorize(999, 999, "private")).allowed
    with pytest.raises(ValidationError):
        await service.admin_set_user_access(100, 100, False)
    with pytest.raises(AuthorizationError):
        await service.admin_allow_user(7, 998)
    with pytest.raises(ValidationError):
        await service.admin_allow_user(100, True)  # type: ignore[arg-type]

    followers = await service.admin_followers(100)
    assert len(followers.items) == 1
    assert followers.items[0].account_id == "tg-7"
    assert followers.items[0].owner_user_id == 7
    assert followers.items[0].account_masked != (await AccountRepo(db).get("tg-7"))["user_address"]
    assert followers.items[0].copy_state is CopyState.PAUSED
    runtime.follower_registry.account_ids.clear()
    assert (await service.admin_followers(100)).items[0].copy_state is CopyState.DEGRADED


async def test_dashboard_read_rejects_broken_reverse_ownership(environment):
    _, _, _, _, service = environment

    class BrokenOwnershipUsers:
        async def get(self, user_id: int):
            return {
                "telegram_user_id": user_id,
                "chat_id": 700,
                "account_id": "tg-7",
                "role": "USER",
                "notifications_enabled": 1,
            }

        async def by_account(self, account_id: str):
            return {"telegram_user_id": 99, "account_id": account_id, "role": "USER"}

    service._users = BrokenOwnershipUsers()
    with pytest.raises(AuthorizationError):
        await service.dashboard(7)


async def test_admin_follower_list_paginates_thirty_accounts(environment):
    db, runtime, _, _, service = environment
    accounts = AccountRepo(db)
    users = TelegramUserRepo(db)
    settings = SettingsRepo(db)
    for index in range(29):
        user_id = 2000 + index
        account_id = f"tg-{user_id}"
        await accounts.upsert(
            account_id,
            "follower",
            account_id,
            f"0x{user_id:040x}",
        )
        await users.upsert(user_id, user_id, account_id=account_id)
        await settings.upsert_policy(
            RiskPolicy(Decimal("0.01"), 3, Decimal("0.02")),
            account_id,
        )
        runtime.follower_registry.account_ids.add(account_id)

    pages = [await service.admin_followers(100, page=page) for page in range(3)]
    assert [len(page.items) for page in pages] == [10, 10, 10]
    assert not pages[0].has_previous and pages[0].has_next
    assert pages[1].has_previous and pages[1].has_next
    assert pages[2].has_previous and not pages[2].has_next


async def test_service_constructor_admin_parser_and_testnet_guards(environment):
    db, runtime, _, _, _ = environment
    with pytest.raises(ValueError):
        UserApplicationService(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        UserApplicationService(runtime, confirmation_ttl_seconds=0)
    with pytest.raises(ValueError):
        UserApplicationService(runtime, permission_ttl_seconds=0)
    with pytest.raises(ValueError):
        UserApplicationService(runtime, activity_page_size=101)
    with pytest.raises(ValueError):
        UserApplicationService(runtime, admin_ids=[True])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        UserApplicationService(runtime, admin_ids="[100, nope]")
    assert UserApplicationService._parse_admin_ids("[100, 100, 101]", runtime) == [100, 101]

    guarded = UserApplicationService(runtime, testnet_check=False)
    with pytest.raises(ServiceUnavailableError):
        await guarded.connect_direct(7, DirectCredentialsInput("u", "s", "k"))

    runtime.cfg = SimpleNamespace(
        rest_base_url="https://not-testnet.example",
        ws_user_base="wss://not-testnet.example/ws",
    )
    with pytest.raises(ServiceUnavailableError):
        await UserApplicationService(runtime).pause(7)
    del runtime.cfg
    runtime.testnet = False
    with pytest.raises(ServiceUnavailableError):
        await UserApplicationService(runtime).pause(7)
    del db


async def test_authorize_database_error_and_context_ownership_fail_closed(environment):
    db, runtime, _, _, service = environment
    original_get = service._users.get

    async def broken_get(user_id: int):
        del user_id
        raise RuntimeError("database down")

    service._users.get = broken_get
    with pytest.raises(ServiceUnavailableError, match="authorization backend unavailable") as exc:
        await service.authorize(7, 700, "private")
    assert "database down" not in str(exc.value)
    service._users.get = original_get

    with pytest.raises(AuthorizationError):
        await service.dashboard(True)  # type: ignore[arg-type]
    with pytest.raises(AuthorizationError):
        await service.admin_dashboard(7)

    await db.execute_write("UPDATE accounts SET execution_mode='SHADOW' WHERE id=?", ("tg-7",))
    with pytest.raises(ServiceUnavailableError, match="REAL"):
        await service.risk(7)
    await db.execute_write("UPDATE accounts SET execution_mode='REAL' WHERE id=?", ("tg-7",))


async def test_system_ready_paths_and_normalized_datetime_shapes(environment):
    db, runtime, _, _, service = environment
    await db.execute_write("DELETE FROM app_state WHERE key IN ('ready', 'heartbeat_at')")
    ready = await service.dashboard(7)
    assert ready.system_state.value == "ready"

    await SettingsRepo(db).set_flag("ready", "true")
    await SettingsRepo(db).set_flag("heartbeat_at", str(int(datetime.now(UTC).timestamp() * 1000)))
    assert (await service.dashboard(7)).system_state.value == "ready"

    class BrokenSettings:
        async def get_flag(self, key: str):
            del key
            raise RuntimeError("flag store down")

    broken = UserApplicationService(runtime, settings=BrokenSettings())
    assert (await broken._system_state()).value == "unknown"

    shaped_snapshot = {
        "positions": [
            {"qty": "1"},
            {"qty": "bad"},
            {"position_amt": "0"},
        ]
    }
    runtime.snapshot_follower = lambda account_id: shaped_snapshot
    runtime.refresh_snapshot = lambda account_id: shaped_snapshot
    status = await service.status(7, refresh=True)
    assert status.positions_count == 2


async def test_dashboard_policy_emergency_and_unconnected_admin_views(environment):
    db, _, _, _, service = environment
    admin = await service.dashboard(100)
    assert admin.message == "account is not connected"
    await SettingsRepo(db).patch_policy("tg-7", enabled=False, allow_new_entries=True)
    blocked = await service.dashboard(7)
    assert blocked.copy_state is CopyState.BLOCKED
    await SettingsRepo(db).patch_policy("tg-7", enabled=True, allow_new_entries=True)
    await SettingsRepo(db).set_flag("emergency_active", "true")
    emergency = await service.dashboard(7)
    assert emergency.copy_state is CopyState.PAUSED
    assert emergency.message == "global emergency is active"


async def test_permission_summary_missing_pending_and_refresh_validation(environment):
    db, runtime, _, _, service = environment
    await db.execute_write("DELETE FROM agent_validations WHERE account_id=?", ("tg-7",))
    unavailable = await service.dashboard(7)
    assert unavailable.permission.warning == "permission_check_unavailable"

    await AgentValidationRepo(db).upsert(
        "tg-7",
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=False,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="",
        status="PENDING",
        checked_at="not-a-date",
    )
    pending = await service.dashboard(7)
    assert pending.permission.warning == "permission_check_pending"
    assert pending.permission.whitelist_ok is None

    runtime.refresh_agent_permission = lambda account_id: AgentValidation(
        signer_address="0x2222222222222222222222222222222222222222",
        can_read=False,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at="2026-08-23T12:00:00+00:00",
    )
    # A proven signed rejection never surfaces the old valid cache as fresh:
    # System Status degrades to a typed blocked DTO instead of raising.
    blocked = await service.status(7, refresh=True)
    assert blocked.state is StatusState.BLOCKED
    assert blocked.permissions_state is PermissionsState.BLOCKED
    assert blocked.reason is StatusReason.PERMISSION_INVALID
    assert blocked.remediation is StatusRemediation.OPEN_BOT


async def test_risk_success_updates_each_field_and_invalid_storage_is_typed(environment):
    _, _, _, _, service = environment
    with pytest.raises(ValidationError):
        await service.update_risk(
            7,
            RiskUpdateInput(
                risk_percent=Decimal("0.02"),
                max_leverage=5,
                max_entry_deviation_pct=Decimal("0.03"),
            ),
        )
    result = await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02")))
    assert result.risk_percent == Decimal("0.02")
    assert result.min == Decimal("0.005")
    assert result.max == Decimal("0.05")
    assert result.step == Decimal("0.005")
    assert result.presets == (
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.03"),
        Decimal("0.05"),
    )
    assert result.risk_base == "available_balance"
    assert not hasattr(result, "max_leverage")
    assert not hasattr(result, "max_entry_deviation_pct")
    # The save is durable: the next read sees the new value.
    assert (await service.risk(7)).risk_percent == Decimal("0.02")

    class InvalidSettings:
        async def get_policy(self, account_id: str):
            del account_id
            return SimpleNamespace(
                risk_percent="not-decimal",
                max_entry_deviation_pct="0.1",
                max_leverage=3,
            )

    invalid = UserApplicationService(service._runtime, settings=InvalidSettings())
    with pytest.raises(ValidationError):
        await invalid.risk(7)


async def test_onboarding_result_validation_and_notification_failures(environment):
    db, _, onboarding, _, service = environment
    await TelegramUserRepo(db).upsert(8, 800, role="USER")
    original_onboard = onboarding.onboard

    async def return_unpaused(request):
        result = await original_onboard(request)
        return type(result)(
            account_id=result.account_id,
            masked_user_address=result.masked_user_address,
            masked_signer_address=result.masked_signer_address,
            position_mode=result.position_mode,
            balance_assets=result.balance_assets,
            position_count=result.position_count,
            available_usdt=result.available_usdt,
            paused=False,
        )

    onboarding.onboard = return_unpaused
    with pytest.raises(ValidationError):
        await service.connect_direct(
            8,
            DirectCredentialsInput("0x3333333333333333333333333333333333333333", "s", "k"),
        )
    onboarding.onboard = original_onboard

    class BrokenOutbox:
        async def counts_for_user(self, user_id: int):
            del user_id
            raise RuntimeError("outbox down")

    broken = UserApplicationService(service._runtime, outbox=BrokenOutbox())
    with pytest.raises(ServiceUnavailableError):
        await broken.notification_settings(7)
    await db.execute_write(
        "UPDATE telegram_users SET notifications_enabled=1 WHERE telegram_user_id=7"
    )


async def test_confirm_account_version_unavailable_disconnect_and_emergency_failures(environment):
    db, runtime, _, _, service = environment
    confirmation = await service.begin_confirmation(7, ConfirmationAction.DISCONNECT)

    async def unavailable_version(account_id: str):
        del account_id
        return None

    runtime.account_version = unavailable_version
    with pytest.raises(ConfirmationExpiredError, match="version"):
        await service.confirm_action(7, confirmation)

    runtime.account_version = lambda account_id: "credentials-v1"
    runtime.commit_disconnect = lambda account_id: False
    disconnect = await service.begin_confirmation(7, ConfirmationAction.DISCONNECT)
    with pytest.raises(ServiceUnavailableError):
        await service.confirm_action(7, disconnect)
    account = await AccountRepo(db).get("tg-7")
    assert account is not None and int(account["enabled"]) == 1


async def test_global_resume_rolls_back_flag_and_confirmation_unknown_action(environment):
    db, runtime, _, _, service = environment
    original_resume = runtime.resume_entries

    async def broken_resume():
        raise RuntimeError("admission unavailable")

    runtime.resume_entries = broken_resume
    pause = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_PAUSE)
    await service.admin_pause(100, pause)
    resume = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_RESUME)
    with pytest.raises(ServiceUnavailableError):
        await service.admin_resume(100, resume)
    assert await SettingsRepo(db).get_flag("allow_new_entries") == "false"
    runtime.resume_entries = original_resume

    confirmation = await service.begin_confirmation(100, ConfirmationAction.GLOBAL_PAUSE)
    forged = type(confirmation)(confirmation.token, "not-action", None, confirmation.expires_at)
    with pytest.raises(ConfirmationExpiredError):
        await service.confirm_action(100, forged)


async def test_miniapp_session_unexpected_log_omits_exception_payload(
    environment, caplog, monkeypatch
):
    _, _, _, _, service = environment
    secret = "remote-response-secret-must-not-be-logged"

    async def broken_account_row(_account_id: str):
        raise RuntimeError(secret)

    monkeypatch.setattr(service, "_account_row", broken_account_row)
    with caplog.at_level("ERROR"), pytest.raises(ServiceUnavailableError):
        await service.miniapp_session(7)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "miniapp_session failed class=RuntimeError" in messages
    assert secret not in messages


def test_service_public_surface_matches_application_protocol():
    protocol_methods = {
        name
        for name, method in inspect.getmembers(ApplicationProtocol, inspect.isfunction)
        if not name.startswith("_")
    }
    service_methods = {
        name
        for name, method in inspect.getmembers(UserApplicationService, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert service_methods == protocol_methods
    assert not hasattr(UserApplicationService, "emergency")
    assert not hasattr(UserApplicationService, "start_emergency")
    assert not hasattr(UserApplicationService, "disconnect")


@pytest.mark.parametrize(
    "raw",
    [
        Decimal("0.004"),  # below min
        Decimal("0.055"),  # above max
        Decimal("0.06"),  # above max
        Decimal("0.012"),  # off-step
        Decimal("0.007"),  # off-step
        Decimal("0.011"),  # off-step
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
async def test_risk_p24_rejects_out_of_policy_values(environment, raw):
    _, _, _, _, service = environment
    with pytest.raises(ValidationError):
        await service.update_risk(7, RiskUpdateInput(risk_percent=raw))


@pytest.mark.parametrize("raw", [0.02, True, False, None, [0.02], {"v": 1}])
async def test_risk_p24_rejects_non_decimal_inputs(environment, raw):
    _, _, _, _, service = environment
    with pytest.raises((ValidationError, ServiceUnavailableError)):
        await service.update_risk(7, RiskUpdateInput(risk_percent=raw))  # type: ignore[arg-type]


async def test_risk_p24_policy_round_trip_and_audit(environment, caplog):
    db, _, _, _, service = environment
    # Public DTO carries only the safe contract with backend-owned constraints.
    current = await service.risk(7)
    assert current.risk_percent == Decimal("0.01")
    assert current.min == Decimal("0.005")
    assert current.max == Decimal("0.05")
    assert current.step == Decimal("0.005")
    assert current.presets == (
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.03"),
        Decimal("0.05"),
    )
    assert current.risk_base == "available_balance"
    assert not hasattr(current, "max_leverage")
    assert not hasattr(current, "max_entry_deviation_pct")

    # Forged legacy fields stay fail-closed.
    with pytest.raises(ValidationError):
        await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02"), max_leverage=10))
    with pytest.raises(ValidationError):
        await service.update_risk(
            7,
            RiskUpdateInput(
                risk_percent=Decimal("0.02"),
                max_entry_deviation_pct=Decimal("0.01"),
            ),
        )

    # Grid edges and a mid-grid step are accepted (exact Decimal checks).
    for raw in ("0.005", "0.05", "0.015", "0.03"):
        updated = await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal(raw)))
        assert updated.risk_percent == Decimal(raw)
        assert (await service.risk(7)).risk_percent == Decimal(raw)

    # Internal engine semantics are untouched: legacy storage columns keep
    # serving the trading core with their pre-existing values.
    row = await db.fetchone("SELECT * FROM copy_settings WHERE account_id=?", ("tg-7",))
    assert row is not None
    assert Decimal(row["risk_percent"]) == Decimal("0.03")
    assert int(row["max_leverage"]) == 3
    assert Decimal(row["max_entry_deviation_pct"]) == Decimal("0.02")

    # Control audit keeps attempt/success semantics for the new policy.
    caplog.clear()
    with caplog.at_level("INFO"):
        await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02")))
    messages = [record.getMessage() for record in caplog.records]
    assert any("control_audit action=update_risk result=attempt" in message for message in messages)
    success = [
        message
        for message in messages
        if "control_audit action=update_risk result=success" in message
    ]
    assert len(success) == 1
    assert "changed_fields=risk_percent" in success[0]


async def test_pause_records_system_event_in_feed(environment):
    _, _, _, _, service = environment
    await service.pause(7)
    feed = await service.activity_feed(7)
    assert "COPY_PAUSED" in [item.kind for item in feed.items]


async def test_resume_records_system_event_in_feed(environment):
    _, _, _, _, service = environment
    await service.pause(7)
    confirmation = await service.begin_confirmation(7, ConfirmationAction.RESUME)
    await service.resume(7, confirmation=confirmation)
    kinds = [item.kind for item in (await service.activity_feed(7)).items]
    assert "COPY_PAUSED" in kinds and "COPY_RESUMED" in kinds


async def test_update_risk_records_percent_change_in_feed(environment):
    from copytradeaster.application.models import RiskUpdateInput

    _, _, _, _, service = environment
    await service.update_risk(7, RiskUpdateInput(risk_percent=Decimal("0.02")))
    (item,) = [
        item for item in (await service.activity_feed(7)).items if item.kind == "RISK_CHANGED"
    ]
    assert item.facts["risk_percent"] == "0.02"
    assert item.facts["previous_percent"] == "0.01"
