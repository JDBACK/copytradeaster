"""Small targeted tests closing remaining coverage gaps (dispatch, run,
logging utils, config validation, database guards, stream robustness)."""

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from copytradeaster.config import Settings
from copytradeaster.logging_utils import (
    log_control_audit,
    log_onboarding_audit,
    redact,
    setup_logging,
)
from copytradeaster.storage.database import Database


class TestLoggingUtils:
    def test_redact_0x(self):
        assert redact("0x1234567890abcdef") == "0x***cdef"
        assert redact("0xab") == "0x***"

    def test_redact_plain_and_empty(self):
        assert redact("deadbeef00") == "de***ef00"[:2] + "***" + "deadbeef00"[-4:]
        assert redact("") == ""

    def test_setup_logging(self, tmp_path: Path):
        path = tmp_path / "logs" / "copytradeaster.log"
        setup_logging("INFO", log_path=path, max_bytes=128, backup_count=2)
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        root = logging.getLogger()
        managed = [
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == path.resolve()
        ]
        assert len(managed) == 1
        setup_logging("INFO", log_path=path, max_bytes=128, backup_count=2)
        managed_again = [
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == path.resolve()
        ]
        assert managed_again == managed
        logging.getLogger("copytradeaster.test").info("persistent event")
        for handler in managed:
            handler.flush()
        assert path.exists()
        assert "persistent event" in path.read_text(encoding="utf-8")
        if os.name == "posix":
            assert path.stat().st_mode & 0o777 == 0o600
        root.setLevel(logging.WARNING)  # restore for other tests
        setup_logging("WARNING", log_path=None)
        assert True

    def test_setup_logging_rotation_and_secret_safe_file(self, tmp_path: Path):
        path = tmp_path / "logs" / "copytradeaster.log"
        setup_logging("INFO", log_path=path, max_bytes=100, backup_count=1)
        logger = logging.getLogger("copytradeaster.secret-test")
        for index in range(5):
            logger.info(
                "signed URL https://fapi.asterdex-testnet.com/fapi/v3/order?"
                "signature=signature-secret-%s&apiKey=api-secret-%s private_key=private-secret-%s",
                index,
                index,
                index,
            )
        for handler in logging.getLogger().handlers:
            if isinstance(handler, RotatingFileHandler):
                handler.flush()
        files = sorted(path.parent.glob("copytradeaster.log*"))
        assert path.exists()
        assert len(files) >= 2
        if os.name == "posix":
            assert all(item.stat().st_mode & 0o777 == 0o600 for item in files)
        text = "\n".join(item.read_text(encoding="utf-8") for item in files)
        assert "signature-secret" not in text
        assert "api-secret" not in text
        assert "private-secret" not in text
        setup_logging("WARNING", log_path=None)

    def test_handler_filter_accepts_formatted_records_and_audit_levels(
        self, tmp_path: Path, caplog
    ):
        path = tmp_path / "logs" / "copytradeaster.log"
        setup_logging("INFO", log_path=path, max_bytes=4096, backup_count=1)
        logger = logging.getLogger("copytradeaster.audit-test")
        with caplog.at_level(logging.INFO):
            logger.info("third-party event=%s", "plain")
            logger.info("wallet=%s private_key=%s", "0x" + "a" * 40, "0x" + "b" * 64)
            log_onboarding_audit(
                logger,
                event="attempt",
                mode="connect",
                user_id=7,
                account_id="tg-7",
                result="started",
                paused=None,
            )
            log_onboarding_audit(
                logger,
                event="rejected",
                mode="connect",
                user_id=7,
                account_id="tg-7",
                result="rejected",
                paused=None,
                category="unexpected",
                exception_type="RuntimeError",
            )
        for handler in logging.getLogger().handlers:
            if isinstance(handler, RotatingFileHandler):
                handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "third-party event=plain" in text
        assert "0x" + "a" * 40 not in text
        assert "0x" + "b" * 64 not in text
        assert any(
            record.levelno == logging.INFO
            and "event=attempt" in record.getMessage()
            and "paused=unknown" in record.getMessage()
            for record in caplog.records
        )
        assert any(
            record.levelno == logging.WARNING
            and "event=rejected" in record.getMessage()
            and "paused=unknown" in record.getMessage()
            for record in caplog.records
        )
        setup_logging("WARNING", log_path=None)

    def test_control_audit_is_bounded_fixed_and_address_safe(self, caplog):
        logger = logging.getLogger("copytradeaster.control-audit-test")
        address = "0x" + "a" * 40
        oversized_code = "safe-code-" + "x" * 500
        with caplog.at_level(logging.INFO):
            log_control_audit(
                logger,
                action="update_risk",
                result="attempt",
                user_id=7,
                account_id=address,
                changed_fields=(
                    "risk_percent",
                    "risk_percent",
                    "private_key_should_be_ignored",
                ),
                state="x" * 500,
                category="unexpected",
                code=oversized_code,
                exception_type="RuntimeError",
            )
            log_control_audit(
                logger,
                action="update_risk",
                result="rejected",
                user_id=7,
                account_id="tg-7",
                category="unexpected",
                exception_type="RuntimeError",
            )

        records = [record for record in caplog.records if "control_audit" in record.getMessage()]
        assert len(records) == 2
        attempt, rejected = records
        assert attempt.levelno == logging.INFO
        assert rejected.levelno == logging.WARNING
        message = attempt.getMessage()
        assert "control_audit action=update_risk result=attempt user_id=7" in message
        assert "account_id=redacted" in message
        assert "changed_fields=risk_percent" in message
        assert "private_key_should_be_ignored" not in message
        assert address not in message
        assert oversized_code not in message
        assert len(message) < 500

    def test_control_audit_logging_failure_is_best_effort(self, monkeypatch):
        class BrokenLogger:
            def info(self, message):
                del message
                raise RuntimeError("logger failed")

            warning = info

        log_control_audit(
            BrokenLogger(),
            action="pause",
            result="success",
            user_id=7,
            account_id="tg-7",
            state="paused",
        )
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

    def test_log_level_is_normalized_and_validated(self):
        assert Settings(encryption_key="ab" * 32, log_level="warning").log_level == "WARNING"
        with pytest.raises(ValueError, match="CT_LOG_LEVEL"):
            Settings(encryption_key="ab" * 32, log_level="TRACE")

    def test_redacted_dump_contains_no_secret_values(self):
        settings = Settings(
            _env_file=None,
            encryption_key="ab" * 32,
            telegram_bot_token="123456:secret-token",
        )
        dumped = repr(settings.redacted_dump())
        assert "ab" * 32 not in dumped
        assert "secret-token" not in dumped

    def test_extra_env_ignored(self, monkeypatch):
        monkeypatch.setenv("CT_ENCRYPTION_KEY", "ab" * 32)
        monkeypatch.setenv("CT_UNRELATED", "x")
        Settings()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("position_mode", "invalid"),
            ("working_type", "last_price"),
            ("reconcile_interval_s", 0),
            ("orders_limit_per_min", -1),
        ],
    )
    def test_invalid_runtime_settings_rejected(self, field, value):
        with pytest.raises(ValueError):
            Settings(encryption_key="ab" * 32, **{field: value})

    def test_symbols_are_normalized_and_must_not_be_empty(self):
        settings = Settings(
            encryption_key="ab" * 32,
            symbol_whitelist=[" btcusdt ", "BTCUSDT", "ethusdt"],
        )
        assert settings.symbol_whitelist == ["BTCUSDT", "ETHUSDT"]
        with pytest.raises(ValueError, match="must not be empty"):
            Settings(encryption_key="ab" * 32, symbol_whitelist=[])

    def test_mainnet_urls_fail_closed_without_explicit_override(self):
        with pytest.raises(ValueError, match="refuses non-testnet"):
            Settings(
                encryption_key="ab" * 32,
                rest_base_url="https://fapi.asterdex.com",
                ws_user_base="wss://fstream.asterdex.com/ws",
                testnet_ip_whitelist_override=False,
            )


