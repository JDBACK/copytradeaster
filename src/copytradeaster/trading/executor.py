"""Durable executor: submits ExecutionPlans through the persistent journal.

Per plan (docs/ARCHITECTURE.md §7):
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


def _is_unknown_error(exc: AsterError) -> bool:
    """Classify transport/gateway timeout variants as outcome-unknown."""
    if exc.code in UNKNOWN_CODES or exc.http_status in UNKNOWN_HTTP | {504}:
        return True
    message = exc.msg.lower()
    return exc.code == 0 and ("timeout" in message or "timed out" in message)


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
    """Identity of a desired-state TRANSITION, not just the payload.

    revision (per symbol/leg, stamped by the Coordinator from sync_basis)
    distinguishes two legitimate identical commands (e.g. two consecutive
    MARKET_CLOSE 0.025 BTC); retries of the same transition keep the same
    revision and therefore the same logical command id.
    """
    canonical = json.dumps(action.payload, sort_keys=True, separators=(",", ":"))
    trade = action.trade_id if action.trade_id is not None else plan.trade_id
    raw = f"{plan.account_id}|{trade}|{action.kind.value}|{action.revision}|{canonical}"
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
        # Planner client-order ids are stable only within this plan. They let us
        # express the one safety dependency we need without introducing a DAG:
        # a replacement cancel is allowed only after its new protection is
        # CONFIRMED. A FAILED/SUBMITTED dependency blocks the cancel and leaves
        # the old protection untouched for the next reconciliation pass.
        completed_by_planner_coid: dict[str, CommandResult] = {}
        lock = self._locks.setdefault(plan.account_id, asyncio.Lock())
        async with lock:
            for action in sorted(plan.actions, key=lambda a: a.priority):
                if action.depends_on_client_order_id is not None:
                    dependency = completed_by_planner_coid.get(action.depends_on_client_order_id)
                    key = command_key_of(plan, action)
                    if dependency is None:
                        results.append(
                            CommandResult(
                                key,
                                "BLOCKED",
                                f"missing dependency {action.depends_on_client_order_id}",
                            )
                        )
                        continue
                    if dependency.status != "CONFIRMED":
                        results.append(
                            CommandResult(
                                key,
                                "BLOCKED",
                                f"dependency status={dependency.status}",
                            )
                        )
                        continue
                if action.kind == ActionKind.CANCEL_ORDER:
                    result = await self._submit_cancel(plan, action)
                elif action.kind == ActionKind.SET_LEVERAGE:
                    result = await self._submit_leverage(plan, action)
                else:
                    result = await self._submit_order(plan, action)
                results.append(result)
                if action.client_order_id is not None:
                    completed_by_planner_coid[action.client_order_id] = result
        return results

    async def wait_until_idle(self, account_id: str) -> None:
        """Wait until the account mutation sequence currently in progress ends."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            return

    # --- individual command kinds -------------------------------------------------

    async def _submit_leverage(
        self, plan: ExecutionPlan, action: ExecutionAction
    ) -> CommandResult | None:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None and existing["status"] in ("CONFIRMED", "SUBMITTED"):
            return CommandResult(key, str(existing["status"]), None)
        if existing is None:
            await self._commands.insert(
                key,
                plan.account_id,
                plan.trade_id,
                action.kind.value,
                json.dumps(action.payload, sort_keys=True),
                action.priority,
                None,
            )
        else:
            # FAILED row of the same logical transition: retry under the
            # original command key instead of colliding on the UNIQUE index.
            await self._commands.set_status(key, "PENDING")
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
        if existing is None:
            await self._commands.insert(
                key,
                plan.account_id,
                plan.trade_id,
                action.kind.value,
                json.dumps(action.payload, sort_keys=True),
                action.priority,
                None,
            )
        else:
            # FAILED row of the same logical transition: retry under the
            # original command key instead of colliding on the UNIQUE index.
            # Without this a failed dependent cancel could NEVER be retried -
            # the duplicate protection it guards would rest forever.
            await self._commands.set_status(key, "PENDING")
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
        if existing is not None:
            status = str(existing["status"])
            if status == "CONFIRMED":
                return CommandResult(key, status, None)
            if status == "SUBMITTED":
                # A previous attempt answered SUBMITTED but was never confirmed
                # in openOrders. That is NOT terminal success: verify on the
                # exchange before deciding to skip or re-send.
                recovered = await self._recover_submitted(plan, action, key, existing)
                if recovered is not None:
                    return recovered

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

        req = _with_coid(_payload_for(action), coid)
        symbol = action.payload["symbol"]

        while True:
            await self._commands.set_status(
                key, "SENDING", bump_attempts=True, client_order_id=coid
            )
            order = None
            try:
                order = await self._clients(plan.account_id).place_order(plan.account_id, req)
            except AsterError as exc:
                if exc.code == -2022 and await self._close_is_already_flat(plan, action):
                    # A concurrent protective fill can flatten the leg between
                    # the snapshot and this close.  Aster rejects the stale
                    # close as ReduceOnly/position-side invalid, but the
                    # desired state is already satisfied.  Confirm only after
                    # an authoritative positionRisk read; a non-flat leg
                    # remains a genuine FAILED command.
                    message = f"already-flat:-2022:{exc.msg}"
                    await self._commands.set_status(key, "CONFIRMED", last_error=message)
                    if self._on_confirmed is not None:
                        await self._on_confirmed(plan.account_id, action)
                    return CommandResult(key, "CONFIRMED", message)
                if not _is_unknown_error(exc):
                    return await self._fail(plan, key, exc)
            except TimeoutError:
                # built-in timeouts (asyncio); transport timeouts arrive as
                # AsterError(-1006/-1007) from the adapter boundary
                pass
            if order is None:
                # outcome unknown: check the exchange BEFORE any retry
                resolved = await self._resolve_unknown(plan, action, key, symbol, coid)
                if resolved is not None:
                    return resolved
                if attempt >= MAX_ATTEMPTS:
                    return await self._degrade(plan, key, "max attempts reached")
                attempt += 1
                coid = "ct" + key + f"r{attempt}"
                req = _with_coid(req, coid)
                continue

            await self._commands.set_status(key, "SUBMITTED", client_order_id=coid)
            return await self._confirm(plan, key, action, symbol, coid, order)

    async def _close_is_already_flat(self, plan: ExecutionPlan, action: ExecutionAction) -> bool:
        if action.kind != ActionKind.MARKET_CLOSE:
            return False
        try:
            positions = await self._clients(plan.account_id).position_risk(
                plan.account_id, symbol=action.symbol
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original failure
            log.warning("flat-state check failed for %s: %s", plan.account_id, exc)
            return False
        wanted_side = action.payload.get("positionSide", PosSide.BOTH.value)
        for position in positions:
            if position.position_side.value == wanted_side:
                return position.position_amt == 0
        return True

    # --- status transitions ---------------------------------------------------------

    async def _recover_submitted(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        key: str,
        existing,
    ) -> CommandResult | None:
        """Resolve a previously SUBMITTED command via the exchange.

        Found -> CONFIRMED (+order_link). Missing -> the caller falls through to
        a fresh attempt (SUBMITTED is never terminal success on its own).
        """
        coid = existing["client_order_id"]
        if not coid:
            return None
        symbol = action.payload["symbol"]
        try:
            order = await self._clients(plan.account_id).query_order(
                plan.account_id, symbol, orig_client_order_id=str(coid)
            )
        except AsterError as exc:
            log.warning("submitted-recovery query failed for %s: %s", key, exc)
            return None
        if order is None:
            return None
        await self._commands.set_status(
            key,
            "CONFIRMED",
            client_order_id=str(coid),
            last_error=f"recovered-submitted:{order.status}",
        )
        await self._record_link(plan, action, str(coid))
        if self._on_confirmed is not None and order.status in ("FILLED", "CANCELED", "EXPIRED"):
            await self._on_confirmed(plan.account_id, action)
        return CommandResult(key, "CONFIRMED", None)

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
        self, plan: ExecutionPlan, action: ExecutionAction, key: str, symbol: str, coid: str
    ) -> CommandResult | None:
        """query_order BEFORE retry: found -> CONFIRMED (+link); missing -> caller retries."""
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
            await self._record_link(plan, action, coid)
            log.info("UNKNOWN resolved as %s for %s (%s)", order.status, key, coid)
            return CommandResult(key, "CONFIRMED", None)
        return None

    async def _fail(self, plan: ExecutionPlan, key: str, exc: AsterError) -> CommandResult:
        error = f"{exc.code}:{exc.msg}"
        await self._commands.set_status(key, "FAILED", last_error=error)
        if self._settings is not None:
            await self._settings.set_flag("degraded", f"{plan.account_id}:{key}:{error}")
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
        trade_id = action.trade_id if action.trade_id is not None else plan.trade_id
        await self._links.add(
            trade_id, plan.account_id, action.kind.value, coid, master_coid=master_coid
        )


