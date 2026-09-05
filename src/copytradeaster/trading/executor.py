"""Durable executor: submits ExecutionPlans through the persistent journal.

Per plan (docs/ARCHITECTURE.md §7):
- command_key = sha256(account|trade_id|kind|canonical payload)[:24] - long-term
  service idempotency;
- client_order_id = "ct" + command_key (+ "r{attempt}" on retries), fits
  ^[\\.A-Z\\:/a-z0-9_-]{1,36}$;
- statuses: PENDING -> SENDING -> SUBMITTED -> CONFIRMED / UNKNOWN / FAILED;
- 503/timeout/-1000/-1006/-1007 => UNKNOWN, resolved from order history plus
  durable pre-position evidence before any retry;
- per-account lock serializes mutations (nonce ordering, ARCHITECTURE §8).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, NamedTuple

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
from copytradeaster.trading.builder_attribution import (
    AttributionDecision,
    AttributionKind,
    BuilderAttribution,
    is_builder_attribution_rejection,
)

log = get_logger(__name__)

UNKNOWN_HTTP = {503}
UNKNOWN_CODES = {-1000, -1006, -1007}
MAX_ATTEMPTS = 3
BOOT_QUERY_ATTEMPTS = 3
BOOT_QUERY_BACKOFF_S = 0.05
RECOVERY_QUERY_ATTEMPTS = 3
RECOVERY_QUERY_BACKOFF_S = 0.25

# Stage 3 Part 2 corrective: only these kinds may DIRECT-retry after an
# authoritative Builder rejection. ENTRY must always fail closed.
RISK_REDUCTION_KINDS = frozenset(
    {ActionKind.MARKET_CLOSE, ActionKind.PLACE_STOP, ActionKind.PLACE_TP}
)

CONDITIONAL_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
MARKET_KINDS = frozenset({ActionKind.MARKET_OPEN, ActionKind.MARKET_CLOSE})
NO_FILL_TERMINAL_STATUSES = frozenset({"CANCELED", "EXPIRED", "REJECTED"})


def _safe_error_category(exc: BaseException) -> str:
    """Persist only a stable code/category; full details stay in local logs."""
    if isinstance(exc, AsterError):
        return f"aster:{exc.code}"
    return type(exc).__name__


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


class ResolutionOutcome(StrEnum):
    """Safety verdict for an exchange mutation whose response is ambiguous."""

    FOUND = "FOUND"
    PROVEN_NOT_EXECUTED = "PROVEN_NOT_EXECUTED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class MarketRecoveryContext:
    """Durable state captured immediately before a MARKET mutation."""

    symbol: str
    position_side: PosSide
    pre_position_amt: Decimal
    expected_delta: Decimal

    def to_json(self) -> str:
        return json.dumps(
            {
                "symbol": self.symbol,
                "position_side": self.position_side.value,
                "pre_position_amt": format(self.pre_position_amt, "f"),
                "expected_delta": format(self.expected_delta, "f"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: object) -> MarketRecoveryContext | None:
        if not isinstance(raw, str) or not raw:
            return None
        try:
            payload = json.loads(raw)
            context = cls(
                symbol=str(payload["symbol"]),
                position_side=PosSide(str(payload["position_side"])),
                pre_position_amt=Decimal(str(payload["pre_position_amt"])),
                expected_delta=Decimal(str(payload["expected_delta"])),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
            return None
        if (
            not context.symbol
            or not context.pre_position_amt.is_finite()
            or not context.expected_delta.is_finite()
            or context.expected_delta == 0
        ):
            return None
        return context


@dataclass(frozen=True, slots=True)
class OrderResolution:
    outcome: ResolutionOutcome
    reason: str
    order: object | None = None
    observed_delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BootResolutionReport:
    confirmed: int = 0
    failed_not_found: int = 0
    unresolved_query_error: int = 0
    unresolved_without_coid: int = 0
    unresolved_economic: int = 0

    @property
    def unresolved(self) -> int:
        return self.unresolved_query_error + self.unresolved_without_coid + self.unresolved_economic


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
    return Decimal(raw) if raw is not None else None


def _order_field(order: object, name: str, default: object = None) -> object:
    if isinstance(order, dict):
        return order.get(name, default)
    return getattr(order, name, default)


def _resolution_from_order(order: object, source: str, *, market: bool = False) -> OrderResolution:
    status = str(_order_field(order, "status", "UNKNOWN")).upper()
    raw_executed = _order_field(order, "executed_qty")
    executed: Decimal | None = None
    if raw_executed is not None:
        try:
            executed = Decimal(str(raw_executed))
        except (InvalidOperation, ValueError, TypeError):
            executed = None
    if status in NO_FILL_TERMINAL_STATUSES and executed == 0:
        return OrderResolution(
            ResolutionOutcome.PROVEN_NOT_EXECUTED,
            f"{source}:{status}:executed=0",
            order=order,
        )
    if (market or _order_field(order, "type") == "MARKET") and status not in {
        "FILLED",
        *NO_FILL_TERMINAL_STATUSES,
    }:
        return OrderResolution(
            ResolutionOutcome.UNRESOLVED, f"{source}:market-{status}", order=order
        )
    executed_text = "unknown" if executed is None else format(executed, "f")
    return OrderResolution(
        ResolutionOutcome.FOUND,
        f"{source}:{status}:executed={executed_text}",
        order=order,
    )


async def _read_position_amount(
    client: object,
    account_id: str,
    symbol: str,
    position_side: PosSide,
) -> Decimal:
    rows = await client.position_risk(account_id, symbol=symbol)  # type: ignore[attr-defined]
    matches: list[Decimal] = []
    for position in rows:
        if str(getattr(position, "symbol", symbol)) != symbol:
            continue
        raw_side = getattr(position, "position_side", PosSide.BOTH)
        try:
            side = raw_side if isinstance(raw_side, PosSide) else PosSide(str(raw_side))
        except ValueError:
            continue
        if side != position_side:
            continue
        matches.append(Decimal(str(position.position_amt)))
    if not matches:
        # Aster-compatible fakes and some account snapshots omit flat rows.
        # A successful symbol-scoped read with no matching leg is flat.
        return Decimal(0)
    if len(set(matches)) != 1:
        raise ValueError("conflicting position rows")
    return matches[0]


def _context_matches_payload(
    context: MarketRecoveryContext,
    payload: dict[str, object],
) -> bool:
    raw_side = payload.get("positionSide", PosSide.BOTH.value)
    try:
        position_side = PosSide(str(raw_side))
        quantity = Decimal(str(payload["quantity"]))
        side = Side(str(payload["side"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False
    expected_delta = quantity if side == Side.BUY else -quantity
    return (
        context.symbol == str(payload.get("symbol", ""))
        and context.position_side == position_side
        and context.expected_delta == expected_delta
    )


async def _resolve_exchange_outcome(
    client: object,
    account_id: str,
    kind: ActionKind,
    payload: dict[str, object],
    coid: str,
    recovery_context: MarketRecoveryContext | None,
) -> OrderResolution:
    """Resolve one ambiguous send without authorizing a blind mutation.

    Order history is the negative-evidence boundary.  For MARKET commands an
    unchanged authoritative position must agree with every history sample
    before absence is considered proven. Only the full expected position delta
    proves completion without order history; partial or inconsistent movement
    remains unresolved.
    """
    symbol = str(payload.get("symbol", ""))
    if not symbol:
        return OrderResolution(ResolutionOutcome.UNRESOLVED, "malformed:missing-symbol")
    is_market = kind in MARKET_KINDS
    valid_context = (
        recovery_context
        if recovery_context is not None and _context_matches_payload(recovery_context, payload)
        else None
    )

    try:
        order = await client.query_order(  # type: ignore[attr-defined]
            account_id, symbol, orig_client_order_id=coid
        )
    except Exception as exc:  # noqa: BLE001 - adapters/fakes share this boundary
        log.warning(
            "current-order recovery unavailable account_id=%s category=%s",
            account_id,
            _safe_error_category(exc),
        )
    else:
        if order is not None:
            order_resolution = _resolution_from_order(order, "current", market=is_market)
            if order_resolution.outcome != ResolutionOutcome.PROVEN_NOT_EXECUTED or not is_market:
                return order_resolution

    complete_absence_samples = 0
    last_reason = "history-unavailable"
    for attempt in range(RECOVERY_QUERY_ATTEMPTS):
        history_read = False
        try:
            order = await client.query_order_history(  # type: ignore[attr-defined]
                account_id, symbol, orig_client_order_id=coid
            )
            history_read = True
        except Exception as exc:  # noqa: BLE001 - fail closed on any adapter failure
            last_reason = f"history-unavailable:{_safe_error_category(exc)}"
            log.warning(
                "order-history recovery unavailable account_id=%s attempt=%d/%d category=%s",
                account_id,
                attempt + 1,
                RECOVERY_QUERY_ATTEMPTS,
                _safe_error_category(exc),
            )
        else:
            if order is not None:
                order_resolution = _resolution_from_order(order, "history", market=is_market)
                if (
                    order_resolution.outcome != ResolutionOutcome.PROVEN_NOT_EXECUTED
                    or not is_market
                ):
                    return order_resolution

        position_unchanged = not is_market
        if is_market:
            if valid_context is None:
                last_reason = "missing-or-invalid-market-context"
                position_unchanged = False
            else:
                try:
                    actual = await _read_position_amount(
                        client,
                        account_id,
                        valid_context.symbol,
                        valid_context.position_side,
                    )
                except Exception as exc:  # noqa: BLE001 - unreadable state is ambiguous
                    last_reason = f"position-unavailable:{_safe_error_category(exc)}"
                    log.warning(
                        "position recovery unavailable account_id=%s attempt=%d/%d category=%s",
                        account_id,
                        attempt + 1,
                        RECOVERY_QUERY_ATTEMPTS,
                        _safe_error_category(exc),
                    )
                    position_unchanged = False
                else:
                    observed_delta = actual - valid_context.pre_position_amt
                    if observed_delta != 0:
                        expected = valid_context.expected_delta
                        progressed = observed_delta == expected
                        if progressed:
                            return OrderResolution(
                                ResolutionOutcome.FOUND,
                                "position-progress:"
                                f"observed={format(observed_delta, 'f')}:"
                                f"expected={format(expected, 'f')}",
                                observed_delta=observed_delta,
                            )
                        return OrderResolution(
                            ResolutionOutcome.UNRESOLVED,
                            "position-inconsistent:"
                            f"observed={format(observed_delta, 'f')}:"
                            f"expected={format(expected, 'f')}",
                            observed_delta=observed_delta,
                        )
                    position_unchanged = True

        if history_read and position_unchanged:
            complete_absence_samples += 1
            last_reason = "history-absent:position-unchanged"
        if attempt + 1 < RECOVERY_QUERY_ATTEMPTS:
            await asyncio.sleep(RECOVERY_QUERY_BACKOFF_S * (attempt + 1))

    if complete_absence_samples == RECOVERY_QUERY_ATTEMPTS:
        return OrderResolution(ResolutionOutcome.PROVEN_NOT_EXECUTED, last_reason)
    return OrderResolution(ResolutionOutcome.UNRESOLVED, last_reason)


def _apply_attribution(
    req: PlaceOrderRequest, attribution: BuilderAttribution | None
) -> PlaceOrderRequest:
    if attribution is None:
        return req
    import dataclasses

    return dataclasses.replace(req, builder=attribution.builder, fee_rate=attribution.fee_rate)


def command_key_of(plan: ExecutionPlan, action: ExecutionAction) -> str:
    """Identity of a desired-state TRANSITION, not just the payload.

    revision (per symbol/leg, stamped by the Coordinator from sync_basis)
    distinguishes two legitimate identical commands (e.g. two consecutive
    MARKET_CLOSE 0.025 BTC); retries of the same transition keep the same
    revision and therefore the same logical command id.
    """
    canonical = json.dumps(action.payload, sort_keys=True, separators=(",", ":"))
    trade = action.trade_id if action.trade_id is not None else plan.trade_id
    raw = (
        f"{plan.account_id}|{trade}|{plan.operation_id}|"
        f"{action.kind.value}|{action.revision}|{canonical}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Executor:
    def __init__(
        self,
        clients: Callable[[str], AsterClient],
        commands: CommandRepo,
        links: OrderLinkRepo,
        locks: dict[str, asyncio.Lock],
        settings: SettingsRepo | None = None,
        on_confirmed: Callable[..., Awaitable[None]] | None = None,
        attribution_provider: Any | None = None,
        builder_invalidator: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._clients = clients
        self._commands = commands
        self._links = links
        self._locks = locks
        self._settings = settings
        self._on_confirmed = on_confirmed
        self._attribution_provider = attribution_provider
        self._builder_invalidator = builder_invalidator
        self._mutation_owners: dict[str, asyncio.Task] = {}
        self._confirmed_callback_accepts_key = self._callback_accepts_command_key(on_confirmed)

    @staticmethod
    def _callback_accepts_command_key(
        callback: Callable[..., Awaitable[None]] | None,
    ) -> bool:
        """Detect the narrowly extended callback shape without breaking Stage 1.

        Existing embedders use ``(account_id, action)``.  The application
        integration uses ``(account_id, action, command_key)`` so its event
        key is the durable journal identity.  Signature inspection avoids
        catching a callback's own ``TypeError`` and retrying side effects.
        """
        if callback is None:
            return False
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return True
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        return has_varargs or len(positional) >= 3

    async def _notify_confirmed(
        self, account_id: str, action: ExecutionAction, command_key: str
    ) -> None:
        """Invoke confirmation observers after journaling, never altering result."""
        if self._on_confirmed is None:
            return
        try:
            if self._confirmed_callback_accepts_key:
                await self._on_confirmed(account_id, action, command_key)
            else:
                await self._on_confirmed(account_id, action)
        except Exception:  # noqa: BLE001 - observer/notification isolation
            log.exception(
                "confirmed callback failed (account_id=%s command_key=%s)",
                account_id,
                command_key,
            )

    async def submit(
        self, plan: ExecutionPlan, *, client: AsterClient | None = None
    ) -> list[CommandResult]:
        """Execute one plan under the account mutation lock.

        Coordinators pass the client captured with their immutable generation
        snapshot.  Direct callers may omit it and retain the registry callback
        routing used by Stage 1 and boot recovery.
        """
        async with self._mutation_guard(plan.account_id):
            return await self._submit_locked(plan, client=client)

    async def submit_under_mutation_lock(
        self, plan: ExecutionPlan, *, client: AsterClient | None = None
    ) -> list[CommandResult]:
        """Submit using a mutation lock already held by the current task.

        ``App.follower_lifecycle()`` owns the account lock while an emergency
        workflow snapshots, submits, verifies, and commits lifecycle state.
        Re-acquiring that non-reentrant lock here would deadlock, so this seam
        checks ownership and reuses the exact submit internals.
        """
        owner = self._mutation_owners.get(plan.account_id)
        if owner is not asyncio.current_task():
            raise RuntimeError(
                "submit_under_mutation_lock requires the current task to hold mutation_context"
            )
        return await self._submit_locked(plan, client=client)

    async def _submit_locked(
        self, plan: ExecutionPlan, *, client: AsterClient | None = None
    ) -> list[CommandResult]:
        ids = [a.action_id for a in plan.actions if a.action_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate execution action_id")
        results: list[CommandResult] = []
        # Planner client-order ids are stable only within this plan. Together
        # with planner action ids they express the safety dependencies we need
        # without introducing a DAG:
        #   * a replacement cancel runs only after its new protection is
        #     CONFIRMED (depends_on_client_order_id);
        #   * an entry/protection runs only after its SET_LEVERAGE prerequisite
        #     is CONFIRMED (depends_on_action_id). A FAILED/UNKNOWN prerequisite
        #     blocks the dependent risk-INCREASING action; closes, reductions
        #     and cancels carry no leverage dependency and always run.
        completed_by_planner_coid: dict[str, CommandResult] = {}
        completed_by_action_id: dict[str, CommandResult] = {}
        cached_decision: AttributionDecision | None = None
        for action in sorted(plan.actions, key=lambda a: a.priority):
            dependency: CommandResult | None = None
            missing = ""
            if action.depends_on_client_order_id is not None:
                dependency = completed_by_planner_coid.get(action.depends_on_client_order_id)
                missing = f"missing dependency {action.depends_on_client_order_id}"
            if dependency is None and action.depends_on_action_id is not None:
                dependency = completed_by_action_id.get(action.depends_on_action_id)
                missing = f"missing dependency {action.depends_on_action_id}"
            if missing:
                key = command_key_of(plan, action)
                if dependency is None:
                    results.append(CommandResult(key, "BLOCKED", missing))
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
                result = await self._submit_cancel(plan, action, client=client)
            elif action.kind == ActionKind.SET_LEVERAGE:
                result = await self._submit_leverage(plan, action, client=client)
            else:
                if cached_decision is None:
                    cached_decision = await self._resolve_attribution(plan.account_id)
                decision = cached_decision
                if (
                    decision.kind == AttributionKind.BUILDER_UNAVAILABLE
                    and action.kind == ActionKind.MARKET_OPEN
                ):
                    result = await self._builder_block_entry(plan, action, decision)
                elif decision.kind == AttributionKind.BUILDER_VALID:
                    assert decision.attribution is not None
                    result, builder_rejected = await self._submit_order(
                        plan, action, client=client, attribution=decision.attribution
                    )
                    if builder_rejected:
                        cached_decision = AttributionDecision(
                            kind=AttributionKind.BUILDER_UNAVAILABLE,
                            reason="builder-rejected",
                        )
                else:
                    is_fallback = decision.kind == AttributionKind.BUILDER_UNAVAILABLE
                    result, builder_rejected = await self._submit_order(plan, action, client=client)
                    if builder_rejected:
                        cached_decision = AttributionDecision(
                            kind=AttributionKind.BUILDER_UNAVAILABLE,
                            reason="builder-rejected",
                        )
                    if is_fallback and action.kind in (
                        ActionKind.MARKET_CLOSE,
                        ActionKind.PLACE_STOP,
                        ActionKind.PLACE_TP,
                    ):
                        await self._flag_builder_fallback(plan, result.command_key, action)
            results.append(result)
            if action.client_order_id is not None:
                completed_by_planner_coid[action.client_order_id] = result
            if action.action_id is not None:
                completed_by_action_id[action.action_id] = result
        return results

    async def wait_until_idle(self, account_id: str) -> None:
        """Wait until the account mutation sequence currently in progress ends."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            return

    @asynccontextmanager
    async def mutation_context(self, account_id: str):
        """Hold the executor's public per-account mutation barrier."""
        async with self._mutation_guard(account_id):
            yield

    @asynccontextmanager
    async def _mutation_guard(self, account_id: str):
        if self._mutation_owners.get(account_id) is asyncio.current_task():
            yield
            return
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            task = asyncio.current_task()
            previous = self._mutation_owners.get(account_id)
            self._mutation_owners[account_id] = task
            try:
                yield
            finally:
                if previous is None:
                    self._mutation_owners.pop(account_id, None)
                else:
                    self._mutation_owners[account_id] = previous

    async def _resolve_attribution(self, account_id: str) -> AttributionDecision:
        provider = self._attribution_provider
        if provider is None:
            return AttributionDecision(kind=AttributionKind.DIRECT)
        try:
            resolve = getattr(provider, "resolve", None)
            if callable(resolve):
                decision = await resolve(account_id)
            else:
                decision = await provider(account_id)
        except Exception:
            log.warning("builder attribution resolve failed account_id=%s", account_id)
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="lookup-failed"
            )
        if not isinstance(decision, AttributionDecision):
            return AttributionDecision(
                kind=AttributionKind.BUILDER_UNAVAILABLE, reason="lookup-failed"
            )
        return decision

    async def _builder_block_entry(
        self, plan: ExecutionPlan, action: ExecutionAction, decision: AttributionDecision
    ) -> CommandResult:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None:
            status = str(existing["status"])
            if status in ("CONFIRMED", "SENDING", "UNKNOWN", "SUBMITTED"):
                if status == "CONFIRMED":
                    return CommandResult(key, status, None)
                return CommandResult(key, status, "boot resolution required")
        reason = "builder-unavailable:entry-blocked"
        payload_json = json.dumps(action.payload, sort_keys=True)
        if existing is None:
            await self._commands.insert(
                key,
                plan.account_id,
                plan.trade_id,
                action.kind.value,
                payload_json,
                action.priority,
                None,
                plan.operation_id,
            )
        else:
            await self._commands.set_status(key, "PENDING", client_order_id=None)
        await self._commands.set_status(key, "FAILED", last_error=reason)
        if self._settings is not None:
            await self._settings.set_flag("degraded", f"{plan.account_id}:{key}:{reason}")
        log.error("command %s blocked: %s", key, reason)
        return CommandResult(key, "FAILED", reason)

    async def _flag_builder_fallback(
        self, plan: ExecutionPlan, command_key: str, action: ExecutionAction
    ) -> None:
        reason = f"builder-fallback:{action.kind.value}"
        if self._settings is not None:
            await self._settings.set_flag("degraded", f"{plan.account_id}:{command_key}:{reason}")
        log.warning("command %s builder fallback: %s", command_key, reason)

    async def _invalidate_builder_proof(self, account_id: str) -> None:
        # Durable invalidation only: identity_source stays builder, the verified
        # snapshot is dropped so the next resolve (even after restart) reads
        # BUILDER_UNAVAILABLE. Best-effort and never raises; never logs secrets.
        try:
            if self._builder_invalidator is not None:
                await self._builder_invalidator(account_id)
                return
            provider = self._attribution_provider
            invalidate = getattr(provider, "invalidate", None)
            if callable(invalidate):
                await invalidate(account_id)
            else:
                log.warning("builder proof invalidation unavailable account_id=%s", account_id)
        except Exception:
            log.warning("builder proof invalidation failed account_id=%s", account_id)

    async def _flag_builder_rejection(
        self, plan: ExecutionPlan, command_key: str, action: ExecutionAction, exc: AsterError
    ) -> None:
        reason = f"builder-rejected:{action.kind.value}:{exc.code}"
        if self._settings is not None:
            await self._settings.set_flag("degraded", f"{plan.account_id}:{command_key}:{reason}")
        log.warning("command %s builder rejected: %s", command_key, reason)

    # --- individual command kinds -------------------------------------------------

    async def _submit_leverage(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        *,
        client: AsterClient | None = None,
    ) -> CommandResult | None:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None and existing["status"] in (
            "SENDING",
            "UNKNOWN",
            "SUBMITTED",
            "CONFIRMED",
        ):
            # A historical confirmation is not proof of today's configuration.
            outcome = await _resolve_set_leverage_boot(
                lambda _: self._client_for(plan, client), self._commands, existing
            )
            if outcome == "confirmed":
                return CommandResult(key, "CONFIRMED", None)
            if outcome != "not_applied":
                await self._commands.set_status(
                    key, "UNKNOWN", last_error="leverage read unavailable"
                )
                return CommandResult(key, "UNKNOWN", "leverage read unavailable")
        if existing is None:
            await self._commands.insert(
                key,
                plan.account_id,
                plan.trade_id,
                action.kind.value,
                json.dumps(action.payload, sort_keys=True),
                action.priority,
                None,
                plan.operation_id,
            )
        else:
            # FAILED row of the same logical transition: retry under the
            # original command key instead of colliding on the UNIQUE index.
            await self._commands.set_status(key, "PENDING")
        await self._commands.set_status(key, "SENDING")
        try:
            await self._client_for(plan, client).set_leverage(
                plan.account_id, action.payload["symbol"], int(action.payload["leverage"])
            )
        except AsterError as exc:
            if exc.code in {-1000, -1006, -1007} or exc.http_status in {503, 504}:
                return await self._degrade(plan, key, "leverage outcome unknown")
            return await self._fail(plan, key, exc)
        except (TimeoutError, OSError):
            return await self._degrade(plan, key, "leverage transport outcome unknown")
        await self._commands.set_status(key, "CONFIRMED")
        await self._notify_confirmed(plan.account_id, action, key)
        return CommandResult(key, "CONFIRMED", None)

    async def _submit_cancel(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        *,
        client: AsterClient | None = None,
    ) -> CommandResult | None:
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None and existing["status"] in ("SENDING", "UNKNOWN"):
            return CommandResult(key, str(existing["status"]), "boot resolution required")
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
                plan.operation_id,
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
            await self._client_for(plan, client).cancel_order(
                plan.account_id, symbol, orig_client_order_id=coid
            )
        except AsterError as exc:
            if exc.code == -2011:  # already canceled/unknown -> treat as done
                safe_error = f"-2011:{_safe_error_category(exc)}"
                await self._commands.set_status(key, "CONFIRMED", last_error=safe_error)
                await self._notify_confirmed(plan.account_id, action, key)
                return CommandResult(key, "CONFIRMED", safe_error)
            return await self._fail(plan, key, exc)
        await self._commands.set_status(key, "CONFIRMED")
        await self._notify_confirmed(plan.account_id, action, key)
        return CommandResult(key, "CONFIRMED", None)

    async def _submit_order(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        *,
        client: AsterClient | None = None,
        attribution: BuilderAttribution | None = None,
    ) -> tuple[CommandResult, bool]:
        # Returns (result, builder_invalidated). builder_invalidated is True
        # only after an authoritative Builder rejection (-5026) proved the
        # stored attribution stale and durably invalidated it. ENTRY never
        # DIRECT-retries; risk reduction (CLOSE/STOP/TP) retries once DIRECT
        # with a fresh COID under the same command_key.
        key = command_key_of(plan, action)
        existing = await self._commands.get(key)
        if existing is not None:
            status = str(existing["status"])
            if status == "CONFIRMED":
                return CommandResult(key, status, None), False
            if status in ("SENDING", "UNKNOWN", "SUBMITTED"):
                # Reconciliation may revisit an in-flight command before a
                # restart.  Re-run the exact same resolver used at boot; only
                # PROVEN_NOT_EXECUTED is allowed to fall through to a resend.
                recovered = await self._recover_in_flight(
                    plan, action, key, existing, client=client
                )
                if recovered is not None:
                    return recovered, False
                existing = await self._commands.get(key)

        if action.kind == ActionKind.MARKET_OPEN:
            # A changed target/revision is not permission to bypass an older outcome.
            for pending in await self._commands.in_flight():
                if pending["account_id"] != plan.account_id or pending["command_key"] == key:
                    continue
                payload = json.loads(str(pending["payload"]))
                if payload.get("symbol") == action.symbol or pending["kind"] in {
                    ActionKind.MARKET_OPEN.value,
                    ActionKind.MARKET_CLOSE.value,
                    ActionKind.SET_LEVERAGE.value,
                }:
                    return CommandResult(key, "BLOCKED", "unresolved account exposure"), False

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
                plan.operation_id,
            )
        else:
            await self._commands.set_status(key, "PENDING", client_order_id=coid)

        base_req = _payload_for(action)
        if action.kind in MARKET_KINDS:
            try:
                recovery_context = await self._capture_market_recovery_context(
                    plan, base_req, client=client
                )
            except Exception as exc:  # noqa: BLE001 - no mutation has left yet
                reason = f"pre-position-unavailable:{_safe_error_category(exc)}"
                failed = await self._degrade(plan, key, reason)
                return failed, False
            await self._commands.set_recovery_context(key, recovery_context.to_json())
        else:
            recovery_context = None

        req = _apply_attribution(_with_coid(base_req, coid), attribution)
        symbol = action.payload["symbol"]
        current_attribution = attribution
        builder_invalidated = False

        while True:
            await self._commands.set_status(
                key, "SENDING", bump_attempts=True, client_order_id=coid
            )
            order = None
            try:
                order = await self._client_for(plan, client).place_order(plan.account_id, req)
            except AsterError as exc:
                if current_attribution is not None and is_builder_attribution_rejection(exc):
                    # Deterministic proof the stored attribution is stale: the
                    # first attempt was never accepted, so a DIRECT safety retry
                    # is idempotent under the same command_key.
                    await self._invalidate_builder_proof(plan.account_id)
                    builder_invalidated = True
                    current_attribution = None
                    if action.kind == ActionKind.MARKET_OPEN:
                        failed = await self._fail(plan, key, exc)
                        return failed, True
                    if action.kind in RISK_REDUCTION_KINDS:
                        await self._flag_builder_rejection(plan, key, action, exc)
                        attempt += 1
                        coid = "ct" + key + f"r{attempt}"
                        req = _with_coid(_payload_for(action), coid)
                        continue
                    failed = await self._fail(plan, key, exc)
                    return failed, True
                if exc.code == -2022 and await self._close_is_already_flat(
                    plan, action, client=client
                ):
                    # A concurrent protective fill can flatten the leg between
                    # the snapshot and this close.  Aster rejects the stale
                    # close as ReduceOnly/position-side invalid, but the
                    # desired state is already satisfied.  Confirm only after
                    # an authoritative positionRisk read; a non-flat leg
                    # remains a genuine FAILED command.
                    message = f"already-flat:-2022:{_safe_error_category(exc)}"
                    await self._commands.set_status(key, "CONFIRMED", last_error=message)
                    await self._notify_confirmed(plan.account_id, action, key)
                    return CommandResult(key, "CONFIRMED", message), builder_invalidated
                if not _is_unknown_error(exc):
                    failed = await self._fail(plan, key, exc)
                    return failed, builder_invalidated
            except TimeoutError:
                # built-in timeouts (asyncio); transport timeouts arrive as
                # AsterError(-1006/-1007) from the adapter boundary
                pass
            if order is None:
                await self._commands.set_status(
                    key,
                    "UNKNOWN",
                    last_error="send-outcome-unknown",
                    client_order_id=coid,
                )
                resolution = await _resolve_exchange_outcome(
                    self._client_for(plan, client),
                    plan.account_id,
                    action.kind,
                    action.payload,
                    coid,
                    recovery_context,
                )
                if resolution.outcome == ResolutionOutcome.FOUND:
                    resolved = await self._complete_resolved_order(
                        plan, action, key, coid, resolution, prefix="resolved-unknown"
                    )
                    return resolved, builder_invalidated
                if resolution.outcome == ResolutionOutcome.UNRESOLVED:
                    unresolved = await self._mark_unresolved(
                        plan, key, "UNKNOWN", resolution.reason
                    )
                    return unresolved, builder_invalidated
                if attempt >= MAX_ATTEMPTS:
                    degraded = await self._degrade(
                        plan, key, f"not-executed:max-attempts:{resolution.reason}"
                    )
                    return degraded, builder_invalidated
                attempt += 1
                coid = "ct" + key + f"r{attempt}"
                req = _with_coid(req, coid)
                await self._commands.set_status(
                    key,
                    "PENDING",
                    last_error=f"proven-not-executed:{resolution.reason}",
                    client_order_id=coid,
                )
                continue

            await self._commands.set_status(key, "SUBMITTED", client_order_id=coid)
            confirmed = await self._confirm(plan, key, action, symbol, coid, order, client=client)
            return confirmed, builder_invalidated

    async def _close_is_already_flat(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        *,
        client: AsterClient | None = None,
    ) -> bool:
        if action.kind != ActionKind.MARKET_CLOSE:
            return False
        try:
            positions = await self._client_for(plan, client).position_risk(
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

    async def _capture_market_recovery_context(
        self,
        plan: ExecutionPlan,
        req: PlaceOrderRequest,
        *,
        client: AsterClient | None = None,
    ) -> MarketRecoveryContext:
        if req.quantity is None or req.quantity <= 0:
            raise ValueError("MARKET recovery requires positive quantity")
        before = await _read_position_amount(
            self._client_for(plan, client),
            plan.account_id,
            req.symbol,
            req.position_side,
        )
        expected_delta = req.quantity if req.side == Side.BUY else -req.quantity
        return MarketRecoveryContext(
            symbol=req.symbol,
            position_side=req.position_side,
            pre_position_amt=before,
            expected_delta=expected_delta,
        )

    # --- status transitions ---------------------------------------------------------

    async def _recover_in_flight(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        key: str,
        existing,
        *,
        client: AsterClient | None = None,
    ) -> CommandResult | None:
        """Apply shared recovery policy to SENDING/UNKNOWN/SUBMITTED.

        ``None`` is returned only for PROVEN_NOT_EXECUTED, allowing the caller
        to create a fresh COID.  UNRESOLVED remains in-flight and can be
        re-queried by a later reconciliation or the boot resolver.
        """
        coid = existing["client_order_id"]
        if not coid:
            return await self._mark_unresolved(
                plan, key, str(existing["status"]), "missing-client-order-id"
            )
        context = MarketRecoveryContext.from_json(existing["recovery_context"])
        resolution = await _resolve_exchange_outcome(
            self._client_for(plan, client),
            plan.account_id,
            action.kind,
            action.payload,
            str(coid),
            context,
        )
        if resolution.outcome == ResolutionOutcome.PROVEN_NOT_EXECUTED:
            await self._commands.set_status(
                key,
                "FAILED",
                last_error=f"proven-not-executed:{resolution.reason}",
            )
            return None
        if resolution.outcome == ResolutionOutcome.UNRESOLVED:
            return await self._mark_unresolved(
                plan, key, str(existing["status"]), resolution.reason
            )
        return await self._complete_resolved_order(
            plan, action, key, str(coid), resolution, prefix="recovered-in-flight"
        )

    async def _confirm(
        self,
        plan: ExecutionPlan,
        key: str,
        action: ExecutionAction,
        symbol: str,
        coid: str,
        order,
        *,
        client: AsterClient | None = None,
    ) -> CommandResult:
        conditional = action.kind in (ActionKind.PLACE_STOP, ActionKind.PLACE_TP)
        if not conditional:
            resolution = _resolution_from_order(order, "result", market=True)
            if resolution.outcome == ResolutionOutcome.UNRESOLVED:
                return await self._mark_unresolved(plan, key, "SUBMITTED", resolution.reason)
            if resolution.outcome == ResolutionOutcome.PROVEN_NOT_EXECUTED:
                await self._commands.set_status(key, "FAILED", last_error=resolution.reason)
                return CommandResult(key, "FAILED", resolution.reason)
            await self._commands.set_status(key, "CONFIRMED", client_order_id=coid)
            await self._record_link(plan, action, coid)
            await self._notify_confirmed(plan.account_id, action, key)
            return CommandResult(key, "CONFIRMED", None)

        # conditional orders: verify via open_orders presence
        open_orders = await self._client_for(plan, client).open_orders(
            plan.account_id, symbol=symbol
        )
        if any(o.client_order_id == coid for o in open_orders):
            await self._commands.set_status(key, "CONFIRMED", client_order_id=coid)
            await self._record_link(plan, action, coid)
            await self._notify_confirmed(plan.account_id, action, key)
            return CommandResult(key, "CONFIRMED", None)
        await self._commands.set_status(key, "SUBMITTED", last_error="not seen in openOrders")
        await self._record_link(plan, action, coid)
        return CommandResult(key, "SUBMITTED", None)

    async def _complete_resolved_order(
        self,
        plan: ExecutionPlan,
        action: ExecutionAction,
        key: str,
        coid: str,
        resolution: OrderResolution,
        *,
        prefix: str,
    ) -> CommandResult:
        await self._commands.set_status(
            key,
            "CONFIRMED",
            client_order_id=coid,
            last_error=f"{prefix}:{resolution.reason}",
        )
        await self._record_link(plan, action, coid)
        await self._notify_confirmed(plan.account_id, action, key)
        log.info("economic outcome resolved for %s (%s): %s", key, coid, resolution.reason)
        return CommandResult(key, "CONFIRMED", None)

    async def _mark_unresolved(
        self,
        plan: ExecutionPlan,
        key: str,
        status: str,
        reason: str,
    ) -> CommandResult:
        safe_status = status if status in ("SENDING", "SUBMITTED", "UNKNOWN") else "UNKNOWN"
        error = f"resolution-unresolved:{reason}"
        await self._commands.set_status(key, safe_status, last_error=error)
        if self._settings is not None:
            await self._settings.set_flag("degraded", f"{plan.account_id}:{key}:{error}")
        log.error("command %s remains %s: %s", key, safe_status, reason)
        return CommandResult(key, safe_status, error)

    def _client_for(self, plan: ExecutionPlan, client: AsterClient | None) -> AsterClient:
        return client if client is not None else self._clients(plan.account_id)

    async def _fail(self, plan: ExecutionPlan, key: str, exc: AsterError) -> CommandResult:
        error = f"{exc.code}:{_safe_error_category(exc)}"
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
    *,
    only_account: str | None = None,
) -> BootResolutionReport:
    """Boot-time resolution of every in-flight command: UNKNOWN / SENDING / SUBMITTED.

    Crash windows use the same three-state exchange resolver as runtime.
    FOUND -> CONFIRMED; PROVEN_NOT_EXECUTED -> FAILED so reconciliation may
    safely issue a new transition; UNRESOLVED stays in-flight and blocks boot.
    """
    rows = await commands.in_flight()
    confirmed = 0
    failed_not_found = 0
    unresolved_query_error = 0
    unresolved_without_coid = 0
    unresolved_economic = 0
    for row in rows:
        if only_account is not None and row["account_id"] != only_account:
            continue
        key = str(row["command_key"])
        account_id = str(row["account_id"])
        coid = row["client_order_id"]
        try:
            payload = json.loads(str(row["payload"]))
            kind = ActionKind(str(row["kind"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            await commands.set_status(
                key, str(row["status"]), last_error="boot-resolution:malformed-command"
            )
            unresolved_economic += 1
            continue
        if not coid and str(row["kind"]) == ActionKind.SET_LEVERAGE.value:
            outcome = await _resolve_set_leverage_boot(clients, commands, row)
            if outcome == "confirmed":
                confirmed += 1
            elif outcome == "not_applied":
                failed_not_found += 1
            else:
                unresolved_query_error += 1
            continue
        if not coid:
            await commands.set_status(key, "UNKNOWN", last_error="boot: no coid to resolve")
            unresolved_without_coid += 1
            continue
        context = MarketRecoveryContext.from_json(row["recovery_context"])
        resolution = await _resolve_exchange_outcome(
            clients(account_id),
            account_id,
            kind,
            payload,
            str(coid),
            context,
        )
        if resolution.outcome == ResolutionOutcome.FOUND:
            await commands.set_status(
                key,
                "CONFIRMED",
                client_order_id=str(coid),
                last_error=f"boot-resolved:{resolution.reason}",
            )
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
        elif resolution.outcome == ResolutionOutcome.PROVEN_NOT_EXECUTED:
            await commands.set_status(
                key,
                "FAILED",
                last_error=f"boot-proven-not-executed:{resolution.reason}",
            )
            failed_not_found += 1
        else:
            await commands.set_status(
                key,
                str(row["status"]),
                last_error=f"boot-resolution-unresolved:{resolution.reason}",
            )
            if "unavailable" in resolution.reason:
                unresolved_query_error += 1
            else:
                unresolved_economic += 1
    return BootResolutionReport(
        confirmed=confirmed,
        failed_not_found=failed_not_found,
        unresolved_query_error=unresolved_query_error,
        unresolved_without_coid=unresolved_without_coid,
        unresolved_economic=unresolved_economic,
    )


async def _resolve_set_leverage_boot(
    clients: Callable[[str], AsterClient],
    commands: CommandRepo,
    row: Any,
) -> str:
    """Resolve a crash-window SET_LEVERAGE (no clientOrderId) via positionRisk.

    A lost leverage write leaves no order to query, so the authoritative read
    is the symbol's configured leverage: matching the journaled target proves
    the write landed (CONFIRMED); any other readable value proves it did not
    (FAILED - reconciliation re-provisions); an unreadable answer keeps the
    command in flight so boot stays fail-closed.
    """
    key = str(row["command_key"])
    account_id = str(row["account_id"])
    try:
        payload = json.loads(str(row["payload"]))
        symbol = str(payload["symbol"])
        target = int(payload["leverage"])
    except (KeyError, TypeError, ValueError) as exc:
        await commands.set_status(key, "UNKNOWN", last_error=f"boot: malformed payload:{exc}")
        return "unresolved"
    last_error: AsterError | None = None
    for attempt in range(BOOT_QUERY_ATTEMPTS):
        try:
            rows = await clients(account_id).position_risk(account_id, symbol)
            values = {int(r.leverage) for r in rows if r.symbol == symbol and int(r.leverage) > 0}
            observed: int | None = values.pop() if len(values) == 1 else None
            break
        except AsterError as exc:
            last_error = exc
            log.warning(
                "boot resolve leverage read failed for %s (%d/%d): %s",
                key,
                attempt + 1,
                BOOT_QUERY_ATTEMPTS,
                exc,
            )
            if attempt + 1 < BOOT_QUERY_ATTEMPTS:
                await asyncio.sleep(BOOT_QUERY_BACKOFF_S * (attempt + 1))
    else:
        safe_error = _safe_error_category(last_error) if last_error is not None else "unknown"
        await commands.set_status(
            key,
            str(row["status"]),
            last_error=f"boot-query-unresolved:{safe_error}",
        )
        return "unresolved"
    if observed is None:
        # Empty or conflicting rows are not an authoritative answer: the
        # outcome is unknown, not disproven - keep the command in flight so
        # boot stays fail-closed instead of marking a maybe-applied write
        # as terminal FAILED.
        await commands.set_status(
            key,
            str(row["status"]),
            last_error="boot-query-unresolved:ambiguous-leverage",
        )
        return "unresolved"
    if observed == target:
        await commands.set_status(key, "CONFIRMED", last_error=f"boot-resolved:leverage={observed}")
        return "confirmed"
    await commands.set_status(
        key, "FAILED", last_error=f"boot-resolved:leverage-mismatch:{observed}"
    )
    return "not_applied"


def _with_coid(req: PlaceOrderRequest, coid: str) -> PlaceOrderRequest:
    import dataclasses

    return dataclasses.replace(req, new_client_order_id=coid)
