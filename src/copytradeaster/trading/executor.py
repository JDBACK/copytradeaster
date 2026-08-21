"""Durable executor: submits ExecutionPlans through the persistent journal.

Per plan (ARCHITECTURE.md §7):
- command_key = sha256(account|trade_id|kind|canonical payload)[:24] - long-term
  service idempotency;
- client_order_id = "ct" + command_key (+ "r{attempt}" on retries), fits
  ^[\\.A-Z\\:/a-z0-9_-]{1,36}$;
- statuses: PENDING -> SENDING -> SUBMITTED -> CONFIRMED / UNKNOWN / FAILED;
- 503/timeout/-1000/-1006/-1007 => UNKNOWN, resolved by query_order BEFORE any retry;
- per-account lock serializes mutations (nonce ordering, ARCHITECTURE §8).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from copytradeaster.aster.trading_api import AsterClient, AsterError, PlaceOrderRequest
from copytradeaster.domain.models import (
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PosSide,
    Side,
)
from copytradeaster.logging_utils import get_logger
from copytradeaster.storage.repositories import CommandRepo, OrderLinkRepo, SettingsRepo

log = get_logger(__name__)

UNKNOWN_HTTP = {503}
UNKNOWN_CODES = {-1000, -1006, -1007}
MAX_ATTEMPTS = 3

CONDITIONAL_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


class CommandResult(NamedTuple):
    command_key: str
    status: str
    error: str | None


def _payload_for(action: ExecutionAction) -> PlaceOrderRequest:
    p = action.payload
    kind = action.kind
    if kind == ActionKind.SET_LEVERAGE:
        raise ValueError("SET_LEVERAGE is not an order")
    pos_side = PosSide(p["positionSide"]) if "positionSide" in p else PosSide.BOTH
    order_type = p.get("type", "MARKET")
    return PlaceOrderRequest(
        symbol=p["symbol"],
        side=Side(p["side"]),
        type=order_type,
        quantity=_dec_or_none(p.get("quantity")),
        price=_dec_or_none(p.get("price")),
        stop_price=_dec_or_none(p.get("stopPrice")),
        reduce_only=p.get("reduceOnly") == "true",
        close_position=p.get("closePosition") == "true",
        position_side=pos_side,
        new_client_order_id=action.client_order_id or "",
        new_order_resp_type=p.get("newOrderRespType", "ACK"),
        working_type=p.get("workingType"),
    )


def _dec_or_none(raw: str | None):
    from decimal import Decimal

    return Decimal(raw) if raw is not None else None


def command_key_of(plan: ExecutionPlan, action: ExecutionAction) -> str:
    canonical = json.dumps(action.payload, sort_keys=True, separators=(",", ":"))
    raw = f"{plan.account_id}|{plan.trade_id}|{action.kind.value}|{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Executor:
    def __init__(
        self,
        clients: Callable[[str], AsterClient],
        commands: CommandRepo,
        links: OrderLinkRepo,
        locks: dict[str, asyncio.Lock],
        settings: SettingsRepo | None = None,
        on_confirmed: Callable[[str, ExecutionAction], Awaitable[None]] | None = None,
    ) -> None:
        self._clients = clients
        self._commands = commands
        self._links = links
        self._locks = locks
        self._settings = settings
        self._on_confirmed = on_confirmed

    async def submit(self, plan: ExecutionPlan) -> list[CommandResult]:
        results: list[CommandResult] = []
        lock = self._locks.setdefault(plan.account_id, asyncio.Lock())
        async with lock:
            for action in sorted(plan.actions, key=lambda a: a.priority):
                if action.kind == ActionKind.CANCEL_ORDER:
                    results.append(await self._submit_cancel(plan, action))
                elif action.kind == ActionKind.SET_LEVERAGE:
                    results.append(await self._submit_leverage(plan, action))
                else:
                    results.append(await self._submit_order(plan, action))
        return [r for r in results if r is not None]

    # --- individual command kinds -------------------------------------------------

    async def _submit_leverage(
        self, plan: ExecutionPlan, action: ExecutionAction
    ) -> CommandResult | None:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None and existing["status"] in ("CONFIRMED", "SUBMITTED"):
            return CommandResult(key, str(existing["status"]), None)
        await self._commands.insert(
            key,
            plan.account_id,
            plan.trade_id,
            action.kind.value,
            json.dumps(action.payload, sort_keys=True),
            action.priority,
            None,
        )
        await self._commands.set_status(key, "SENDING")
        try:
            await self._clients(plan.account_id).set_leverage(
                plan.account_id, action.payload["symbol"], int(action.payload["leverage"])
            )
        except AsterError as exc:
            return await self._fail(plan, key, exc)
        await self._commands.set_status(key, "CONFIRMED")
        return CommandResult(key, "CONFIRMED", None)

    async def _submit_cancel(
        self, plan: ExecutionPlan, action: ExecutionAction
    ) -> CommandResult | None:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None and existing["status"] in ("CONFIRMED", "SUBMITTED"):
            return CommandResult(key, str(existing["status"]), None)
        await self._commands.insert(
            key,
            plan.account_id,
            plan.trade_id,
            action.kind.value,
            json.dumps(action.payload, sort_keys=True),
            action.priority,
            None,
        )
        await self._commands.set_status(key, "SENDING")
        symbol = action.payload["symbol"]
        coid = action.payload["origClientOrderId"]
        try:
            await self._clients(plan.account_id).cancel_order(
                plan.account_id, symbol, orig_client_order_id=coid
            )
        except AsterError as exc:
            if exc.code == -2011:  # already canceled/unknown -> treat as done
                await self._commands.set_status(key, "CONFIRMED", last_error=f"-2011:{exc.msg}")
                return CommandResult(key, "CONFIRMED", f"-2011:{exc.msg}")
            return await self._fail(plan, key, exc)
        await self._commands.set_status(key, "CONFIRMED")
        return CommandResult(key, "CONFIRMED", None)

    async def _submit_order(self, plan: ExecutionPlan, action: ExecutionAction) -> CommandResult:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None and existing["status"] in ("CONFIRMED", "SUBMITTED"):
            return CommandResult(key, str(existing["status"]), None)
        # idempotent re-submission after UNKNOWN resolution happens below; a FAILED
        # or PENDING/UNKNOWN record gets a fresh attempt with a new coid.

        attempts = int(existing["attempts"]) if existing is not None else 0
        attempt = attempts + 1
        coid = "ct" + key + (f"r{attempt}" if attempt > 1 else "")
        payload_json = json.dumps(action.payload, sort_keys=True)
        if existing is None:
            await self._commands.insert(
                key,
                plan.account_id,
                plan.trade_id,
                action.kind.value,
                payload_json,
                action.priority,
                coid,
            )
        else:
            await self._commands.set_status(key, "PENDING", client_order_id=coid)

        req = _payload_for(action)
        req = _with_coid(req, coid)
        symbol = action.payload["symbol"]

        while True:
            await self._commands.set_status(
                key, "SENDING", bump_attempts=True, client_order_id=coid
            )
            try:
                order = await self._clients(plan.account_id).place_order(plan.account_id, req)
            except TimeoutError:
                resolved = await self._resolve_unknown(plan, key, symbol, coid)
                if resolved is not None:
                    return resolved
            except AsterError as exc:
                if exc.code in UNKNOWN_CODES or exc.http_status in UNKNOWN_HTTP:
                    resolved = await self._resolve_unknown(plan, key, symbol, coid)
                    if resolved is not None:
                        return resolved
                else:
                    return await self._fail(plan, key, exc)

                if attempt >= MAX_ATTEMPTS:
                    return await self._degrade(plan, key, "max attempts reached")
                attempt += 1
                coid = "ct" + key + f"r{attempt}"
                req = _with_coid(req, coid)
                continue

            await self._commands.set_status(key, "SUBMITTED", client_order_id=coid)
            confirmed = await self._confirm(plan, key, action, symbol, coid, order)
            return confirmed

    # --- status transitions ---------------------------------------------------------

    async def _confirm(
        self, plan: ExecutionPlan, key: str, action: ExecutionAction, symbol: str, coid: str, order
    ) -> CommandResult:
        conditional = action.kind in (ActionKind.PLACE_STOP, ActionKind.PLACE_TP)
        if not conditional:
            # MARKET RESULT response carries final fill data
            await self._commands.set_status(key, "CONFIRMED", client_order_id=coid)
            await self._record_link(plan, action, coid)
            if self._on_confirmed is not None:
                await self._on_confirmed(plan.account_id, action)
            return CommandResult(key, "CONFIRMED", None)

        # conditional orders: verify via open_orders presence
        open_orders = await self._clients(plan.account_id).open_orders(
            plan.account_id, symbol=symbol
        )
        if any(o.client_order_id == coid for o in open_orders):
            await self._commands.set_status(key, "CONFIRMED", client_order_id=coid)
            await self._record_link(plan, action, coid)
            if self._on_confirmed is not None:
                await self._on_confirmed(plan.account_id, action)
            return CommandResult(key, "CONFIRMED", None)
        await self._commands.set_status(key, "SUBMITTED", last_error="not seen in openOrders")
        await self._record_link(plan, action, coid)
        return CommandResult(key, "SUBMITTED", None)

    async def _resolve_unknown(
        self, plan: ExecutionPlan, key: str, symbol: str, coid: str
    ) -> CommandResult | None:
        """query_order BEFORE retry: found -> CONFIRMED; missing -> caller retries."""
        try:
            order = await self._clients(plan.account_id).query_order(
                plan.account_id, symbol, orig_client_order_id=coid
            )
        except AsterError as exc:
            log.warning("unknown-resolution query failed: %s", exc)
            return None
        if order is not None:
            await self._commands.set_status(
                key,
                "CONFIRMED",
                client_order_id=coid,
                last_error=f"resolved-unknown:{order.status}",
            )
            log.info("UNKNOWN resolved as %s for %s (%s)", order.status, key, coid)
            return CommandResult(key, "CONFIRMED", None)
        return None

    async def _fail(self, plan: ExecutionPlan, key: str, exc: AsterError) -> CommandResult:
        error = f"{exc.code}:{exc.msg}"
        await self._commands.set_status(key, "FAILED", last_error=error)
        log.error("command %s failed: %s", key, error)
        return CommandResult(key, "FAILED", error)

    async def _degrade(self, plan: ExecutionPlan, key: str, reason: str) -> CommandResult:
        await self._commands.set_status(key, "FAILED", last_error=reason)
        if self._settings is not None:
            await self._settings.set_flag("degraded", f"{plan.account_id}:{key}:{reason}")
        log.error("command %s degraded: %s", key, reason)
        return CommandResult(key, "FAILED", reason)

    async def _record_link(self, plan: ExecutionPlan, action: ExecutionAction, coid: str) -> None:
        master_coid = action.replaces_client_order_id
        await self._links.add(
            plan.trade_id, plan.account_id, action.kind.value, coid, master_coid=master_coid
        )


def _with_coid(req: PlaceOrderRequest, coid: str) -> PlaceOrderRequest:
    import dataclasses

    return dataclasses.replace(req, new_client_order_id=coid)


async def resolve_boot_unknowns(
    clients: Callable[[str], AsterClient], commands: CommandRepo
) -> int:
    """Boot-time UNKNOWN resolution across all accounts; returns confirmed count."""
    rows = await commands.unresolved_unknowns()
    confirmed = 0
    for row in rows:
        account_id = str(row["account_id"])
        coid = row["client_order_id"]
        payload = json.loads(str(row["payload"]))
        if not coid:
            await commands.set_status(
                str(row["command_key"]), "FAILED", last_error="no coid to resolve"
            )
            continue
        try:
            order = await clients(account_id).query_order(
                account_id, payload["symbol"], orig_client_order_id=str(coid)
            )
        except AsterError as exc:
            log.warning("boot resolve query failed for %s: %s", row["command_key"], exc)
            continue
        if order is not None:
            await commands.set_status(
                str(row["command_key"]),
                "CONFIRMED",
                last_error=f"resolved-unknown-boot:{order.status}",
            )
            confirmed += 1
        else:
            await commands.set_status(
                str(row["command_key"]), "FAILED", last_error="resolved-unknown-boot:not-found"
            )
    return confirmed
