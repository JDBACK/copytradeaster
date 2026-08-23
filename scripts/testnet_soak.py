"""Two-hour signed Aster testnet soak for the Stage 1 corrective gate.

The campaign deliberately trades only a few minimum-size cycles.  Long idle
periods exercise the real App heartbeat, user stream and periodic reconciliation
without turning a soak into an unnecessary order-volume test.  A run includes
one forced listen-key reconnect, one graceful restart and one real SIGKILL of a
child App followed by stale-heartbeat detection and recovery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hybrid_load_test import (
    SYMBOL,
    _audit,
    _cancel_master_order,
    _cleanup,
    _clone_real_accounts,
    _follower_protection_complete,
    _position,
    _preflight,
    _snap_down,
    _submit_master_order,
    _wait_follower_state_with_recovery,
    _wait_until,
)

from copytradeaster.app import App
from copytradeaster.aster.trading_api import PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import SettingsRepo

MIN_ACCEPTED_DURATION_S = 2 * 60 * 60
DEFAULT_CYCLES = 6
DEFAULT_QTY = Decimal("0.002")
STOP_DISTANCE = Decimal("3000")
MOVED_STOP_DISTANCE = Decimal("2800")
TP_DISTANCE = Decimal("3000")
MOVED_TP_DISTANCE = Decimal("2800")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="two-hour signed Stage 1 testnet soak")
    parser.add_argument("--source-db", type=Path, default=None)
    parser.add_argument("--run-db", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--duration", type=int, default=MIN_ACCEPTED_DURATION_S)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--quantity", type=Decimal, default=DEFAULT_QTY)
    return parser


def _new_app(cfg: Settings) -> App:
    app = App(cfg)
    # Durable journal + the JSON report are the evidence. Console notifications
    # add noise during a long unattended run and are not part of the contract.
    app.coordinator._on_notify = None
    return app


async def _follower_qty(app: App) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for fr in app.followers:
        positions = await fr.client.position_risk(fr.account_id, SYMBOL)
        result[fr.account_id] = sum(
            (abs(position.position_amt) for position in positions if position.position_amt != 0),
            Decimal(0),
        )
    return result


async def _slots_match(
    app: App,
    *,
    stop_price: Decimal,
    tp_price: Decimal | None,
) -> bool:
    for fr in app.followers:
        positions, orders = await asyncio.gather(
            fr.client.position_risk(fr.account_id, SYMBOL),
            fr.client.open_orders(fr.account_id, SYMBOL),
        )
        legs = [position for position in positions if position.position_amt != 0]
        if len(legs) != 1:
            return False
        bot_orders = [order for order in orders if order.client_order_id.startswith("ct")]
        stops = [order for order in bot_orders if order.type in {"STOP_MARKET", "STOP"}]
        tps = [order for order in bot_orders if order.type in {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}]
        if len(stops) != 1 or stops[0].stop_price != stop_price:
            return False
        if tp_price is None:
            if tps:
                return False
        elif len(tps) != 1 or tps[0].stop_price != tp_price:
            return False
    return True


def _retry_stats() -> dict[str, int]:
    return {
        "unknown_outcomes": 0,
        "resolved_by_query": 0,
        "resolved_by_position": 0,
        "partial_residuals": 0,
        "retries": 0,
        "authoritative_recoveries": 0,
    }


async def _cycle(app: App, cycle: int, quantity: Decimal, timeout: float = 60.0) -> dict:
    main = app.master_client
    filters = (await main.exchange_info(force=True)).tradable_filters()[SYMBOL]
    quantity = _snap_down(quantity, filters.step_size)
    half = _snap_down(quantity / 2, filters.step_size)
    if quantity < filters.min_qty or half < filters.min_qty:
        raise RuntimeError(f"quantity {quantity} cannot execute the resize phase")
    hedge = await main.get_position_mode("main")
    position_side = PosSide.LONG if hedge else PosSide.BOTH
    stats = _retry_stats()
    stamp = time.time_ns()
    started = time.monotonic()

    await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=quantity,
            position_side=position_side,
            new_order_resp_type="RESULT",
            new_client_order_id=f"soak-open-{cycle}-{stamp}"[:36],
        ),
        stats,
    )
    position = (
        await _wait_until(lambda: _position(main, "main"), timeout, f"soak master open {cycle}")
    )[0]
    entry_price = position.entry_price
    stop_price = _snap_down(entry_price - STOP_DISTANCE, filters.tick_size)
    stop_coid = await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=abs(position.position_amt),
            stop_price=stop_price,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"soak-stop-{cycle}-{stamp}"[:36],
        ),
        stats,
    )
    await _wait_follower_state_with_recovery(
        app,
        main,
        want_open=True,
        want_protection=True,
        timeout=timeout,
        description=f"soak follower entry {cycle}",
        retry_stats=stats,
    )
    await _wait_until(
        lambda: _slots_match(app, stop_price=stop_price, tp_price=None),
        timeout,
        f"soak initial stop slots {cycle}",
    )
    initial_qty = await _follower_qty(app)

    await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=half,
            reduce_only=not hedge,
            position_side=position_side,
            new_order_resp_type="RESULT",
            new_client_order_id=f"soak-reduce-{cycle}-{stamp}"[:36],
        ),
        stats,
    )

    async def reduced() -> bool:
        current = await _follower_qty(app)
        return all(Decimal(0) < current[aid] < qty for aid, qty in initial_qty.items())

    await _wait_until(reduced, timeout, f"soak follower reduce {cycle}")
    reduced_qty = await _follower_qty(app)
    if not await _follower_protection_complete(app):
        raise RuntimeError(f"incomplete protection after reduce, soak cycle {cycle}")

    await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            type="MARKET",
            quantity=half,
            position_side=position_side,
            new_order_resp_type="RESULT",
            new_client_order_id=f"soak-grow-{cycle}-{stamp}"[:36],
        ),
        stats,
    )

    async def grown() -> bool:
        current = await _follower_qty(app)
        return all(current[aid] > qty for aid, qty in reduced_qty.items())

    await _wait_until(grown, timeout, f"soak follower grow {cycle}")
    if not await _follower_protection_complete(app):
        raise RuntimeError(f"incomplete protection after grow, soak cycle {cycle}")

    moved_stop = _snap_down(entry_price - MOVED_STOP_DISTANCE, filters.tick_size)
    new_stop_coid = await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="STOP_MARKET",
            quantity=quantity,
            stop_price=moved_stop,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"soak-stop2-{cycle}-{stamp}"[:36],
        ),
        stats,
    )
    await _cancel_master_order(main, SYMBOL, stop_coid, stats)
    await _wait_until(
        lambda: _slots_match(app, stop_price=moved_stop, tp_price=None),
        timeout,
        f"soak moved stop {cycle}",
    )

    tp_price = _snap_down(entry_price + TP_DISTANCE, filters.tick_size)
    tp_coid = await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            stop_price=tp_price,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"soak-tp-{cycle}-{stamp}"[:36],
        ),
        stats,
    )
    await _wait_until(
        lambda: _slots_match(app, stop_price=moved_stop, tp_price=tp_price),
        timeout,
        f"soak initial TP {cycle}",
    )

    moved_tp = _snap_down(entry_price + MOVED_TP_DISTANCE, filters.tick_size)
    new_tp_coid = await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            stop_price=moved_tp,
            reduce_only=not hedge,
            position_side=position_side,
            working_type="MARK_PRICE",
            new_client_order_id=f"soak-tp2-{cycle}-{stamp}"[:36],
        ),
        stats,
    )
    await _cancel_master_order(main, SYMBOL, tp_coid, stats)
    await _wait_until(
        lambda: _slots_match(app, stop_price=moved_stop, tp_price=moved_tp),
        timeout,
        f"soak moved TP {cycle}",
    )

    await _cancel_master_order(main, SYMBOL, new_stop_coid, stats)
    await _cancel_master_order(main, SYMBOL, new_tp_coid, stats)
    position = (await _position(main, "main"))[0]
    await _submit_master_order(
        main,
        PlaceOrderRequest(
            symbol=SYMBOL,
            side=Side.SELL,
            type="MARKET",
            quantity=abs(position.position_amt),
            reduce_only=not hedge,
            position_side=position_side,
            new_order_resp_type="RESULT",
            new_client_order_id=f"soak-close-{cycle}-{stamp}"[:36],
        ),
        stats,
    )
    await _wait_follower_state_with_recovery(
        app,
        main,
        want_open=False,
        want_protection=False,
        timeout=timeout,
        description=f"soak follower flat {cycle}",
        retry_stats=stats,
    )
    return {
        "cycle": cycle,
        "elapsed_s": round(time.monotonic() - started, 3),
        "master_retry_stats": stats,
    }


async def _flags(db_path: Path) -> dict[str, str | None]:
    db = Database(db_path)
    await db.connect()
    try:
        repo = SettingsRepo(db)
        return {
            key: await repo.get_flag(key)
            for key in ("runtime_id", "heartbeat_at", "ready", "degraded_active")
        }
    finally:
        await db.close()


async def _wait_child_ready(db_path: Path, timeout: float = 90.0) -> dict[str, str | None]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await _flags(db_path)
        heartbeat = int(state["heartbeat_at"] or 0)
        if state["ready"] == "true" and int(time.time() * 1000) - heartbeat < 15_000:
            return state
        await asyncio.sleep(1)
    raise TimeoutError("child App did not become READY")


async def _kill_and_recover(cfg: Settings) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "CT_DB_PATH": str(cfg.db_path.resolve()),
            "CT_LOG_LEVEL": "WARNING",
            "CT_POSITION_MODE": cfg.position_mode,
            # pydantic-settings decodes complex env values as JSON before the
            # field validator runs.
            "CT_FOLLOWER_SNAPSHOT_SYMBOLS": json.dumps([SYMBOL]),
        }
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "copytradeaster",
        "run",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready_state = await _wait_child_ready(cfg.db_path)
    except Exception as exc:
        if child.returncode is None:
            child.kill()
        _stdout, stderr = await child.communicate()
        detail = stderr.decode(errors="replace")[-1000:] if stderr else ""
        raise RuntimeError(f"child App failed to become READY: {detail}") from exc
    child.kill()  # real SIGKILL: no App.stop(), READY remains persisted until stale
    return_code = await child.wait()
    stale_after_s = max(2 * cfg.reconcile_interval_s, 30) + 2
    await asyncio.sleep(stale_after_s)
    stale_state = await _flags(cfg.db_path)
    heartbeat = int(stale_state["heartbeat_at"] or 0)
    heartbeat_age_ms = int(time.time() * 1000) - heartbeat
    if stale_state["ready"] != "true" or heartbeat_age_ms <= stale_after_s * 1000:
        raise RuntimeError(
            f"SIGKILL stale-heartbeat proof failed: state={stale_state}, age={heartbeat_age_ms}"
        )
    return {
        "child_ready": ready_state,
        "return_code": return_code,
        "stale_state": stale_state,
        "heartbeat_age_ms": heartbeat_age_ms,
    }


async def run(args: argparse.Namespace) -> dict:
    if os.environ.get("CT_TESTNET") != "1" or os.environ.get("CT_SOAK") != "1":
        raise RuntimeError("signed soak requires CT_TESTNET=1 and CT_SOAK=1")
    if args.duration <= 0 or args.cycles < 2 or args.quantity <= 0:
        raise ValueError("duration/quantity must be positive and cycles must be >= 2")
    cfg = Settings()
    if "testnet" not in cfg.rest_base_url.lower() or "testnet" not in cfg.ws_user_base.lower():
        raise RuntimeError("soak is restricted to Aster testnet REST and WS URLs")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    source_db = args.source_db or cfg.db_path
    run_db = args.run_db or Path("data") / f"testnet-soak-{stamp}.sqlite3"
    report_path = args.report or run_db.with_suffix(".json")
    await _clone_real_accounts(cfg, source_db, run_db)
    original_modes = await _preflight(cfg, run_db)
    run_cfg = cfg.model_copy(
        update={
            "db_path": run_db,
            "log_level": "WARNING",
            "follower_snapshot_symbols": [SYMBOL],
        }
    )

    app: App | None = _new_app(run_cfg)
    started_at = datetime.now(UTC)
    started_mono = time.monotonic()
    events: list[dict] = []
    cycles: list[dict] = []
    audits: list[dict] = []
    cleanup: dict = {"errors": ["not-run"]}
    result: dict = {}
    try:
        await app.start()
        events.append({"event": "initial-ready", "at_s": 0})
        cycle_offsets = [args.duration * index / args.cycles for index in range(args.cycles)]
        reconnect_at = args.duration * 0.27
        restart_at = args.duration * 0.45
        kill_at = args.duration * 0.65
        next_audit = 0.0
        cycle_index = 0
        reconnect_done = restart_done = kill_done = False

        while True:
            elapsed = time.monotonic() - started_mono
            if cycle_index < args.cycles and elapsed >= cycle_offsets[cycle_index]:
                cycle = await _cycle(app, cycle_index + 1, args.quantity)
                cycle["campaign_at_s"] = round(time.monotonic() - started_mono, 3)
                cycles.append(cycle)
                cycle_index += 1
                print(json.dumps({"soak_cycle": cycle}, sort_keys=True), flush=True)
                continue
            if not reconnect_done and elapsed >= reconnect_at:
                before = app._stream_reconnect_count
                await app.master_client.close_listen_key("main")

                async def reconnected(current_app=app, old_count=before) -> bool:
                    return current_app._stream_reconnect_count > old_count

                await _wait_until(
                    reconnected,
                    90,
                    "soak forced reconnect",
                )
                reconnect_done = True
                events.append({"event": "forced-reconnect", "at_s": round(elapsed, 3)})
                continue
            if not restart_done and elapsed >= restart_at:
                await app.stop()
                app = _new_app(run_cfg)
                await app.start()
                restart_done = True
                events.append({"event": "graceful-restart", "at_s": round(elapsed, 3)})
                continue
            if not kill_done and elapsed >= kill_at:
                await app.stop()
                app = None
                crash = await _kill_and_recover(run_cfg)
                app = _new_app(run_cfg)
                await app.start()
                kill_done = True
                events.append(
                    {"event": "sigkill-recovery", "at_s": round(elapsed, 3), "proof": crash}
                )
                continue
            if elapsed >= next_audit:
                audit = await _audit(app)
                dirty = {
                    account_id: state
                    for account_id, state in audit.items()
                    if state["positions"] or state["open_orders"]
                }
                if dirty:
                    raise RuntimeError(f"idle soak audit is not flat/order-free: {dirty}")
                audits.append({"at_s": round(elapsed, 3), "accounts": len(audit)})
                next_audit = elapsed + 300
            if elapsed >= args.duration and cycle_index == args.cycles:
                break
            await asyncio.sleep(min(10.0, max(args.duration - elapsed, 0.1)))

        final_audit = await _audit(app)
        command_rows = await app.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM execution_commands GROUP BY status"
        )
        command_counts = {str(row["status"]): int(row["n"]) for row in command_rows}
        in_flight = await app.commands.in_flight()
        if any(state["positions"] or state["open_orders"] for state in final_audit.values()):
            raise RuntimeError(f"final soak audit is dirty: {final_audit}")
        if in_flight or any(status != "CONFIRMED" for status in command_counts):
            raise RuntimeError(
                "final soak journal is not terminal: "
                f"counts={command_counts}, in_flight={len(in_flight)}"
            )
        result = {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "duration_requested_s": args.duration,
            "duration_actual_s": round(time.monotonic() - started_mono, 3),
            "accepted_two_hour_duration": args.duration >= MIN_ACCEPTED_DURATION_S,
            "run_db": str(run_db),
            "followers": [fr.account_id for fr in app.followers],
            "cycles": cycles,
            "events": events,
            "periodic_flat_audits": audits,
            "stream_connect_count_final_runtime": app._stream_connect_count,
            "stream_reconnect_count_final_runtime": app._stream_reconnect_count,
            "ingest_dropped_final_runtime": app._ingest_dropped,
            "command_counts_before_cleanup": command_counts,
            "in_flight_before_cleanup": len(in_flight),
            "audit_before_cleanup": final_audit,
        }
    except Exception as exc:
        result = {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "duration_requested_s": args.duration,
            "duration_actual_s": round(time.monotonic() - started_mono, 3),
            "run_db": str(run_db),
            "cycles": cycles,
            "events": events,
            "periodic_flat_audits": audits,
            "error": f"{type(exc).__name__}: {exc}",
        }
        raise
    finally:
        if app is None:
            # Recovery/cleanup is still mandatory if orchestration itself
            # failed after stopping the parent App.
            try:
                recovery_app = _new_app(run_cfg)
                await recovery_app.start()
                app = recovery_app
            except Exception as exc:  # noqa: BLE001 - preserve original failure
                app = None
                result.setdefault("cleanup", {"errors": []})["errors"].append(
                    f"could not start cleanup App: {exc}"
                )
        if app is not None:
            cleanup = await _cleanup(app, original_modes)
            result["cleanup"] = cleanup
            result["audit_after_cleanup"] = await _audit(app)
            await app.stop()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


async def _main() -> int:
    result = await run(_parser().parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not result.get("cleanup", {}).get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
