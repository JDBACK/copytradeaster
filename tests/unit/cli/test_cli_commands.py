"""Coverage for CLI command handlers against a temporary database."""

from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.cli.main import (
    _init_db,
    _load_settings,
    _set_pause,
    _status,
    build_close_plan,
    build_parser,
)
from copytradeaster.domain.models import (
    AccountState,
    PositionKey,
    PositionState,
    PosSide,
    ProtectiveOrder,
    Side,
    TargetPosition,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import SettingsRepo

WALLET = """\
main test

evm adress: 0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

api wallet adress: 0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB

privte key: 0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("CT_DB_PATH", str(db_path))
    monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
    return tmp_path, db_path


async def test_init_db_creates_schema(env):
    tmp_path, db_path = env
    rc = await _init_db()
    assert rc == 0
    assert db_path.exists()
    db = Database(db_path)
    await db.connect()
    tables = {
        r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "execution_commands" in tables
    await db.close()


async def test_status_reports_state(env, capsys):
    tmp_path, db_path = env
    await _init_db()
    db = Database(db_path)
    await db.connect()
    await SettingsRepo(db).set_flag("degraded", "copyc1:x")
    await db.close()
    rc = await _status()
    out = capsys.readouterr().out
    assert rc == 0
    assert "degraded=copyc1:x" in out
    assert "account main" not in out or "role=" in out


async def test_pause_resume_flags(env, capsys):
    await _init_db()
    assert await _set_pause(True) == 0
    assert await _set_pause(False) == 0
    out = capsys.readouterr().out
    assert "paused" in out and "resumed" in out


def test_parser_requires_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_load_settings_from_env(env):
    tmp_path, db_path = env
    settings = _load_settings()
    assert str(settings.db_path) == str(db_path)


def test_build_close_plan_cancels_then_closes():
    key = PositionKey("BTCUSDT")
    pos = PositionState(
        key=key,
        qty=D("0.05"),
        entry_price=D(65000),
        leverage=20,
        mark_price=D(65000),
        liquidation_price=D(30000),
        stop=ProtectiveOrder(
            client_order_id="ct-s",
            order_id=1,
            type="STOP_MARKET",
            side=Side.SELL,
            stop_price=D(64000),
            reduce_only=True,
            close_position=False,
            working_type="MARK_PRICE",
        ),
    )
    actual = AccountState(account_id="copyc1", available_usdt=D(100), positions={key: pos})
    targets = [TargetPosition(key=key, qty=D(0), stop_price=None, tp_price=None, leverage=20)]
    plan = build_close_plan("copyc1", actual, targets)
    kinds = [(a.kind, a.priority) for a in plan.actions]
    assert kinds == [("CANCEL_ORDER", 1), ("MARKET_CLOSE", 2)]
    close = plan.actions[1]
    assert close.payload["quantity"] == "0.05"
    assert close.payload["side"] == "SELL"
    assert close.payload["reduceOnly"] == "true"


def test_build_close_plan_hedge_side():
    key = PositionKey("BTCUSDT", PosSide.SHORT)
    pos = PositionState(
        key=key,
        qty=D("1"),
        entry_price=D(150),
        leverage=10,
        mark_price=D(150),
        liquidation_price=D(200),
    )
    actual = AccountState(account_id="c", available_usdt=D(10), positions={key: pos})
    targets = [TargetPosition(key=key, qty=D(0), stop_price=None, tp_price=None, leverage=10)]
    plan = build_close_plan("c", actual, targets)
    close = plan.actions[0]
    assert close.payload["side"] == "BUY"  # closes short leg
    assert close.payload["positionSide"] == "SHORT"


async def test_import_wallet_end_to_end(env, capsys):
    tmp_path, db_path = env
    wallet = tmp_path / "wallet.txt"
    wallet.write_text(WALLET)
    from copytradeaster.cli.main import _import_wallet

    rc = await _import_wallet(wallet)
    assert rc == 0
    out = capsys.readouterr().out
    assert "role=master" in out
    assert "a1a1" not in out  # keys never printed
    # idempotent re-import
    rc2 = await _import_wallet(wallet)
    assert rc2 == 0
