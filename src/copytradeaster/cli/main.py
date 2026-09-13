"""CLI: init-db, import-wallet, approve-builder, run-bot, status, reconcile,
pause/resume, emergency-close-all."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from copytradeaster.application.control import (
    EmergencyController,
    EmergencyReport,
)
from copytradeaster.application.control import build_close_plan as _build_close_plan
from copytradeaster.config import Settings
from copytradeaster.logging_utils import redact
from copytradeaster.storage.repositories import NonceStore, SettingsRepo, TelegramUserRepo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copytradeaster",
        description="AsterDex V3 copy-trade bot: core engine and Telegram runtime (testnet)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create/migrate the SQLite database")

    p_telegram_user = sub.add_parser(
        "telegram-user", help="bootstrap the closed Telegram user allowlist"
    )
    telegram_user_sub = p_telegram_user.add_subparsers(dest="telegram_user_action", required=True)
    p_telegram_add = telegram_user_sub.add_parser(
        "add", help="add a USER by numeric Telegram user ID"
    )
    p_telegram_add.add_argument("user_id", help="positive numeric Telegram user ID")
    p_telegram_add.add_argument(
        "--chat-id",
        default=None,
        help="private chat ID (defaults to the Telegram user ID)",
    )
    telegram_user_sub.add_parser("list", help="list safe Telegram allowlist fields")
    for action, help_text in (
        ("block", "block Telegram notifications for one user"),
        ("unblock", "restore Telegram notifications for one user"),
    ):
        p_action = telegram_user_sub.add_parser(action, help=help_text)
        p_action.add_argument("user_id", help="positive numeric Telegram user ID")

    p_import = sub.add_parser("import-wallet", help="import wallet.txt blocks")
    p_import.add_argument("file", type=Path)
    p_import.add_argument("--wipe", action="store_true", help="replace credentials")

    p_approve = sub.add_parser("approve-builder", help="approveAgent+approveBuilder run")
    p_approve.add_argument("--accounts", default="all", help="all or comma-separated IDs")
    p_approve.add_argument("--max-fee-rate", default=None)

    sub.add_parser("run", help="run the core engine until Ctrl+C")
    sub.add_parser("run-bot", help="run the Stage 2 Telegram runtime until Ctrl+C")
    sub.add_parser("status", help="show engine status")
    p_reconcile = sub.add_parser("reconcile", help="force reconciliation sweep")
    p_reconcile.add_argument("--account", default=None)

    sub.add_parser("pause", help="block new entries on all followers")
    sub.add_parser("resume", help="allow new entries again")
    p_close = sub.add_parser("emergency-close-all", help="flatten every follower")
    p_close.add_argument("--yes", action="store_true", required=True)
    p_fee = sub.add_parser(
        "prove-builder-fee",
        help="measure the Builder fee source on a signed testnet account and stamp the policy",
    )
    p_fee.add_argument("--account", required=True, help="follower account id or label")
    p_fee.add_argument("--symbol", default="BTCUSDT")
    p_fee.add_argument("--quantity", default="0.001")
    p_fee.add_argument("--yes", action="store_true", required=True)
    p_backfill = sub.add_parser(
        "backfill-income",
        help="import income (commissions/funding) older than the live 7-day window",
    )
    p_backfill.add_argument("--account", required=True, help="follower account id or label")
    p_backfill.add_argument(
        "--days", type=int, default=30, help="how many days back from the live window to import"
    )
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
    if args.command == "telegram-user":
        if args.telegram_user_action == "add":
            return await _telegram_user_add(args.user_id, args.chat_id)
        if args.telegram_user_action == "list":
            return await _telegram_user_list()
        if args.telegram_user_action == "block":
            return await _telegram_user_set_blocked(args.user_id, blocked=True)
        if args.telegram_user_action == "unblock":
            return await _telegram_user_set_blocked(args.user_id, blocked=False)
        print("error: unknown telegram-user action", file=sys.stderr)
        return 2
    if args.command == "import-wallet":
        return await _import_wallet(args.file, wipe=args.wipe)
    if args.command == "approve-builder":
        return await _approve_builder(args.accounts, args.max_fee_rate)
    if args.command == "run":
        return await _run()
    if args.command == "run-bot":
        return await _run_bot()
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
    if args.command == "backfill-income":
        return await _backfill_income(args.account, args.days)
    if args.command == "prove-builder-fee":
        if not args.yes:
            print("refusing without --yes", file=sys.stderr)
            return 2
        return await _prove_builder_fee(args.account, args.symbol, args.quantity)
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


# --- Telegram allowlist bootstrap ------------------------------------------------------


def _positive_telegram_id(value: object, field: str) -> int:
    """Normalize a CLI Telegram ID without accepting bools or lossy integers."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.isdecimal():
            raise ValueError(f"{field} must be a positive integer")
        parsed = int(text)
    else:
        raise ValueError(f"{field} must be a positive integer")
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


