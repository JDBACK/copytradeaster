"""P2.6 reauthorization store regression tests (``SqliteOnboardingStore``).

Covers the same-signer re-approval durability boundary over real SQLite:
context gating (builder-only), the credential version marker, and the
race-gated atomic ``commit_reauthorization`` — a replacement/disconnect
that lands mid-read aborts instead of storing an old proof, and the
account stays paused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from copytradeaster.application.adapters import (
    AppStage2RuntimeAdapter,
    SqliteOnboardingStore,
)
from copytradeaster.application.models import ServiceUnavailableError
from copytradeaster.aster.models import BuilderDTO
from copytradeaster.aster.trading_api import AsterError
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import RiskPolicy
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    AccountRepo,
    AgentValidation,
    AgentValidationRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from tests.unit.aster.conftest import KEY, SIGNER, USER

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
OTHER_SIGNER = "0x" + "44" * 20

POLICY = RiskPolicy(
    risk_percent=Decimal("0.01"),
    max_leverage=3,
    max_entry_deviation_pct=Decimal("0.02"),
    enabled=True,
    allow_new_entries=True,
)


def _validation(signer: str = SIGNER) -> AgentValidation:
    return AgentValidation(
        signer_address=signer,
        can_read=True,
        can_perp_trade=True,
        can_withdraw=False,
        expired_ms=2_000_000_000_000,
        ip_whitelist="127.0.0.1/32",
        status="VALID",
        checked_at=NOW.isoformat(),
    )


@pytest.fixture
async def reauth_env(tmp_path: Path):
    db = Database(tmp_path / "reauth_store.sqlite3")
    try:
        await db.connect()
        await db.migrate()
        users = TelegramUserRepo(db)
        await AccountRepo(db).upsert("tg-7", "follower", "tg-7", USER)
        await users.upsert(7, 700, account_id="tg-7")
        credentials = SqliteCredentials(db, bytes.fromhex("11" * 32))
        await credentials.import_identity("tg-7", USER, KEY, "follower", "tg-7")
        await AgentValidationRepo(db).upsert("tg-7", validation=_validation())
        await SettingsRepo(db).upsert_policy(POLICY, "tg-7")
        store = SqliteOnboardingStore(db, credentials, users)
        yield SimpleNamespace(db=db, store=store)
    finally:
        await db.close()


async def _make_builder(env) -> None:
    await AccountRepo(env.db).set_builder_snapshot(
        "tg-7",
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at="2026-01-01T00:00:00+00:00",
    )


async def test_context_none_for_direct_account(reauth_env):
    ctx = await reauth_env.store.get_reauthorize_context(7)

    assert ctx is None


async def test_context_returns_current_identity_for_builder(reauth_env):
    await _make_builder(reauth_env)

    ctx = await reauth_env.store.get_reauthorize_context(7)

    assert ctx is not None
    assert ctx.account_id == "tg-7"
    assert ctx.user_address == USER
    assert ctx.signer_address == SIGNER


async def test_context_none_when_disabled_or_unvalidated(reauth_env):
    await _make_builder(reauth_env)
    await reauth_env.db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-7",))

    assert await reauth_env.store.get_reauthorize_context(7) is None

    await reauth_env.db.execute_write("UPDATE accounts SET enabled=1 WHERE id=?", ("tg-7",))
    await reauth_env.db.execute_write("DELETE FROM agent_validations WHERE account_id=?", ("tg-7",))

    assert await reauth_env.store.get_reauthorize_context(7) is None


async def test_account_version_tracks_identity(reauth_env):
    version = await reauth_env.store.account_version("tg-7")

    assert version is not None
    assert SIGNER in version
    assert version.endswith(":1")

    assert await reauth_env.store.account_version("tg-missing") is None


async def test_commit_reauthorization_repairs_proof_paused(reauth_env):
    await _make_builder(reauth_env)
    expected = await reauth_env.store.account_version("tg-7")
    assert expected is not None

    await reauth_env.store.commit_reauthorization(
        account_id="tg-7",
        telegram_user_id=7,
        validation=_validation(),
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at=NOW.isoformat(),
        expected_version=expected,
    )

    snapshot = await AccountRepo(reauth_env.db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["identity_source"] == "builder"
    assert snapshot["builder_address"] == BUILDER
    assert snapshot["builder_verified_at"] == NOW.isoformat()
    settings = await reauth_env.db.fetchone(
        "SELECT allow_new_entries FROM copy_settings WHERE account_id=?", ("tg-7",)
    )
    assert settings is not None
    assert int(settings["allow_new_entries"]) == 0


async def test_commit_reauthorization_rejects_stale_version(reauth_env):
    await _make_builder(reauth_env)

    with pytest.raises(ValueError, match="identity changed"):
        await reauth_env.store.commit_reauthorization(
            account_id="tg-7",
            telegram_user_id=7,
            validation=_validation(),
            builder_address=BUILDER,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at=NOW.isoformat(),
            expected_version="stale-version",
        )

    snapshot = await AccountRepo(reauth_env.db).get_builder_snapshot("tg-7")
    assert snapshot is not None
    assert snapshot["builder_verified_at"] == "2026-01-01T00:00:00+00:00"


async def test_commit_reauthorization_rejects_signer_mismatch(reauth_env):
    await _make_builder(reauth_env)
    expected = await reauth_env.store.account_version("tg-7")
    assert expected is not None

    with pytest.raises(ValueError, match="signer"):
        await reauth_env.store.commit_reauthorization(
            account_id="tg-7",
            telegram_user_id=7,
            validation=_validation(signer=OTHER_SIGNER),
            builder_address=BUILDER,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at=NOW.isoformat(),
            expected_version=expected,
        )


async def test_commit_reauthorization_rejects_foreign_user(reauth_env):
    await _make_builder(reauth_env)
    expected = await reauth_env.store.account_version("tg-7")
    assert expected is not None

    with pytest.raises(ValueError, match="own"):
        await reauth_env.store.commit_reauthorization(
            account_id="tg-7",
            telegram_user_id=8,
            validation=_validation(),
            builder_address=BUILDER,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at=NOW.isoformat(),
            expected_version=expected,
        )


async def test_context_none_for_unknown_user_or_missing_account(reauth_env):
    assert await reauth_env.store.get_reauthorize_context(999) is None

    users = TelegramUserRepo(reauth_env.db)
    await users.upsert(9, 900)
    assert await reauth_env.store.get_reauthorize_context(9) is None


async def test_commit_reauthorization_rejects_inactive_or_vanished_identity(reauth_env):
    await _make_builder(reauth_env)
    expected = await reauth_env.store.account_version("tg-7")
    assert expected is not None
    kwargs = dict(
        account_id="tg-7",
        telegram_user_id=7,
        validation=_validation(),
        builder_address=BUILDER,
        builder_name="copytradeaster",
        builder_max_fee_rate="0.0001",
        builder_agent_expires_at_ms=2_000_000_000_000,
        builder_verified_at=NOW.isoformat(),
        expected_version=expected,
    )

    await reauth_env.db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("tg-7",))
    with pytest.raises(ValueError, match="not active"):
        await reauth_env.store.commit_reauthorization(**kwargs)

    await reauth_env.db.execute_write("UPDATE accounts SET enabled=1 WHERE id=?", ("tg-7",))
    await reauth_env.db.execute_write("DELETE FROM credentials WHERE account_id=?", ("tg-7",))
    with pytest.raises(ValueError, match="identity is gone"):
        await reauth_env.store.commit_reauthorization(**kwargs)


class _BuilderClient:
    def __init__(self, result: object) -> None:
        self._result = result

    async def builder(self, account_id: str):
        del account_id
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _BuilderRegistry:
    def __init__(self, runtimes: dict) -> None:
        self._runtimes = runtimes

    def get(self, account_id: str):
        return self._runtimes.get(account_id)


def _builder_adapter(result: object) -> AppStage2RuntimeAdapter:
    app = SimpleNamespace(
        db=None,
        cfg=SimpleNamespace(is_testnet=True),
        credentials=None,
        follower_registry=_BuilderRegistry(
            {"tg-7": SimpleNamespace(client=_BuilderClient(result))}
        ),
    )
    return AppStage2RuntimeAdapter(app)  # type: ignore[arg-type]


def _typed_records() -> list[BuilderDTO]:
    return [
        BuilderDTO.model_validate(
            {
                "userAddress": USER,
                "builderAddress": BUILDER,
                "builderName": "copytradeaster",
                "maxFeeRate": "0.0001",
            }
        )
    ]


async def test_refresh_builder_record_returns_typed_list():
    adapter = _builder_adapter(_typed_records())

    records = await adapter.refresh_builder_record("tg-7")

    assert len(records) == 1
    assert records[0].builder_address == BUILDER


async def test_refresh_builder_record_missing_runtime_is_key_error():
    adapter = _builder_adapter([])

    with pytest.raises(KeyError):
        await adapter.refresh_builder_record("tg-missing")


async def test_refresh_builder_record_transient_maps_to_unavailable():
    adapter = _builder_adapter(AsterError(-1007, "timeout"))

    with pytest.raises(ServiceUnavailableError):
        await adapter.refresh_builder_record("tg-7")


async def test_refresh_builder_record_rejection_propagates():
    adapter = _builder_adapter(AsterError(-2015, "bad key"))

    with pytest.raises(AsterError):
        await adapter.refresh_builder_record("tg-7")


async def test_refresh_builder_record_non_list_is_unavailable():
    adapter = _builder_adapter({"not": "a-list"})

    with pytest.raises(ServiceUnavailableError):
        await adapter.refresh_builder_record("tg-7")


async def test_find_identity_conflict_pins_legacy_reonboard_policy(reauth_env):
    """A disabled legacy-slot account blocks same-address re-onboarding.

    The owner still linked to the disabled legacy row gets ``owner``; a
    pre-allowlisted fresh user offering the same address gets ``address``.
    Only the stable-slot reconnect path returns None.  This pins the
    product behavior the P2.6 live re-onboarding step must plan around.
    """
    db = reauth_env.db
    store = reauth_env.store
    legacy_user = "0x" + "ab" * 20
    await AccountRepo(db).upsert("legacy-1", "follower", "legacy-1", legacy_user)
    await db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", ("legacy-1",))
    users = TelegramUserRepo(db)
    await users.upsert(11, 1100, account_id="legacy-1")
    await users.upsert(12, 1200)

    assert await store.find_identity_conflict(11, legacy_user) == "owner"
    assert await store.find_identity_conflict(12, legacy_user) == "address"
