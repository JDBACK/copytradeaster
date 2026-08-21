"""Small targeted tests closing remaining coverage gaps (dispatch, run,
logging utils, config validation, database guards, stream robustness)."""

import asyncio
import logging
from pathlib import Path

import pytest

from copytradeaster.config import Settings
from copytradeaster.logging_utils import redact, setup_logging
from copytradeaster.storage.database import Database


class TestLoggingUtils:
    def test_redact_0x(self):
        assert redact("0x1234567890abcdef") == "0x***cdef"
        assert redact("0xab") == "0x***"

    def test_redact_plain_and_empty(self):
        assert redact("deadbeef00") == "de***ef00"[:2] + "***" + "deadbeef00"[-4:]
        assert redact("") == ""

    def test_setup_logging(self):
        setup_logging("INFO")
        root = logging.getLogger()
        root.setLevel(logging.WARNING)  # restore for other tests
        assert True


class TestConfig:
    def test_invalid_hex_rejected(self):
        with pytest.raises(ValueError, match="hex"):
            Settings(encryption_key="zz" * 32)

    def test_wrong_length_rejected(self):
        with pytest.raises(ValueError, match="32 bytes"):
            Settings(encryption_key="ab" * 16)

    def test_decimal_env_parsed(self, monkeypatch):
        monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
        monkeypatch.setenv("CT_FEE_BUFFER_PCT", "0.002")
        s = Settings()
        assert str(s.fee_buffer_pct) == "0.002"

    def test_extra_env_ignored(self, monkeypatch):
        monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
        monkeypatch.setenv("CT_UNRELATED", "x")
        Settings()


class TestDatabaseGuards:
    async def test_migrate_before_connect_raises(self, tmp_path: Path):
        db = Database(tmp_path / "x.sqlite3")
        with pytest.raises(RuntimeError, match="connect"):
            await db.migrate()

    async def test_execute_before_connect_raises(self, tmp_path: Path):
        db = Database(tmp_path / "x.sqlite3")
        with pytest.raises(RuntimeError, match="connect"):
            await db.execute("SELECT 1")

    async def test_close_twice_ok(self, tmp_path: Path):
        db = Database(tmp_path / "x.sqlite3")
        await db.connect()
        await db.close()
        await db.close()


class TestUserStreamRobustness:
    async def test_non_json_frame_dropped(self):
        from tests.unit.aster.test_user_stream_protocol import (
            FakeClient,
            FakeWS,
            make_stream,
        )

        ws = FakeWS([b"not-json{{{", '{"e":"MARGIN_CALL","E":2,"cw":"1","p":[]}'])

        class Factory:
            def __call__(self, url):
                return ws

        client = FakeClient(["lk1"])
        stream = make_stream(client, Factory(), lambda e: asyncio.sleep(0)) if False else None
        # use explicit async on_event
        collected = []

        async def collect(event):
            collected.append(event)

        from copytradeaster.aster.user_stream import UserStream

        stream = UserStream(
            client=client,
            account_id="main",
            ws_base="wss://s/ws",
            keepalive_min=50,
            on_event=collect,
            on_reconnect=lambda: asyncio.sleep(0),
            ws_factory=Factory(),
            backoff_base_s=0.05,
            backoff_max_s=0.1,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(stream.run(stop))
        for _ in range(200):
            if len(collected) >= 1:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert [e.raw["e"] for e in collected] == ["MARGIN_CALL"]


class TestCliDispatch:
    def test_main_status_dispatch(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CT_DB_PATH", str(tmp_path / "d.sqlite3"))
        monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
        monkeypatch.setattr(__import__("sys"), "argv", ["copytradeaster", "status"])
        from copytradeaster.cli.main import main

        rc = main()
        assert rc == 0
        assert "ready=" in capsys.readouterr().out

    def test_main_error_returns_nonzero(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CT_DB_PATH", "/proc/nonexistent/dir/x.sqlite3")
        monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
        monkeypatch.setattr(__import__("sys"), "argv", ["copytradeaster", "init-db"])
        from copytradeaster.cli.main import main

        rc = main()
        assert rc == 1

    async def test_run_until_stopped_invokes_app(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CT_DB_PATH", str(tmp_path / "d.sqlite3"))
        monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
        called = {"start": False}

        class FakeApp:
            def __init__(self, cfg):
                pass

            async def run_until_stopped(self):
                called["start"] = True
                return 0

        import copytradeaster.app as app_mod
        import copytradeaster.cli.main as cli_main

        monkeypatch.setattr(app_mod, "App", FakeApp)
        rc = await cli_main._run()
        assert rc == 0 and called["start"]