async def _telegram_db():
    from copytradeaster.storage.database import Database

    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    return db


async def _telegram_user_add(user_id: object, chat_id: object | None = None) -> int:
    try:
        telegram_user_id = _positive_telegram_id(user_id, "user_id")
        requested_chat_id = None if chat_id is None else _positive_telegram_id(chat_id, "chat_id")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    db = await _telegram_db()
    try:
        users = TelegramUserRepo(db)
        current = await users.get(telegram_user_id)
        if current is None:
            effective_chat_id = telegram_user_id if requested_chat_id is None else requested_chat_id
            await users.upsert(
                telegram_user_id,
                effective_chat_id,
                role="USER",
            )
            print(f"added telegram user {telegram_user_id} chat_id={effective_chat_id} role=USER")
            return 0

        # Re-adding an existing identity is deliberately a no-op on ownership,
        # role, and notification state.  An explicit --chat-id may repair a
        # changed private chat destination; omission preserves the stored one.
        effective_chat_id = (
            int(current["chat_id"]) if requested_chat_id is None else requested_chat_id
        )
        notifications_enabled = _telegram_bool_value(
            current["notifications_enabled"], "notifications"
        )
        await users.upsert(
            telegram_user_id,
            effective_chat_id,
            role=str(current["role"]),
            account_id=current["account_id"],
            notifications_enabled=notifications_enabled,
            blocked_at=current["blocked_at"],
        )
        print(
            f"already present telegram user {telegram_user_id} "
            f"chat_id={effective_chat_id} role={current['role']} "
            f"account={current['account_id'] or '-'}"
        )
        return 0
    except (ValueError, sqlite3.IntegrityError) as exc:
        print(f"error: cannot add telegram user {telegram_user_id}: {exc}", file=sys.stderr)
        return 2
    finally:
        await db.close()


async def _telegram_user_list() -> int:
    db = await _telegram_db()
    try:
        rows = await db.fetchall(
            "SELECT telegram_user_id, chat_id, role, account_id, "
            "notifications_enabled, blocked_at, access_enabled FROM telegram_users "
            "ORDER BY telegram_user_id"
        )
        for row in rows:
            notifications = _telegram_bool_word(row["notifications_enabled"], "notifications")
            blocked = "yes" if row["blocked_at"] is not None else "no"
            print(
                f"user_id={row['telegram_user_id']} chat_id={row['chat_id']} "
                f"role={row['role']} account={row['account_id'] or '-'} "
                f"access={'on' if row['access_enabled'] else 'off'} "
                f"notifications={notifications} blocked={blocked}"
            )
        print(f"telegram users: {len(rows)}")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close()


def _telegram_bool_word(value: object, field: str) -> str:
    return "on" if _telegram_bool_value(value, field) else "off"


