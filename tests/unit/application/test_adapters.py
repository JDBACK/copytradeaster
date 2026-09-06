from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from pydantic import SecretStr

from copytradeaster.application.adapters import (
    AppEmergencyStateAdapter,
    AppStage2RuntimeAdapter,
    DirectOnboardingAdapter,
    SqliteOnboardingStore,
    Stage2Composition,
    build_stage2_composition,
)
from copytradeaster.application.control import EmergencyReport, EmergencyScope
from copytradeaster.aster.models import AgentDTO, PositionDTO
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import (
    AccountState,
    PositionKey,
    PositionState,
    PosSide,
    RiskPolicy,
)
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    AgentValidationSnapshot,
    DirectOnboardingRequest,
    DirectPreflightResult,
    OnboardingCommit,
    SignedReadBundle,
    stable_account_id,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import AccountRepo, SettingsRepo, TelegramUserRepo
from copytradeaster.trading.leverage import LeverageError
from tests.unit.aster.conftest import KEY, SIGNER, USER

NEW_KEY = "0x" + "55" * 32


class _Registry:
    def __init__(self, runtimes: dict[str, object] | None = None) -> None:
        self.runtimes = runtimes or {}

    def get(self, account_id: str) -> object | None:
        return self.runtimes.get(account_id)


class _Coordinator:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_reconcile = False
        self.admission_open = True
        self.entry_skipped_hook = None

    def set_entry_skipped_hook(self, hook) -> None:
        self.entry_skipped_hook = hook

    async def close_generation_admission(self) -> None:
        self.events.append("close")
        self.admission_open = False

    async def open_generation_admission(self) -> None:
        self.events.append("open")
        self.admission_open = True


class _Reconciler:
    async def reconcile(self, state, *, reason: str) -> object:
        return SimpleNamespace(state=state, reason=reason)


class _PermissionClient:
    def __init__(self, agent: AgentDTO) -> None:
        self.agent_record = agent

    async def agent(self, account_id: str) -> AgentDTO:
        assert account_id == "tg-7"
        return self.agent_record


class _App:
    def __init__(
        self,
        db: Database,
        cfg: Settings,
        credentials: SqliteCredentials,
        *,
        runtime: object | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.credentials = credentials
        self.follower_registry = _Registry({"tg-7": runtime} if runtime is not None else {})
        self.coordinator = _Coordinator()
        self.reconciler = _Reconciler()
        self.projector = SimpleNamespace(state=SimpleNamespace(positions={}))
        self.settings = SettingsRepo(db)
        self.executor = object()
        self.http = object()
        self.limiter = object()
        self.nonces = object()
        self.ready = True
        self.snapshot_calls = 0
        self.snapshot_value = AccountState("tg-7", 100, {}, [])
        self.reconcile_calls: list[tuple[str, str]] = []
        self.new_runtime_calls: list[str] = []

    async def register_follower(self, account_id: str) -> object:
        runtime = SimpleNamespace(account_id=account_id)
        self.follower_registry.runtimes[account_id] = runtime
        return runtime

    async def snapshot_follower(self, account_id: str) -> AccountState:
        self.snapshot_calls += 1
        assert account_id == "tg-7"
        return self.snapshot_value

    async def reconcile_follower(self, account_id: str, *, reason: str) -> None:
        assert self.coordinator.admission_open is True
        self.coordinator.events.append("reconcile")
        self.reconcile_calls.append((account_id, reason))
        if self.coordinator.fail_reconcile:
            raise RuntimeError("reconcile failed")

    async def pause_entries(self, *, timeout_s: float) -> None:
        self.coordinator.events.append(f"pause_entries:{timeout_s}")

    async def resume_entries(self) -> None:
        self.coordinator.events.append("resume_entries")

    async def reopen_entries_while_paused(self) -> None:
        self.coordinator.events.append("reopen_entries_while_paused")

    def follower_lifecycle(self, account_id: str):
        del account_id

        @asynccontextmanager
        async def lifecycle():
            yield object()

        return lifecycle()

    def new_real_follower_runtime(self, account_id: str) -> object:
        self.new_runtime_calls.append(account_id)
        return SimpleNamespace(account_id=account_id)


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "encryption_key": SecretStr("11" * 32),
        "aster_egress_ips": ["127.0.0.1/32"],
        "dry_run": True,
    }
    values.update(changes)
    return Settings(**values)