class TestDatabaseGuards:
    async def test_migrate_before_connect_raises(self, tmp_path: Path):
        db = Database(tmp_path / "x.sqlite3")
        with pytest.raises(RuntimeError, match="connect"):
            await db.migrate()

    async def test_execute_before_connect_raises(self, tmp_path: Path):
        db = Database(tmp_path / "x.sqlite3")
        with pytest.raises(RuntimeError, match="connect"):
            await db.execute("SELECT 1")

    async def test_busy_timeout_pragmas_set(self, tmp_path: Path):
        db = Database(tmp_path / "x.sqlite3")
        await db.connect()
        row = await db.fetchone("PRAGMA busy_timeout")
        assert list(row)[0] == 30_000
        assert db.conn.isolation_level is None
        await db.close()

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


class TestAppStartupGap:
    async def test_connected_callback_resnapshots_before_stream_ready_once(self):
        from copytradeaster.app import App

        calls: list[str] = []

        class Projector:
            state = object()

            async def rebuild_full(self, client, account_id):
                assert client is master_client and account_id == "main"
                assert not app._stream_ready.is_set()
                calls.append("rebuild")

        class Reconciler:
            async def reconcile(self, state, reason):
                assert state is Projector.state and reason == "post-stream-startup"
                assert not app._stream_ready.is_set()
                calls.append("reconcile")

        master_client = object()
        app = App.__new__(App)
        app.master_client = master_client
        app.projector = Projector()
        app.reconciler = Reconciler()
        app._stream_ready = asyncio.Event()
        app._stream_connect_count = 0

        await app._on_stream_connected()
        assert calls == ["rebuild", "reconcile"]
        assert app._stream_ready.is_set() and app._stream_connect_count == 1

        await app._on_stream_connected()
        assert calls == ["rebuild", "reconcile"]
        assert app._stream_connect_count == 2


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
