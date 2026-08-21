"""CLI: init-db, import-wallet, approve-builder, run, status, reconcile,
pause/resume, emergency-close-all."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from copytradeaster.config import Settings
from copytradeaster.logging_utils import redact
from copytradeaster.storage.repositories import SettingsRepo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copytradeaster", description="AsterDex copy-trade engine (stage 1)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create/migrate the SQLite database")

    p_import = sub.add_parser("import-wallet", help="import wallet.txt blocks")
    p_import.add_argument("file", type=Path)
    p_import.add_argument("--wipe", action="store_true", help="replace credentials")

    p_approve = sub.add_parser("approve-builder", help="approveAgent+approveBuilder run")
    p_approve.add_argument("--accounts", default="all", help="all or comma-separated IDs")
    p_approve.add_argument("--max-fee-rate", default=None)

    sub.add_parser("run", help="run the engine until Ctrl+C")
    sub.add_parser("status", help="show engine status")
    p_reconcile = sub.add_parser("reconcile", help="force reconciliation sweep")
    p_reconcile.add_argument("--account", default=None)

    sub.add_parser("pause", help="block new entries on all followers")
    sub.add_parser("resume", help="allow new entries again")
    p_close = sub.add_parser("emergency-close-all", help="flatten every follower")
    p_close.add_argument("--yes", action="store_true", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


async def _dispatch(args) -> int:
    if args.command == "init-db":
        return await _init_db()
    if args.command == "import-wallet":
        return await _import_wallet(args.file, wipe=args.wipe)
    if args.command == "approve-builder":
        return await _approve_builder(args.accounts, args.max_fee_rate)
    if args.command == "run":
        return await _run()
    if args.command == "status":
        return await _status()
    if args.command == "reconcile":
        return await _reconcile(args.account)
    if args.command == "pause":
        return await _set_pause(True)
    if args.command == "resume":
        return await _set_pause(False)
    if args.command == "emergency-close-all":
        if not args.yes:
            print("refusing without --yes", file=sys.stderr)
            return 2
        return await _emergency_close_all()
    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


def _load_settings() -> Settings:
    return Settings()


def _domain_imports():
    from copytradeaster.domain.models import (
        ActionKind,
        ExecutionAction,
        ExecutionPlan,
        PosSide,
        TargetPosition,
        TargetState,
    )

    return ActionKind, ExecutionAction, ExecutionPlan, PosSide, TargetPosition, TargetState


async def _init_db() -> int:
    from copytradeaster.storage.database import Database

    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    applied = await db.migrate()
    await db.close()
    print(f"database ready at {cfg.db_path} (migrations applied: {applied})")
    return 0


# --- wallet.txt parser ------------------------------------------------------------


def parse_wallet_file(path: Path) -> list[dict]:
    """Blocks separated by blank lines. Fields (case-insensitive):
    '<label>' (first line), 'evm adress:', 'api wallet adress:', 'privte key:',
    optional 'main privte key:'. The literal 'adress' typo is part of the format.
    """
    text = path.read_text(encoding="utf-8")
    blocks: list[dict] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("evm adress:") and current is not None:
            current["user"] = _after_colon(line)
        elif lowered.startswith("api wallet adress:") and current is not None:
            current["signer_address"] = _after_colon(line)
        elif lowered.startswith("main privte key:") and current is not None:
            current["main_key"] = _after_colon(line)
        elif lowered.startswith("privte key:") and current is not None:
            current["signer_key"] = _after_colon(line)
        else:
            # a non-field line is the label of a NEW block
            if current is not None and current.get("signer_key"):
                blocks.append(current)
                current = None
            if current is None:
                current = {"label": line}
    if current is not None and current.get("signer_key"):
        blocks.append(current)
    return [b for b in blocks if b.get("user") or b.get("signer_key")]


def _after_colon(line: str) -> str:
    return line.split(":", 1)[1].strip()


async def _import_wallet(path: Path, wipe: bool = False) -> int:
    from copytradeaster.credentials.service import SqliteCredentials
    from copytradeaster.storage.database import Database

    blocks = parse_wallet_file(path)
    if not blocks:
        print("no wallet blocks found", file=sys.stderr)
        return 2
    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    creds = SqliteCredentials(db, cfg.encryption_key_bytes())
    imported = 0
    for block in blocks:
        label = block.get("label", "").strip()
        user = block.get("user", "")
        signer_key = block.get("signer_key", "")
        main_key = block.get("main_key")
        if not (label and user and signer_key):
            print(f"skip incomplete block {label!r}", file=sys.stderr)
            continue
        role = "master" if label.lower().startswith("main") else "follower"
        account_id = "main" if role == "master" else label
        if wipe:
            from copytradeaster.storage.repositories import CredentialRepo

            await db.execute("DELETE FROM credentials WHERE account_id=?", (account_id,))
            await db.conn.commit()
            del CredentialRepo
        await creds.import_identity(
            account_id=account_id,
            user=user,
            signer_private_key_hex=signer_key,
            role=role,
            label=label,
            main_private_key_hex=main_key,
        )
        signer_address = block.get("signer_address", "")
        print(
            f"imported {label}: role={role} user={redact(user)} "
            f"signer={redact(signer_address or '0x0')} main_key={'yes' if main_key else 'no'}"
        )
        imported += 1
    await db.close()
    print(f"imported {imported} accounts")
    return 0 if imported == len(blocks) else 1


# --- approve-builder -----------------------------------------------------------------


async def _approve_builder(accounts: str, max_fee_rate: str | None) -> int:
    import httpx

    from copytradeaster.aster.management_api import ManagementClient
    from copytradeaster.aster.rate_limits import RateLimits

    cfg = _load_settings()
    db_path = cfg.db_path
    from copytradeaster.storage.database import Database

    db = Database(db_path)
    await db.connect()
    await db.migrate()
    from copytradeaster.credentials.service import SqliteCredentials

    creds = SqliteCredentials(db, cfg.encryption_key_bytes())
    await creds.load()
    wanted = None if accounts == "all" else [a.strip() for a in accounts.split(",") if a.strip()]
    targets = creds.account_ids() if wanted is None else wanted
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    limiter = RateLimits(
        cfg.weight_limit_per_min, cfg.orders_limit_per_min, cfg.orders_limit_per_10s
    )
    client = ManagementClient(
        http=http,
        base_url=cfg.rest_base_url,
        limiter=limiter,
        identities=creds,
        keys=creds,
        nonces=creds,
        cfg=cfg,
    )
    failures = 0
    import time

    expired_ms = int(time.time() * 1000) + 365 * 24 * 3600 * 1000
    for account_id in targets:
        if not creds.has_main_key(account_id):
            print(f"[{account_id}] WARN: no main private key - skipped")
            failures += 1
            continue
        try:
            resp = await client.approve_agent(
                account_id,
                agent_name=cfg.builder_name,
                expired_ms=expired_ms,
                with_builder=bool(cfg.builder_address),
            )
            print(f"[{account_id}] approveAgent -> {json.dumps(resp)}")
            resp2 = await client.approve_builder(account_id, max_fee_rate=max_fee_rate)
            print(f"[{account_id}] approveBuilder -> {json.dumps(resp2)}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"[{account_id}] FAILED: {exc}")
            failures += 1
    await http.aclose()
    await db.close()
    return 1 if failures else 0


# --- run/status/reconcile/pause/close ---------------------------------------------------


async def _run() -> int:
    from copytradeaster.app import App

    app = App(_load_settings())
    await app.run_until_stopped()
    return 0


async def _status() -> int:
    from copytradeaster.storage.database import Database

    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    settings = SettingsRepo(db)
    ready = await settings.get_flag("ready")
    degraded = await settings.get_flag("degraded")
    paused = await settings.get_flag("allow_new_entries")
    print(
        f"ready={ready or 'false'} degraded={degraded or '-'} "
        f"allow_new_entries={paused if paused is not None else 'true'}"
    )
    trades = await db.fetchall("SELECT * FROM copy_trades WHERE status='OPEN' ORDER BY trade_id")
    for t in trades:
        print(f"trade #{t['trade_id']} {t['symbol']} {t['pos_side']} opened {t['opened_at']}")
    commands = await db.fetchall(
        "SELECT * FROM execution_commands ORDER BY updated_at DESC LIMIT 10"
    )
    for c in commands:
        print(
            f"cmd {c['command_key']} {c['kind']} {c['status']} "
            f"attempts={c['attempts']} {c['last_error'] or ''}"
        )
    accounts = await db.fetchall("SELECT id, role, label, enabled FROM accounts ORDER BY id")
    for a in accounts:
        print(f"account {a['id']} role={a['role']} enabled={a['enabled']}")
    await db.close()
    return 0


async def _reconcile(account: str | None) -> int:
    from copytradeaster.app import App

    app = App(_load_settings())
    try:
        await app.db.connect()
        await app.db.migrate()
        await app.credentials.load()
        accounts = await app.db.fetchall("SELECT id FROM accounts WHERE role='follower'")
        for row in accounts:
            app.followers.append(_follower_runtime(app, str(row["id"])))
        exchange = await app.master_client.exchange_info(force=True)
        app._filters_cache = exchange.tradable_filters()
        await app.projector.rebuild_full(app.master_client, "main")
        ids = [account] if account else None
        report = await app.reconciler.reconcile(app.projector.state, account_ids=ids, reason="cli")
        for aid, created in report.per_account.items():
            print(f"{aid}: commands created: {created}")
    finally:
        from copytradeaster.storage.database import Database

        await app.http.aclose()
        await app.db.close()
        del Database
    return 0


def _follower_runtime(app, account_id: str):
    from copytradeaster.trading.coordinator import FollowerRuntime

    return FollowerRuntime(account_id=account_id, client=app._make_client(), identity=None)


async def _set_pause(paused: bool) -> int:
    from copytradeaster.storage.database import Database

    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    await SettingsRepo(db).set_flag("allow_new_entries", "false" if paused else "true")
    await db.close()
    print("paused" if paused else "resumed")
    return 0


async def _emergency_close_all() -> int:
    from copytradeaster.app import App
    from copytradeaster.domain.models import (
        TargetPosition,
    )

    app = App(_load_settings())
    try:
        await app.db.connect()
        await app.db.migrate()
        await app.credentials.load()
        accounts = await app.db.fetchall("SELECT id FROM accounts WHERE role='follower'")
        for row in accounts:
            app.followers.append(_follower_runtime(app, str(row["id"])))
        exchange = await app.master_client.exchange_info(force=True)
        app._filters_cache = exchange.tradable_filters()
        for fr in app.followers:
            actual = await app.coordinator._snapshot(fr)
            targets = [
                TargetPosition(
                    key=key, qty=0, stop_price=None, tp_price=None, leverage=pos.leverage
                )
                for key, pos in actual.positions.items()
            ]
            plan = build_close_plan(fr.account_id, actual, targets)
            if plan.actions:
                results = await app.executor.submit(plan)
                print(f"[{fr.account_id}] closed: {[r.status for r in results]}")
            else:
                print(f"[{fr.account_id}] already flat")
    finally:
        await app.http.aclose()
        await app.db.close()
    return 0


def build_close_plan(account_id, actual, targets):
    """Emergency plan: cancel protections then close full remainder."""
    from copytradeaster.domain.models import (
        ActionKind,
        ExecutionAction,
        ExecutionPlan,
        PosSide,
    )

    actions = []
    seq = 0
    for tp in targets:
        pos = actual.positions.get(tp.key)
        if pos is None:
            continue
        for prot in (pos.stop, pos.take_profit):
            if prot is not None:
                actions.append(
                    ExecutionAction(
                        kind=ActionKind.CANCEL_ORDER,
                        symbol=tp.key.symbol,
                        payload={
                            "symbol": tp.key.symbol,
                            "origClientOrderId": prot.client_order_id,
                        },
                        client_order_id=None,
                        replaces_client_order_id=prot.client_order_id,
                        priority=1,
                    )
                )
        hedge = tp.key.pos_side != PosSide.BOTH
        qty = abs(pos.qty)
        side = "SELL" if pos.qty > 0 else "BUY"
        if hedge:
            side = "SELL" if tp.key.pos_side == PosSide.LONG else "BUY"
        payload = {
            "symbol": tp.key.symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format(qty, "f"),
            "newOrderRespType": "RESULT",
        }
        if hedge:
            payload["positionSide"] = tp.key.pos_side.value
        else:
            payload["reduceOnly"] = "true"
        seq += 1
        actions.append(
            ExecutionAction(
                kind=ActionKind.MARKET_CLOSE,
                symbol=tp.key.symbol,
                payload=payload,
                client_order_id=f"ct-emergency-{tp.key.symbol}-{seq}",
                replaces_client_order_id=None,
                priority=2,
            )
        )
    actions.sort(key=lambda a: a.priority)
    return ExecutionPlan(account_id=account_id, trade_id=None, actions=actions)


if __name__ == "__main__":
    raise SystemExit(main())
