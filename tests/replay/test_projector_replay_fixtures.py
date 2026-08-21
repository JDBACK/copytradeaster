"""Replay every tests/replay/fixtures/*.json through the projector and verify
the final MasterState against the expected snapshot embedded in the file."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from copytradeaster.master.projector import MasterProjector
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import (
    CopyTradeRepo,
    MasterEventRepo,
    OrderLinkRepo,
    SettingsRepo,
)

FIXTURES = Path(__file__).parent / "fixtures"


async def build_projector(tmp_path: Path):
    db = Database(tmp_path / "replay.sqlite3")
    await db.connect()
    await db.migrate()
    deltas: list = []

    async def on_delta(delta):
        deltas.append(delta)

    projector = MasterProjector(
        MasterEventRepo(db), CopyTradeRepo(db), OrderLinkRepo(db), SettingsRepo(db), on_delta
    )
    return projector, deltas, db


def replay_fixture(fixture_path: Path, tmp_path: Path):
    async def run():
        payload = json.loads(fixture_path.read_text())
        projector, deltas, db = await build_projector(tmp_path)
        for raw in payload["events"]:
            await projector.apply_raw(raw)
        expected = payload["expected"]
        state = projector.state
        got_positions = {
            f"{k.symbol}:{k.pos_side}": {
                "qty": str(p.qty.quantize(Decimal("0.001"))),
                "leverage": p.leverage,
                "stop": p.stop.stop_price if p.stop else None,
                "tp": p.take_profit.stop_price if p.take_profit else None,
            }
            for k, p in state.positions.items()
            if p.qty != 0 or p.stop is not None or p.take_profit is not None
        }
        result = {"available_usdt": str(state.available_usdt), "positions": got_positions}
        for key, want in expected["positions"].items():
            assert key in got_positions, f"{fixture_path.name}: missing {key}: {got_positions}"
            got = got_positions[key]
            assert got["qty"] == str(Decimal(want["qty"])), (
                f"{fixture_path.name}:{key} qty {got['qty']} != {want['qty']}"
            )
            if "stop" in want:
                assert got["stop"] is not None and got["stop"].quantize(Decimal("1")) == Decimal(
                    want["stop"]
                )
        if "available_usdt" in expected:
            assert result["available_usdt"] == expected["available_usdt"]
        trades = await db.fetchall("SELECT * FROM copy_trades WHERE status='OPEN'")
        assert len(trades) == expected.get("open_trades", 0), (
            f"{fixture_path.name}: open trades {len(trades)}"
        )
        await db.close()
        return result

    return asyncio.run(run())


@pytest.mark.parametrize("fixture_path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_replay_fixture(fixture_path: Path, tmp_path: Path):
    replay_fixture(fixture_path, tmp_path)