@pytest.fixture
async def database(tmp_path: Path):
    db = Database(tmp_path / "adapters.sqlite3")
    await db.connect()
    await db.migrate()
    yield db
    await db.close()


def _state(qty: str = "1", *, client_order_id: str = "ct-managed") -> AccountState:
    key = PositionKey("BTCUSDT", PosSide.BOTH)
    position = PositionState(
        key=key,
        qty=Decimal(qty),
        entry_price=100,
        leverage=3,
        mark_price=100,
        liquidation_price=0,
    )
    return AccountState("tg-7", 100, {key: position}, [{"client_order_id": client_order_id}])


def _snapshot(signer: str = SIGNER) -> DirectPreflightResult:
    validation = AgentValidationSnapshot(
        signer_address=signer,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        checked_at_ms=1_700_000_000_000,
    )
    return DirectPreflightResult(
        masked_user_address="0xaaaa…aaaa",
        masked_signer_address="0xbbbb…bbbb",
        validation=validation,
        read_bundle=SignedReadBundle((), (), (), False),
    )


def _nonflat_snapshot(signer: str = SIGNER) -> DirectPreflightResult:
    position = PositionDTO.model_validate(
        {
            "symbol": "BTCUSDT",
            "positionAmt": "1",
            "entryPrice": "100",
            "markPrice": "100",
            "leverage": 3,
            "liquidationPrice": "0",
            "marginType": "CROSSED",
            "positionSide": "BOTH",
            "unRealizedProfit": "0",
        }
    )
    base = _snapshot(signer)
    return DirectPreflightResult(
        masked_user_address=base.masked_user_address,
        masked_signer_address=base.masked_signer_address,
        validation=base.validation,
        read_bundle=SignedReadBundle((), (position,), (), False),
    )


