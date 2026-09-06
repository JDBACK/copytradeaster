"""WP4 application lifecycle and REAL/SHADOW routing boundaries."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

import copytradeaster.app as app_module
from copytradeaster.app import (
    TESTNET_REST_URL,
    TESTNET_WS_URL,
    App,
    validate_shadow_boot,
)
from copytradeaster.aster.shadow import ShadowExchange
from copytradeaster.config import Settings
from copytradeaster.domain.models import AccountState, ActionKind, ExecutionAction
from copytradeaster.notifications.models import NotificationEvent, NotificationSeverity
from copytradeaster.trading.coordinator import FollowerRuntime
from copytradeaster.trading.registry import FollowerRegistry


def cfg(*, shadow_enabled: bool, rest: str = TESTNET_REST_URL, ws: str = TESTNET_WS_URL):
    return SimpleNamespace(
        shadow_accounts_enabled=shadow_enabled,
        rest_base_url=rest,
        ws_user_base=ws,
    )


def account(
    account_id: str,
    *,
    role: str = "follower",
    enabled: int = 1,
    execution_mode: str = "REAL",
) -> dict:
    return {
        "id": account_id,
        "role": role,
        "enabled": enabled,
        "execution_mode": execution_mode,
    }


def test_disabled_shadow_row_does_not_block_restart():
    validate_shadow_boot(
        cfg(shadow_enabled=False, rest="https://lookalike.invalid", ws="wss://main.invalid/ws"),
        [account("main", role="master"), account("shadow-old", enabled=0, execution_mode="SHADOW")],
    )


def test_enabled_shadow_requires_exact_official_testnet_urls():
    validate_shadow_boot(
        cfg(shadow_enabled=True),
        [account("main", role="master"), account("shadow-1", execution_mode="SHADOW")],
    )
    with pytest.raises(RuntimeError, match="official Aster testnet"):
        validate_shadow_boot(
            cfg(
                shadow_enabled=True,
                rest="https://fapi.asterdex-testnet.com.evil.example",
            ),
            [account("main", role="master"), account("shadow-1", execution_mode="SHADOW")],
        )
    with pytest.raises(RuntimeError, match="official Aster testnet"):
        validate_shadow_boot(
            cfg(shadow_enabled=True, ws="wss://fstream.asterdex.com/ws"),
            [account("main", role="master"), account("shadow-1", execution_mode="SHADOW")],
        )


def test_runtime_factory_keeps_shadow_off_real_client():
    app = App.__new__(App)
    app.cfg = cfg(shadow_enabled=True)
    app.shadow_exchange = ShadowExchange(hedge=False)
    real_client = object()
    app._make_client = lambda: real_client

    shadow = app.runtime_for_row(account("shadow-1", execution_mode="SHADOW"))
    real = app.runtime_for_row(account("copyc1"))

    assert shadow.client is app.shadow_exchange
    assert shadow.execution_mode == "SHADOW"
    assert real.client is real_client
    assert real.execution_mode == "REAL"


@pytest.mark.asyncio
async def test_stop_is_concurrent_safe_and_closes_resources_once(tmp_path):
    app = App(
        Settings(
            encryption_key="ab" * 32,
            db_path=tmp_path / "app.sqlite3",
            log_path=tmp_path / "logs" / "app.log",
        )
    )
    calls = {"http": 0, "db": 0}

    async def close_http():
        calls["http"] += 1

    async def close_db():
        calls["db"] += 1

    app.http.aclose = close_http
    app.db.close = close_db
    app._ready = True

    await asyncio.gather(app.stop(), app.stop(), app.stop())
    await app.stop()

    assert calls == {"http": 1, "db": 1}
    assert app._ready is False
    assert app.stop_event.is_set()
    assert app._sweep_task is None
    assert app._ingest_task is None
    assert app._heartbeat_task is None
    assert app._stream_task is None
    assert app._leverage_task is None


@pytest.mark.asyncio
async def test_stop_stops_live_leverage_worker_before_closing_resources(tmp_path):
    """The leverage worker is a managed task: a live one must be stopped and
    awaited BEFORE the shared HTTP/DB resources close (review finding: the
    second task_names assignment used to drop it)."""
    app = App(
        Settings(
            encryption_key="ab" * 32,
            db_path=tmp_path / "app.sqlite3",
            log_path=tmp_path / "logs" / "app.log",
        )
    )
    order: list[str] = []

    async def worker():
        try:
            await app.stop_event.wait()
        finally:
            order.append("leverage-stopped")

    app._leverage_task = asyncio.create_task(worker())

    async def close_http():
        order.append("http-closed")

    async def close_db():
        order.append("db-closed")

    app.http.aclose = close_http
    app.db.close = close_db
    app._ready = True

    await app.stop()

    assert app._leverage_task is None
    assert "leverage-stopped" in order
    assert order.index("leverage-stopped") < order.index("db-closed")


@pytest.mark.asyncio
async def test_reopen_paused_admission_never_enables_durable_entry_gate(tmp_path):
    app = App(
        Settings(
            encryption_key="ab" * 32,
            db_path=tmp_path / "pause.sqlite3",
            log_path=tmp_path / "logs" / "pause.log",
        )
    )
    await app.db.connect()
    await app.db.migrate()
    observed: list[str | None] = []

    async def open_admission():
        observed.append(await app.settings.get_flag("allow_new_entries"))

    app.coordinator.open_generation_admission = open_admission
    await app.settings.set_flag("allow_new_entries", "false")
    await app.reopen_entries_while_paused()

    assert observed == ["false"]
    assert await app.settings.get_flag("allow_new_entries") == "false"

    await app.settings.set_flag("allow_new_entries", "true")
    with pytest.raises(RuntimeError, match="entries are enabled"):
        await app.reopen_entries_while_paused()
    assert observed == ["false"]
    await app.stop()


class _FlatCoordinator:
    def __init__(self):
        self.snapshot_calls = 0
        self.invalidated = False

    async def snapshot_follower_locked(self, runtime):
        if self.invalidated:
            raise AssertionError("credential cache was invalidated; no second snapshot allowed")
        self.snapshot_calls += 1
        return AccountState(runtime.account_id, Decimal(0), {}, [])


class _NoopCommands:
    async def in_flight(self):
        return []


class _NoopExecutor:
    @asynccontextmanager
    async def mutation_context(self, account_id):
        del account_id
        yield


def _lifecycle_test_app():
    app = App.__new__(App)
    app.follower_registry = FollowerRegistry([FollowerRuntime("copyc1", object(), identity=None)])
    app.coordinator = _FlatCoordinator()
    app.commands = _NoopCommands()
    app.executor = _NoopExecutor()
    return app


@pytest.mark.asyncio
async def test_unpublish_after_commit_does_not_resnapshot_after_cache_invalidation():
    app = _lifecycle_test_app()
    coordinator = app.coordinator

    async with app.follower_lifecycle("copyc1") as lifecycle:
        await lifecycle.assert_safe_to_remove()
        coordinator.invalidated = True  # simulated durable commit/cache removal
        removed = await lifecycle.unpublish_after_commit()

    assert removed.account_id == "copyc1"
    assert coordinator.snapshot_calls == 1
    assert app.follower_registry.get("copyc1") is None


@pytest.mark.asyncio
async def test_unpublish_after_commit_requires_recorded_precondition():
    app = _lifecycle_test_app()

    async with app.follower_lifecycle("copyc1") as lifecycle:
        with pytest.raises(RuntimeError, match="successful assert_safe_to_remove"):
            await lifecycle.unpublish_after_commit()

    assert app.follower_registry.get("copyc1") is not None


@pytest.mark.asyncio
async def test_app_accepts_structured_notifier_callback_and_emits_explicit_event(tmp_path):
    events: list[NotificationEvent] = []

    async def collect(event: NotificationEvent) -> None:
        events.append(event)

    app = App(
        Settings(
            encryption_key="ab" * 32,
            db_path=tmp_path / "notify.sqlite3",
            log_path=tmp_path / "logs" / "notify.log",
        ),
        collect,
    )
    await app._emit_notification(
        event_key="app:test:ready",
        kind="engine.ready",
        severity=NotificationSeverity.INFO,
        text="plain ready",
        account_id="copyc1",
    )
    await app.stop()

    assert events[0].event_key == "app:test:ready"
    assert events[0].account_id == "copyc1"
    assert events[0].kind == "engine.ready"
    assert events[0].severity == NotificationSeverity.INFO
    assert events[0].text == "plain ready"


@pytest.mark.asyncio
async def test_app_confirmation_isolated_notifier_failure_still_starts_postfill():
    class BrokenNotifier:
        async def emit(self, event):
            del event
            raise RuntimeError("outbox unavailable")

    class Coordinator:
        def __init__(self):
            self.confirmed = []

        async def on_command_confirmed(self, account_id, action):
            self.confirmed.append((account_id, action))

    app = App.__new__(App)
    app.notifier = BrokenNotifier()
    app.coordinator = Coordinator()
    action = ExecutionAction(
        kind=ActionKind.MARKET_OPEN,
        symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT"},
        client_order_id="ct-test",
        replaces_client_order_id=None,
        priority=1,
    )

    await app._on_confirmed("copyc1", action, "abc123")

    assert app.coordinator.confirmed == [("copyc1", action)]


def test_app_composes_leverage_control_plane(tmp_path):
    """The coordinator must read leverage from the shared RAM control-plane,
    never from per-client TTL caches on the trading path."""
    from copytradeaster.trading.leverage import LeverageManager

    app = App(
        Settings(
            encryption_key="ab" * 32,
            db_path=tmp_path / "lev.sqlite3",
            log_path=tmp_path / "logs" / "lev.log",
        )
    )
    assert isinstance(app.leverage, LeverageManager)
    assert app.coordinator._leverage is app.leverage  # noqa: SLF001
    assert app.leverage._symbols == tuple(  # noqa: SLF001
        dict.fromkeys(s.strip().upper() for s in app.cfg.symbol_whitelist)
    )


@pytest.mark.asyncio
async def test_stream_startup_timeout_selects_real_rest_fallback(monkeypatch):
    app = App.__new__(App)
    app._stream_ready = asyncio.Event()
    app._notification_token = "test-token"
    events: list[NotificationEvent] = []

    async def collect(event_key, kind, severity, text, account_id=None):
        events.append(
            NotificationEvent(
                event_key=event_key,
                kind=kind,
                severity=severity,
                text=text,
                account_id=account_id,
            )
        )

    app._emit_notification = collect
    monkeypatch.setattr(app_module, "STREAM_READY_TIMEOUT_S", 0.001)

    assert await app._wait_for_stream_startup() is False
    assert events[0].kind == "engine.stream_fallback"
    assert events[0].account_id == "main"


def _observe_app():
    from copytradeaster.aster.models import OrderDTO
    from tests.unit.aster.test_trading_api import ORDER_RESPONSE

    app = App.__new__(App)
    app.cfg = SimpleNamespace(dry_run=False, rest_base_url="https://fapi.asterdex-testnet.com")
    app.follower_registry = SimpleNamespace(
        get=lambda account_id: SimpleNamespace(execution_mode="REAL")
    )
    observed = {}

    async def observe_order(scope, order, *, kind, command_key, observed_at):
        observed["scope"] = scope

    app.history = SimpleNamespace(observe_order=observe_order)
    app.history_mark_dirty = None
    order = OrderDTO.model_validate({**ORDER_RESPONSE, "symbol": "BTCUSDT"})
    params = {"account_id": "copy1", "command_key": "cmd-1", "kind": "MARKET_OPEN"}
    return app, order, params, observed


@pytest.mark.asyncio
async def test_observe_history_order_marks_symbol_dirty():
    app, order, params, observed = _observe_app()
    hints = []
    app.history_mark_dirty = lambda a, e, s: hints.append((a, e, s))
    await app._observe_history_order(order=order, **params)
    assert observed["scope"].account_id == "copy1"
    assert hints == [("copy1", "testnet", "BTCUSDT")]


@pytest.mark.asyncio
async def test_observe_history_order_survives_broken_hint():
    app, order, params, observed = _observe_app()

    def broken(account_id, environment, symbol):
        raise RuntimeError("hint sink down")

    app.history_mark_dirty = broken
    await app._observe_history_order(order=order, **params)
    assert observed["scope"].account_id == "copy1"
