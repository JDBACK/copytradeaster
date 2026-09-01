from __future__ import annotations

import inspect
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
    AuthorizationError,
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    CopyState,
    DirectCredentialsInput,
    OnboardingApplicationError,
    RiskUpdateInput,
    ServiceUnavailableError,
    StaleOperationError,
    UserRole,
    ValidationError,
)
from copytradeaster.application.service import UserApplicationService
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import DirectOnboardingResult
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    CommandRepo,
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
    commands = CommandRepo(db)
    await commands.insert(
        "command-1",
        "tg-7",
        None,
        "MARKET_OPEN",
        '{"private_key":"never-display"}',
        1,
        "client-1",
    )
    await commands.set_status("command-1", "CONFIRMED", last_error="secret-error")
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
    assert status.aster_connected
    assert any(action[0] == "refresh_snapshot" for action in runtime.actions)

    risk = await service.risk(7)
    assert risk.max_leverage == 3
    activity = await service.activity(7)
    assert activity.items[0].action == "MARKET_OPEN"
    assert activity.items[0].detail is None

    notifications = await service.notification_settings(7)
    assert notifications.pending == 1
    assert notifications.last_delivery_at is None


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
    result = await service.update_risk(7, RiskUpdateInput())
    assert result.max_leverage == 3
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
    assert disconnected.message == "account is disconnected; connect credentials to continue"

    await db.execute_write("UPDATE accounts SET enabled=1 WHERE id=?", ("tg-7",))
    await SettingsRepo(db).set_flag("degraded_active", "false")

    async def broken_refresh(account_id: str):
        del account_id
        raise RuntimeError("exchange down")

    runtime.refresh_snapshot = broken_refresh
    refreshed = await service.status(7, refresh=True)
    assert refreshed.message == "Aster status is temporarily unavailable"


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
    db, _, _, _, service = environment
    commands = CommandRepo(db)
    for index in range(11):
        await commands.insert(
            f"page-{index}",
            "tg-7",
            None,
            "KIND!" if index == 10 else "MARKET_OPEN",
            "{}",
            1,
            f"ct-{index}",
        )
    first = await service.activity(7)
    second = await service.activity(7, page=1)
    assert first.has_next is True
    assert second.has_previous is True
    assert second.page == 1
    assert first.items[0].action == "KIND"
    with pytest.raises(ValidationError):
        await service.activity(7, page=-1)
    with pytest.raises(ValidationError):
        await service.activity(7, page=True)

    disabled = await service.set_notifications(7, False)
    assert disabled.enabled is False
    assert (await service.notification_settings(7)).enabled is False
    with pytest.raises(ValidationError):
        await service.set_notifications(7, "false")  # type: ignore[arg-type]


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
    with pytest.raises(StaleOperationError):
        await service.status(7, refresh=True)


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
    assert result.max_leverage == 3
    assert result.max_entry_deviation_pct == Decimal("0.02")

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