async def test_runtime_cache_and_pause_reopens_admission_on_failure(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    adapter = AppStage2RuntimeAdapter(app)

    assert await adapter.cached_snapshot("tg-7") is None
    assert app.snapshot_calls == 0
    refreshed = await adapter.refresh_snapshot("tg-7")
    cached = await adapter.cached_snapshot("tg-7")
    assert cached == refreshed
    assert app.snapshot_calls == 1

    await adapter.pause_account("tg-7")
    assert app.coordinator.events == ["close", "open", "reconcile"]
    assert app.reconcile_calls == [("tg-7", "account_pause")]

    app.coordinator.fail_reconcile = True
    with pytest.raises(RuntimeError, match="reconcile failed"):
        await adapter.pause_account("tg-7")
    assert app.coordinator.events[-3:] == ["close", "open", "reconcile"]
    assert app.coordinator.admission_open is True

    await adapter.reopen_entries_while_paused()
    assert app.coordinator.events[-1] == "reopen_entries_while_paused"


async def test_runtime_activity_reconcile_and_master_state(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    adapter = AppStage2RuntimeAdapter(app)
    await database.execute_write(
        "INSERT INTO execution_commands(command_key, account_id, kind, payload, priority, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "activity-1",
            "tg-7",
            "MARKET_CLOSE",
            "{}",
            1,
            "CONFIRMED",
            "2026-08-24T00:00:00+00:00",
            "2026-08-24T00:00:00+00:00",
        ),
    )
    rows = await adapter.read_activity("tg-7", 10, 0)
    assert rows[0]["command_key"] == "activity-1"
    await adapter.reconcile_account("tg-7")
    await adapter.reconcile_all()
    await adapter.resume_account("tg-7")
    assert app.reconcile_calls == [("tg-7", "application")]
    assert app.coordinator.events[-1] == "open"

    app.projector.state.positions = {
        PositionKey("BTCUSDT"): PositionState(
            key=PositionKey("BTCUSDT"),
            qty=Decimal("0.1"),
            entry_price=100,
            leverage=3,
            mark_price=100,
            liquidation_price=0,
        )
    }
    assert await adapter.master_has_open_positions() is True
    app.projector.state.positions.clear()
    assert await adapter.master_has_open_positions() is False


async def test_runtime_account_version_and_recovery_require_paused_matching_identity(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    await credentials.import_identity("tg-7", USER, KEY, "follower", "tg-7")
    await SettingsRepo(database).upsert_policy(
        RiskPolicy(
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=Decimal("0.01"),
            allow_new_entries=False,
        ),
        "tg-7",
    )
    app = _App(database, _settings(), credentials)
    adapter = AppStage2RuntimeAdapter(app)
    version = await adapter.account_version("tg-7")
    assert version is not None and SIGNER not in version
    assert await adapter.account_version("missing") is None
    assert await adapter.recover_account_runtime("tg-7", USER, SIGNER) is True
    assert await adapter.is_account_registered("tg-7") is True

    app.follower_registry = _Registry()
    assert await adapter.recover_account_runtime("tg-7", "wrong", SIGNER) is False
    credentials.install_cache("missing", USER, KEY, "follower")
    assert await adapter.recover_account_runtime("missing", USER, SIGNER) is False


async def test_runtime_rejects_invalid_resume_snapshot_and_permission_errors(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    credentials.install_cache("tg-7", USER, KEY, "follower")
    app = _App(database, _settings(), credentials)
    adapter = AppStage2RuntimeAdapter(app)
    with pytest.raises(ValueError, match="snapshot is invalid"):
        await adapter.validate_resume_safety("tg-7", object())

    app.follower_registry = _Registry({"tg-7": SimpleNamespace(client=object())})
    with pytest.raises(AttributeError):
        await adapter.refresh_agent_permission("tg-7")


async def test_direct_adapter_replace_rejects_bad_preflight_and_wraps_commit_failure(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    await credentials.import_identity("tg-7", USER, KEY, "follower", "tg-7")
    users = TelegramUserRepo(database)
    await users.upsert(7, 700, account_id="tg-7")
    store = SqliteOnboardingStore(database, credentials, users)
    app = _App(database, _settings(), credentials)
    adapter = DirectOnboardingAdapter(app, store)
    request = DirectOnboardingRequest(7, USER, SIGNER, KEY)

    with pytest.raises(OnboardingError) as exc_info:
        await adapter.replace(request, preflight=object())
    assert exc_info.value.code is OnboardingErrorCode.INVALID_INPUT

    with pytest.raises(OnboardingError) as exc_info:
        await adapter.replace(request, preflight=_nonflat_snapshot())
    assert exc_info.value.code is OnboardingErrorCode.ACCOUNT_NOT_FLAT

    async def broken_replace(**kwargs: object) -> None:
        del kwargs
        raise RuntimeError("storage failure")

    store.replace_identity = broken_replace  # type: ignore[method-assign]
    with pytest.raises(OnboardingError) as exc_info:
        await adapter.replace(request, preflight=_snapshot())
    assert exc_info.value.code is OnboardingErrorCode.COMMIT_FAILED


async def test_resume_safety_uses_current_issued_target_not_historical_commands(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    adapter = AppStage2RuntimeAdapter(app)
    await database.execute_write(
        "INSERT INTO sync_basis(account_id, symbol, pos_side, master_ref_abs, revision, "
        "issued_target, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "tg-7",
            "BTCUSDT",
            "BOTH",
            "1",
            1,
            json.dumps({"qty": "2", "stop_price": None}),
            "2026-08-24T00:00:00+00:00",
        ),
    )
    await database.execute_write(
        "INSERT INTO execution_commands(command_key, account_id, kind, payload, priority, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old-open",
            "tg-7",
            "MARKET_OPEN",
            '{"symbol":"BTCUSDT","quantity":"1"}',
            1,
            "CONFIRMED",
            "2026-08-23T00:00:00+00:00",
            "2026-08-23T00:00:00+00:00",
        ),
    )

    await adapter.validate_resume_safety("tg-7", _state())
    with pytest.raises(RuntimeError, match="exceeds issued target"):
        await adapter.validate_resume_safety("tg-7", _state("3"))
    with pytest.raises(RuntimeError, match="exceeds issued target"):
        await adapter.validate_resume_safety("tg-7", _state("-1"))
    with pytest.raises(RuntimeError, match="manual open order"):
        await adapter.validate_resume_safety("tg-7", _state(client_order_id="manual-order"))

    await database.execute_write(
        "UPDATE sync_basis SET issued_target=? WHERE account_id=?",
        ("not-json", "tg-7"),
    )
    with pytest.raises(RuntimeError, match="issued target is invalid"):
        await adapter.validate_resume_safety("tg-7", _state())


async def test_permission_refresh_has_explicit_testnet_override(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    credentials.install_cache("tg-7", USER, KEY, "follower")
    agent = AgentDTO.model_validate(
        {
            "agentAddress": SIGNER,
            "agentName": "copytradeaster",
            "expired": 2_000_000_000_000,
            "ipWhitelist": "198.51.100.7/32",
            "canRead": True,
            "canPerpTrade": True,
            "canWithdraw": False,
        }
    )
    app = _App(
        database,
        _settings(testnet_ip_whitelist_override=True),
        credentials,
        runtime=SimpleNamespace(client=_PermissionClient(agent)),
    )
    adapter = AppStage2RuntimeAdapter(app)
    record = await adapter.refresh_agent_permission("tg-7")
    assert record.status == "VALID_OVERRIDE"
    assert await adapter.permission_override_allowed("tg-7") is True

    app.follower_registry = _Registry()
    assert await adapter.permission_override_allowed("tg-7") is False


async def test_store_requires_allowlisted_user_and_preserves_chat(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    users = TelegramUserRepo(database)
    store = SqliteOnboardingStore(database, credentials, users)
    payload = OnboardingCommit(
        account_id=stable_account_id(7),
        telegram_user_id=7,
        user_address=USER,
        signer_address=SIGNER,
        signer_private_key=KEY,
        validation=_snapshot().validation,
        read_bundle=SignedReadBundle((), (), (), False),
    )

    with pytest.raises(ValueError, match="pre-allowlisted"):
        await store.commit_onboarding(payload)

    await users.upsert(7, 700, role="USER")
    await store.commit_onboarding(payload)
    row = await users.get(7)
    assert row["chat_id"] == 700
    assert row["account_id"] == "tg-7"

    app = _App(database, _settings(), credentials)
    runtime = AppStage2RuntimeAdapter(app)
    assert await runtime.account_version("tg-7")
    assert await runtime.recover_account_runtime("tg-7", USER, SIGNER) is True
    assert await runtime.is_account_registered("tg-7") is True
    assert await runtime.recover_account_runtime("tg-7", USER, SIGNER) is False

    await users.upsert(8, 800, role="ADMIN")
    admin_payload = OnboardingCommit(
        account_id=stable_account_id(8),
        telegram_user_id=8,
        user_address="0x" + "bb" * 20,
        signer_address=SIGNER,
        signer_private_key=KEY,
        validation=_snapshot().validation,
        read_bundle=SignedReadBundle((), (), (), False),
    )
    await store.commit_onboarding(admin_payload)
    admin_row = await users.get(8)
    assert admin_row["role"] == "ADMIN"
    assert admin_row["account_id"] == "tg-8"


async def test_store_only_allows_disabled_address_for_same_stable_owner(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    users = TelegramUserRepo(database)
    store = SqliteOnboardingStore(database, credentials, users)
    old_address = "0x" + "cc" * 20
    await AccountRepo(database).upsert("other", "follower", "other", old_address)
    await database.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("other",))
    await users.upsert(9, 900, role="USER")
    assert await store.find_identity_conflict(9, old_address) == "address"

    stable_address = "0x" + "dd" * 20
    await AccountRepo(database).upsert("tg-10", "follower", "tg-10", stable_address)
    await database.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-10",))
    await users.upsert(10, 1000, account_id="tg-10")
    assert await store.find_identity_conflict(10, stable_address) is None


async def test_replace_uses_atomic_credentials_and_public_runtime_seam(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    await credentials.import_identity("tg-7", USER, KEY, "follower", "tg-7")
    users = TelegramUserRepo(database)
    await users.upsert(7, 700, account_id="tg-7")
    store = SqliteOnboardingStore(database, credentials, users)
    app = _App(database, _settings(), credentials)
    adapter = DirectOnboardingAdapter(app, store)
    new_signer = Account.from_key(NEW_KEY).address
    request = DirectOnboardingRequest(7, USER, new_signer, NEW_KEY)

    result = await adapter.replace(request, preflight=_snapshot(new_signer))
    assert result.account_id == "tg-7"
    assert result.paused is True
    assert credentials.private_key("tg-7") == NEW_KEY

    runtime_adapter = AppStage2RuntimeAdapter(app)
    candidate = await runtime_adapter.build_replacement_runtime("tg-7", request, result)
    assert candidate.account_id == "tg-7"
    assert app.new_runtime_calls == ["tg-7"]


async def test_emergency_state_and_composition_are_concrete(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(telegram_admin_ids=[99]), credentials)
    await AccountRepo(database).upsert("tg-7", "follower", "tg-7", USER)
    await SettingsRepo(database).upsert_policy(
        RiskPolicy(
            risk_percent=Decimal("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=Decimal("0.01"),
        ),
        "tg-7",
    )
    state = AppEmergencyStateAdapter(app)
    await state.pause_account("tg-7")
    assert (await app.settings.get_policy("tg-7")).allow_new_entries is False
    await state.begin_global("op-1")
    assert await app.settings.get_flag("emergency_active") == "true"
    await state.record_result(
        EmergencyReport("op-1", EmergencyScope.GLOBAL, ("tg-7",), True, 1, 1, True, True, ())
    )
    assert await app.settings.get_flag("emergency_last_result") == "CONFIRMED:op-1"

    composition = build_stage2_composition(app)
    assert isinstance(composition, Stage2Composition)
    assert composition.application is not None
    assert composition.runtime.ready is True


class _FakeLeverageManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def prepare_account(self, account_id, symbols):
        from copytradeaster.trading.leverage import PrepareReport

        self.calls.append((account_id, tuple(symbols)))
        if self.fail:
            return PrepareReport(confirmed={}, failed={"BTCUSDT": "aster:-4028"})
        return PrepareReport(confirmed={s: 200 for s in symbols}, failed={})


async def test_resume_prepares_leverage_and_opens_admission_only_on_success(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    app.follower_registry.runtimes["tg-7"] = SimpleNamespace(
        account_id="tg-7", execution_mode="REAL"
    )
    manager = _FakeLeverageManager()
    app.leverage = manager
    adapter = AppStage2RuntimeAdapter(app)

    await adapter.resume_account("tg-7")

    assert manager.calls == [("tg-7", tuple(app.cfg.symbol_whitelist))]
    # Admission reopened only AFTER leverage was confirmed.
    assert app.coordinator.events == ["open"]


async def test_resume_failure_keeps_admission_closed_for_paused_rollback(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    app.follower_registry.runtimes["tg-7"] = SimpleNamespace(
        account_id="tg-7", execution_mode="REAL"
    )
    app.leverage = _FakeLeverageManager(fail=True)
    adapter = AppStage2RuntimeAdapter(app)

    with pytest.raises(LeverageError, match="aster:-4028"):
        await adapter.resume_account("tg-7")

    # The application service rolls allow_new_entries back on this raise;
    # the runtime boundary must not have reopened admission either.
    assert app.coordinator.events == []


async def test_shadow_runtime_resume_skips_leverage_preparation(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    app.follower_registry.runtimes["tg-shadow"] = SimpleNamespace(
        account_id="tg-shadow", execution_mode="SHADOW"
    )
    manager = _FakeLeverageManager()
    app.leverage = manager
    adapter = AppStage2RuntimeAdapter(app)

    await adapter.resume_account("tg-shadow")
    assert manager.calls == []
    assert app.coordinator.events == ["open"]


async def test_onboarding_activation_failure_surfaces_as_typed_error(database):
    credentials = SqliteCredentials(database, bytes.fromhex("11" * 32))
    app = _App(database, _settings(), credentials)
    users = TelegramUserRepo(database)
    await users.upsert(7, 700, role="USER")
    store = SqliteOnboardingStore(database, credentials, users)
    adapter = DirectOnboardingAdapter(app, store)
    manager = _FakeLeverageManager(fail=True)
    app.leverage = manager

    with pytest.raises(LeverageError, match="BTCUSDT"):
        await adapter._activate("tg-7")
    assert manager.calls == [("tg-7", tuple(app.cfg.symbol_whitelist))]
    # The follower WAS published (durable row stays paused; retryable), and a
    # second attempt must not re-register but must re-attempt preparation.
    first_calls = len(manager.calls)
    with pytest.raises(LeverageError):
        await adapter._activate("tg-7")
    assert len(manager.calls) == first_calls + 1
