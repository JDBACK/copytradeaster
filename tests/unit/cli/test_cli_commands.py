"""Coverage for CLI command handlers against a temporary database."""

from decimal import Decimal as D
from pathlib import Path

import pytest

from copytradeaster.cli.main import (
    _dispatch,
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
from copytradeaster.storage.repositories import AccountRepo, SettingsRepo, TelegramUserRepo

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


async def test_status_does_not_trust_stale_ready_flag(env, capsys):
    tmp_path, db_path = env
    del tmp_path
    await _init_db()
    db = Database(db_path)
    await db.connect()
    settings = SettingsRepo(db)
    await settings.set_flag("ready", "true")
    await settings.set_flag("heartbeat_at", "1")
    await db.close()
    assert await _status() == 0
    out = capsys.readouterr().out
    assert "ready=false" in out and "runtime=stale" in out


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


def test_build_close_plan_cancels_orphan_and_extra_protections():
    key = PositionKey("BTCUSDT")

    def protection(coid, price):
        return ProtectiveOrder(
            client_order_id=coid,
            order_id=1,
            type="STOP_MARKET",
            side=Side.SELL,
            stop_price=D(price),
            reduce_only=False,
            close_position=True,
            working_type="MARK_PRICE",
        )

    pos = PositionState(
        key=key,
        qty=D(0),
        entry_price=D(0),
        leverage=20,
        mark_price=D(0),
        liquidation_price=D(0),
        stop=protection("ct-new", 64000),
        extra_stops=(protection("ct-old", 64500),),
    )
    actual = AccountState(account_id="c", available_usdt=D(0), positions={key: pos})
    target = TargetPosition(key, D(0), None, None, 20)
    plan = build_close_plan("c", actual, [target], operation_id="emg-1", revision=2)
    assert {a.payload["origClientOrderId"] for a in plan.actions} == {"ct-new", "ct-old"}
    assert plan.operation_id == "emg-1"
    assert all(action.revision == 2 for action in plan.actions)


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


def test_parser_telegram_user_commands() -> None:
    parser = build_parser()

    add = parser.parse_args(["telegram-user", "add", "123", "--chat-id", "456"])
    assert add.command == "telegram-user"
    assert add.telegram_user_action == "add"
    assert add.user_id == "123"
    assert add.chat_id == "456"

    listing = parser.parse_args(["telegram-user", "list"])
    assert listing.telegram_user_action == "list"
    block = parser.parse_args(["telegram-user", "block", "123"])
    unblock = parser.parse_args(["telegram-user", "unblock", "123"])
    assert block.user_id == unblock.user_id == "123"


@pytest.mark.parametrize(
    ("user_id", "chat_id"),
    [("0", None), ("-1", None), ("not-an-int", None), ("123", "0"), ("123", "no")],
)
async def test_telegram_user_add_rejects_invalid_ids(env, user_id, chat_id, capsys):
    rc = await _dispatch(
        build_parser().parse_args(
            [
                "telegram-user",
                "add",
                user_id,
                *([] if chat_id is None else ["--chat-id", chat_id]),
            ]
        )
    )
    assert rc == 2
    assert "positive integer" in capsys.readouterr().err


async def test_telegram_user_rejects_invalid_boolean_and_unknown_block_target(env, capsys):
    from copytradeaster.cli.main import _telegram_user_set_blocked

    assert await _telegram_user_set_blocked("123", blocked="false") == 2  # type: ignore[arg-type]
    assert "boolean" in capsys.readouterr().err
    assert await _telegram_user_set_blocked("123", blocked=True) == 2
    assert "not found" in capsys.readouterr().err


async def test_telegram_user_add_defaults_chat_and_is_idempotent(env, capsys):
    add = build_parser().parse_args(["telegram-user", "add", "123"])
    assert await _dispatch(add) == 0
    first_output = capsys.readouterr().out
    assert "added telegram user 123" in first_output

    db = Database(env[1])
    await db.connect()
    first = await TelegramUserRepo(db).get(123)
    await db.close()
    assert first is not None
    assert (first["chat_id"], first["role"], first["account_id"]) == (123, "USER", None)

    assert await _dispatch(add) == 0
    second_output = capsys.readouterr().out
    assert "already present telegram user 123" in second_output
    db = Database(env[1])
    await db.connect()
    second = await TelegramUserRepo(db).get(123)
    await db.close()
    assert second is not None
    assert dict(second)["created_at"] == dict(first)["created_at"]
    assert (second["chat_id"], second["role"], second["account_id"]) == (123, "USER", None)


async def test_telegram_user_add_preserves_existing_role_and_account(env, capsys):
    await _init_db()
    db = Database(env[1])
    await db.connect()
    accounts = AccountRepo(db)
    await accounts.upsert("copyc1", "follower", "copyc1", "0x" + "aa" * 20)
    users = TelegramUserRepo(db)
    await users.upsert(700, 7_700, role="ADMIN")
    await users.upsert(701, 7_701, account_id="copyc1")
    await users.set_notifications_enabled(701, False)
    before = await users.get(701)
    await db.close()

    assert await _dispatch(build_parser().parse_args(["telegram-user", "add", "700"])) == 0
    assert await _dispatch(build_parser().parse_args(["telegram-user", "add", "701"])) == 0
    capsys.readouterr()

    db = Database(env[1])
    await db.connect()
    refreshed_users = TelegramUserRepo(db)
    after_admin = await refreshed_users.get(700)
    after_user = await refreshed_users.get(701)
    await db.close()
    assert before is not None and after_user is not None and after_admin is not None
    assert after_admin["role"] == "ADMIN"
    assert after_user["account_id"] == "copyc1"
    assert after_user["chat_id"] == 7_701
    assert after_user["notifications_enabled"] == before["notifications_enabled"] == 0
    assert after_user["blocked_at"] == before["blocked_at"]


async def test_telegram_user_list_is_safe_and_block_unblock_is_idempotent(env, capsys):
    await _init_db()
    assert await _dispatch(build_parser().parse_args(["telegram-user", "add", "900"])) == 0
    capsys.readouterr()
    assert await _dispatch(build_parser().parse_args(["telegram-user", "block", "900"])) == 0
    assert await _dispatch(build_parser().parse_args(["telegram-user", "block", "900"])) == 0
    assert await _dispatch(build_parser().parse_args(["telegram-user", "list"])) == 0
    output = capsys.readouterr().out
    assert "user_id=900" in output
    assert "chat_id=900" in output
    assert "role=USER" in output
    assert "account=-" in output
    assert "access=on" in output
    assert "notifications=off" in output
    assert "blocked=yes" in output
    assert "private-key-value" not in output
    assert "0xsecret" not in output

    assert await _dispatch(build_parser().parse_args(["telegram-user", "unblock", "900"])) == 0
    assert await _dispatch(build_parser().parse_args(["telegram-user", "unblock", "900"])) == 0
    capsys.readouterr()
    db = Database(env[1])
    await db.connect()
    row = await TelegramUserRepo(db).get(900)
    await db.close()
    assert row is not None
    assert row["notifications_enabled"] == 1
    assert row["blocked_at"] is None
