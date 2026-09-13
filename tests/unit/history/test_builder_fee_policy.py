"""Builder fee policy stamp: the net result is unlocked only where it was measured.

The signed testnet campaign proved the Builder fee is charged inside the
ordinary ``COMMISSION`` flow. That fact is stored per account together with the
environment it was measured on, so the money layer may claim
``included_in_commission`` for a Builder account only on that environment —
never by default, never from configuration.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from copytradeaster.history.builder_fee_proof import (
    BuilderFeeProof,
    BuilderFeeProofError,
    policy_stamp,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import AccountRepo


async def _db(tmp_path) -> Database:
    db = Database(tmp_path / "policy.sqlite3")
    await db.connect()
    await db.migrate()
    await AccountRepo(db).upsert("f1", "follower", "f1", "0xabc")
    return db


async def _policy(db: Database, environment: str = "testnet") -> str:
    return await HistoryRepository(db).builder_policy(HistoryScope("f1", environment))


async def test_direct_account_is_proven_without_a_stamp(tmp_path) -> None:
    db = await _db(tmp_path)
    try:
        assert await _policy(db) == "included_in_commission"
    finally:
        await db.close()


async def test_builder_account_without_a_stamp_stays_unknown(tmp_path) -> None:
    db = await _db(tmp_path)
    try:
        await AccountRepo(db).set_builder_snapshot(
            "f1",
            builder_address="0x" + "1" * 40,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at="2026-09-13T00:00:00Z",
        )
        assert await _policy(db) == "unknown"
    finally:
        await db.close()


async def test_builder_account_with_matching_stamp_claims_the_policy(tmp_path) -> None:
    db = await _db(tmp_path)
    try:
        await AccountRepo(db).set_builder_snapshot(
            "f1",
            builder_address="0x" + "1" * 40,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at="2026-09-13T00:00:00Z",
        )
        await AccountRepo(db).set_builder_fee_policy(
            "f1", "included_in_commission:testnet:1789270000"
        )
        assert await _policy(db) == "included_in_commission"
        # A stamp measured on one environment never leaks into another one.
        assert await _policy(db, "mainnet") == "unknown"
    finally:
        await db.close()


async def test_malformed_or_unknown_stamp_is_never_trusted(tmp_path) -> None:
    db = await _db(tmp_path)
    try:
        repo = AccountRepo(db)
        await repo.set_builder_snapshot(
            "f1",
            builder_address="0x" + "1" * 40,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at="2026-09-13T00:00:00Z",
        )
        for stamp in (
            "included_in_commission",
            "included_in_commission:testnet",
            "separate:testnet:1789270000",
            "unknown:testnet:1789270000",
            "included_in_commission:testnet:extra:parts",
        ):
            await repo.set_builder_fee_policy("f1", stamp)
            assert await _policy(db) == "unknown", stamp
    finally:
        await db.close()


async def test_going_direct_drops_a_builder_stamp(tmp_path) -> None:
    db = await _db(tmp_path)
    try:
        repo = AccountRepo(db)
        await repo.set_builder_snapshot(
            "f1",
            builder_address="0x" + "1" * 40,
            builder_name="copytradeaster",
            builder_max_fee_rate="0.0001",
            builder_agent_expires_at_ms=2_000_000_000_000,
            builder_verified_at="2026-09-13T00:00:00Z",
        )
        await repo.set_builder_fee_policy("f1", "included_in_commission:testnet:1789270000")
        await repo.set_identity_direct("f1")
        row = await db.fetchone("SELECT builder_fee_policy FROM accounts WHERE id='f1'")
        assert row is not None and row["builder_fee_policy"] is None
        assert await _policy(db) == "included_in_commission"
    finally:
        await db.close()


def test_stamp_carries_policy_environment_and_time() -> None:
    proof = BuilderFeeProof(
        policy="included_in_commission",
        attributed_rate=Decimal("0.0005"),
        control_rate=Decimal("0.0004"),
        fee_rate=Decimal("0.0001"),
        attributed_fills=2,
        control_fills=2,
        income_types=("COMMISSION", "REALIZED_PNL"),
        detail="measured",
    )
    stamp = policy_stamp(proof, "testnet")
    policy, environment, measured_at = stamp.split(":")
    assert policy == "included_in_commission"
    assert environment == "testnet"
    assert measured_at.isdigit() and len(measured_at) == 10


def test_an_unknown_policy_is_never_stamped() -> None:
    proof = BuilderFeeProof(
        policy="unknown",
        attributed_rate=Decimal("0.0004"),
        control_rate=Decimal("0.0004"),
        fee_rate=Decimal("0.0001"),
        attributed_fills=2,
        control_fills=2,
        income_types=("COMMISSION",),
        detail="no delta",
    )
    with pytest.raises(BuilderFeeProofError):
        policy_stamp(proof, "testnet")