def _telegram_bool_value(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value in {"0", "1"}:
        return value == "1"
    raise ValueError(f"{field} must be stored as 0 or 1")


async def _telegram_user_set_blocked(user_id: object, *, blocked: bool) -> int:
    if not isinstance(blocked, bool):
        print("error: blocked must be a boolean", file=sys.stderr)
        return 2
    try:
        telegram_user_id = _positive_telegram_id(user_id, "user_id")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    db = await _telegram_db()
    try:
        users = TelegramUserRepo(db)
        current = await users.get(telegram_user_id)
        if current is None:
            print(f"error: telegram user {telegram_user_id} not found", file=sys.stderr)
            return 2
        currently_blocked = current["blocked_at"] is not None
        if currently_blocked != blocked:
            await users.set_notifications_enabled(telegram_user_id, not blocked)
        print(f"{'blocked' if blocked else 'unblocked'} telegram user {telegram_user_id}")
        return 0
    finally:
        await db.close()


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

            await db.execute_write("DELETE FROM credentials WHERE account_id=?", (account_id,))
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
        nonces=NonceStore(db),
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


async def _run_bot() -> int:
    """Run the bounded Stage 2 process; Bot API access starts inside polling."""
    from copytradeaster.application.engine import build_stage2_process

    process = build_stage2_process(_load_settings())
    await process.run()
    return 0


async def _status() -> int:
    from copytradeaster.storage.database import Database

    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    settings = SettingsRepo(db)
    stored_ready = await settings.get_flag("ready")
    heartbeat_raw = await settings.get_flag("heartbeat_at")
    heartbeat_at = int(heartbeat_raw) if heartbeat_raw and heartbeat_raw.isdigit() else 0
    heartbeat_age_ms = max(int(__import__("time").time() * 1000) - heartbeat_at, 0)
    stale_after_ms = max(2 * cfg.reconcile_interval_s, 30) * 1000
    fresh = heartbeat_at > 0 and heartbeat_age_ms <= stale_after_ms
    ready = stored_ready == "true" and fresh
    runtime_state = "ready" if ready else ("stale" if stored_ready == "true" else "stopped")
    degraded_active = await settings.get_flag("degraded_active")
    degraded_reason = await settings.get_flag("degraded_reason")
    paused = await settings.get_flag("allow_new_entries")
    print(
        f"ready={'true' if ready else 'false'} runtime={runtime_state} "
        f"heartbeat_age_ms={heartbeat_age_ms if heartbeat_at else '-'} "
        f"degraded={degraded_reason if degraded_active == 'true' else '-'} "
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
        accounts = await app.db.fetchall(
            "SELECT * FROM accounts WHERE role='follower' AND enabled=1 ORDER BY id"
        )
        for row in accounts:
            await app.follower_registry.publish_new(app.runtime_for_row(row))
        exchange = await app.master_client.exchange_info(force=True)
        app._filters_cache = exchange.tradable_filters()
        await app.projector.rebuild_full(app.master_client, "main")
        ids = [account] if account else None
        report = await app.reconciler.reconcile(app.projector.state, account_ids=ids, reason="cli")
        for aid, created in report.per_account.items():
            print(f"{aid}: commands created: {created}")
    finally:
        await app.stop()
    return 0


async def _backfill_income(account: str, days: int) -> int:
    """One resumable pass over income windows the live worker never reaches.

    Testnet and mainnet both allowed: this reads history only, never places an
    order. The pass stops at the exchange's live 7-day boundary, so it cannot
    race the running worker.
    """
    import time

    from copytradeaster.app import App
    from copytradeaster.history.backfill import backfill_income
    from copytradeaster.history.reader import HistoryReader
    from copytradeaster.storage.history import HistoryScope

    if not 1 <= days <= 365:
        print("refusing: --days must be between 1 and 365", file=sys.stderr)
        return 2
    app = App(_load_settings())
    try:
        await app.db.connect()
        await app.db.migrate()
        await app.credentials.load()
        row = await app.db.fetchone(
            "SELECT id, role FROM accounts WHERE id=? OR label=?", (account, account)
        )
        if row is None or str(row["role"]) != "follower":
            print(f"unknown follower account {account!r}", file=sys.stderr)
            return 2
        account_id = str(row["id"])
        scope = HistoryScope(account_id, app.cfg.network_profile.name.lower())
        now_ms = int(time.time() * 1000)
        # The history reader signs with the follower's own credentials; the
        # registry is populated exactly like `reconcile` does so an unknown
        # account can never fall back to the master client.
        for follower in await app.db.fetchall(
            "SELECT * FROM accounts WHERE role='follower' AND enabled=1 ORDER BY id"
        ):
            await app.follower_registry.publish_new(app.runtime_for_row(follower))
        reader = HistoryReader(app._client_for(account_id))
        report = await backfill_income(
            reader,
            app.history,
            scope,
            start_ms=now_ms - (7 + days) * 86_400_000,
            end_ms=now_ms,
            now_ms=now_ms,
        )
        for step in report.steps:
            print(
                f"income window {step.start_ms}-{step.end_ms} rows={step.rows} "
                f"requests={step.requests} complete={step.complete}"
            )
        print(
            f"covered {report.covered_from_ms}-{report.covered_to_ms} rows={report.rows} "
            f"requests={report.requests} stopped_at={report.stopped_at_ms} "
            f"reason={report.reason}"
        )
        return 0 if report.stopped_at_ms is None else 1
    finally:
        await app.stop()


async def _prove_builder_fee(account: str, symbol: str, quantity: str) -> int:
    """Measure the Builder fee on a signed testnet account; stamp it only on proof.

    The measurement places real (test-funds) orders, so it is testnet-only and
    refuses a Builder account that this exchange account has not approved.
    """
    from decimal import Decimal as _D

    from copytradeaster.app import App
    from copytradeaster.history.builder_fee_proof import (
        BuilderFeeProofError,
        measure_builder_fee_policy,
        policy_stamp,
    )
    from copytradeaster.storage.repositories import AccountRepo

    cfg = _load_settings()
    if "asterdex-testnet" not in cfg.rest_base_url:
        print("refusing: builder-fee proof is a testnet measurement", file=sys.stderr)
        return 2
    builder_address = str(cfg.builder_address or "").strip()
    if not builder_address.startswith("0x"):
        print("refusing: CT_BUILDER_ADDRESS is not configured", file=sys.stderr)
        return 2
    app = App(cfg)
    try:
        await app.db.connect()
        await app.db.migrate()
        await app.credentials.load()
        row = await app.db.fetchone(
            "SELECT id, label, identity_source FROM accounts WHERE id=? OR label=?",
            (account, account),
        )
        if row is None:
            print(f"unknown account {account!r}", file=sys.stderr)
            return 2
        account_id = str(row["id"])
        if str(row["identity_source"] or "") != "builder":
            print(
                f"account {account_id} is not a Builder account; nothing to measure",
                file=sys.stderr,
            )
            return 2
        client = app._client_for(account_id)
        builders = await client.builder(account_id)
        if not any(
            str(entry.builder_address or "").casefold() == builder_address.casefold()
            for entry in builders
        ):
            print(
                f"account {account_id} has not approved builder {builder_address[:10]}…",
                file=sys.stderr,
            )
            return 2
        hedge = await client.get_position_mode(account_id)
        try:
            proof = await measure_builder_fee_policy(
                client=client,
                account_id=account_id,
                symbol=symbol,
                quantity=_D(quantity),
                builder_address=builder_address,
                fee_rate=_D(cfg.max_fee_rate),
                hedge=hedge,
            )
        except BuilderFeeProofError as exc:
            print(f"measurement failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"policy={proof.policy} attributed={proof.attributed_rate} "
            f"control={proof.control_rate} feeRate={proof.fee_rate} "
            f"fills={proof.attributed_fills}/{proof.control_fills} "
            f"income={','.join(proof.income_types)}"
        )
        print(f"detail: {proof.detail}")
        if proof.policy != "included_in_commission" or not proof.matches_fee_rate:
            print("policy not stamped: the measurement did not prove the expected delta")
            return 1
        stamp = policy_stamp(proof, app.cfg.network_profile.name.lower())
        await AccountRepo(app.db).set_builder_fee_policy(account_id, stamp)
        print(f"stamped account_id={account_id} policy={stamp}")
        return 0
    finally:
        await app.stop()


async def _set_pause(paused: bool) -> int:
    from copytradeaster.storage.database import Database

    cfg = _load_settings()
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    settings = SettingsRepo(db)
    await settings.set_flag("allow_new_entries", "false" if paused else "true")
    if not paused:
        # Resume is deliberately explicit: emergency never re-enables entries
        # by itself, but the operator can do so after inspecting status.
        await settings.set_flag("emergency_active", "false")
    await db.close()
    print("paused" if paused else "resumed")
    return 0


async def _emergency_close_all() -> int:
    from copytradeaster.app import App

    app = App(_load_settings())
    try:
        await app.db.connect()
        await app.db.migrate()
        await app.credentials.load()
        accounts = await app.db.fetchall(
            "SELECT * FROM accounts WHERE role='follower' AND enabled=1 ORDER BY id"
        )
        for row in accounts:
            await app.follower_registry.publish_new(app.runtime_for_row(row))
        exchange = await app.master_client.exchange_info(force=True)
        app._filters_cache = exchange.tradable_filters()
        state = _CliEmergencyState(app.settings)
        controller = EmergencyController(
            lifecycle_factory=app.follower_lifecycle,
            executor=app.executor,
            state=state,
        )
        report = await controller.emergency_global(
            follower.account_id for follower in app.followers
        )
        _print_emergency_report(report)
        return 0 if report.success else 1
    finally:
        await app.stop()


def build_close_plan(*args, **kwargs):
    """Compatibility export; implementation lives in application.control."""
    return _build_close_plan(*args, **kwargs)


class _CliEmergencyState:
    def __init__(self, settings: SettingsRepo) -> None:
        self._settings = settings

    async def pause_account(self, account_id: str) -> None:
        await self._settings.set_allow_new_entries(account_id, False)

    async def begin_global(self, operation_id: str) -> None:
        await self._settings.set_flag("emergency_active", "true")
        await self._settings.set_flag("allow_new_entries", "false")
        await self._settings.set_flag("emergency_operation_id", operation_id)

    async def record_result(self, report: EmergencyReport) -> None:
        status = "CONFIRMED" if report.success else "FAILED"
        await self._settings.set_flag("emergency_last_result", f"{status}:{report.operation_id}")


def _print_emergency_report(report: EmergencyReport) -> None:
    for account in report.accounts:
        status = "flat" if account.success else f"failed:{account.failure.value}"
        print(
            f"[{account.account_id}] emergency {status}; "
            f"passes={account.passes} flat_proofs={account.flat_proofs}"
        )
    if report.success:
        print(
            f"emergency operation {report.operation_id}: flat verified twice; entries remain paused"
        )
    else:
        print(f"emergency operation {report.operation_id}: FAILED; entries remain paused")


if __name__ == "__main__":
    raise SystemExit(main())