async def resolve_boot_unknowns(
    clients: Callable[[str], AsterClient],
    commands: CommandRepo,
    links: OrderLinkRepo | None = None,
) -> int:
    """Boot-time resolution of every in-flight command: UNKNOWN / SENDING / SUBMITTED.

    Crash windows: SENDING (request left, no response recorded) and SUBMITTED
    (response seen, confirmation lost) are resolved against exchange state via
    query_order on the stored client_order_id. Found -> CONFIRMED (+order_link
    so recovered protective orders re-enter the follower-own-fill guard);
    missing -> FAILED (reconciliation re-issues a fresh transition if needed).
    """
    rows = await commands.in_flight()
    confirmed = 0
    for row in rows:
        key = str(row["command_key"])
        account_id = str(row["account_id"])
        coid = row["client_order_id"]
        payload = json.loads(str(row["payload"]))
        if not coid:
            await commands.set_status(key, "FAILED", last_error="boot: no coid to resolve")
            continue
        try:
            order = await clients(account_id).query_order(
                account_id, payload["symbol"], orig_client_order_id=str(coid)
            )
        except AsterError as exc:
            log.warning("boot resolve query failed for %s: %s", key, exc)
            continue  # leave in-flight; retried on next boot
        if order is not None:
            await commands.set_status(key, "CONFIRMED", last_error=f"boot-resolved:{order.status}")
            if links is not None:
                trade_id = row["trade_id"]
                await links.add(
                    int(trade_id) if trade_id is not None else None,
                    account_id,
                    str(row["kind"]),
                    str(coid),
                    master_coid=None,
                )
            confirmed += 1
        else:
            await commands.set_status(key, "FAILED", last_error="boot-resolved:not-found")
    return confirmed


def _with_coid(req: PlaceOrderRequest, coid: str) -> PlaceOrderRequest:
    import dataclasses

    return dataclasses.replace(req, new_client_order_id=coid)
