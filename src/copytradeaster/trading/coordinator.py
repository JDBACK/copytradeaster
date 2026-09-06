"""Coordinator: master deltas -> per-follower target states -> plans -> executor.

Fan-out is concurrent and isolated: one follower's error never blocks the rest;
3 consecutive follower errors raise the app-level degraded flag.

State-based reconciliation contract:
- The sizing basis (master magnitude the follower was last synced to) is
  PERSISTED in sync_basis and updated only AFTER a plan executes without
  failures, so a restart replays missed master reductions deterministically
  (current exchange state + persisted basis -> desired follower state).
- Idempotency: each desired-state transition carries a per-leg revision stamped
  from sync_basis; identical-looking but distinct transitions (two equal closes,
  SL A->B->A) get different revisions, retries keep one logical command id.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from decimal import Decimal

from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PositionKey,
    PositionState,
    PosSide,
    ProtectiveOrder,
    RiskPolicy,
    SkipReason,
    SymbolFilters,
    TargetPosition,
    TargetState,
    ZeroReason,
)
from copytradeaster.logging_utils import get_logger
from copytradeaster.master.projector import MasterDelta, MasterState
from copytradeaster.notifications.formatter import format_skip_reason
from copytradeaster.notifications.models import NotificationEvent, NotificationSeverity
from copytradeaster.notifications.protocol import (
    NotificationCallback,
    Notifier,
    as_notifier,
)
from copytradeaster.risk.engine import (
    deviation_exceeded,
    entry_quantity,
    post_fill_cap,
    scaled_reduce,
)
from copytradeaster.storage.repositories import (
    CopyTradeRepo,
    OrderLinkRepo,
    SettingsRepo,
    SyncBasisRepo,
)
from copytradeaster.trading.executor import Executor, command_key_of
from copytradeaster.trading.leverage import LeverageManager
from copytradeaster.trading.planner import build_plan
from copytradeaster.trading.registry import FollowerRegistry

log = get_logger(__name__)

STOP_TYPES = ("STOP_MARKET", "STOP")
TP_TYPES = ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")

POSTFILL_RECOMPUTE_DEBOUNCE_S = 2.0
POSTFILL_STATE_MAX_AGE_S = 10.0
NOTIFICATION_KEY_CACHE_MAX = 4096
_SAFE_FRAGMENT = re.compile(r"[^A-Za-z0-9_.:-]+")
LIQUIDATION_MMR_CUSHION = Decimal("0.01")


def _safe_fragment(value: object, fallback: str = "unknown") -> str:
    text = _SAFE_FRAGMENT.sub("_", str(value or ""))[:80].strip("_")
    return text or fallback


def _error_category(exc: BaseException) -> str:
    """Keep exception details in logs; expose only a stable safe category."""
    code = getattr(exc, "code", None)
    if code is not None:
        return f"aster_{_safe_fragment(code, 'error')}"
    return _safe_fragment(type(exc).__name__, "error")


@dataclass(slots=True)
class FollowerRuntime:
    account_id: str
    client: AsterClient
    identity: object
    lock: asyncio.Lock = None  # type: ignore[assignment]
    execution_mode: str = "REAL"

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()


@dataclass(slots=True)
class SkipRecord:
    symbol: str
    reason: SkipReason
    pos_side: PosSide = PosSide.BOTH
    trade_id: int | None = None


def _canonical_target(t: TargetPosition) -> str:
    return json.dumps(
        {
            "qty": str(t.qty),
            "stop_price": None if t.stop_price is None else str(t.stop_price),
            "tp_price": None if t.tp_price is None else str(t.tp_price),
            "stop_type": t.stop_type,
            "tp_type": t.tp_type,
            "leverage": t.leverage,
            # the WHY of a zero target is part of the desired state: a sticky
            # dust-flat and a transitional flip-zero with identical qty must
            # not share command keys
            "hold_flat_until_episode_end": t.hold_flat_until_episode_end,
            "zero_reason": None if t.zero_reason is None else t.zero_reason.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _filters_for(filters_by_symbol: dict[str, SymbolFilters], symbol: str) -> SymbolFilters:
    filters = filters_by_symbol.get(symbol)
    if filters is None:
        raise KeyError(f"no filters for symbol {symbol}")
    return filters


def _bot_owned(order) -> bool:
    """Follower orders owned by this engine use Executor's reserved prefix."""
    return str(order.client_order_id).startswith("ct")


def _as_protective(order) -> ProtectiveOrder:
    return ProtectiveOrder(
        client_order_id=str(order.client_order_id),
        order_id=int(order.order_id),
        type=str(order.type),
        side=order.side,
        stop_price=order.stop_price,
        reduce_only=bool(order.reduce_only),
        close_position=bool(order.close_position),
        working_type=str(order.working_type),
        orig_qty=order.orig_qty,
    )


def _pick_with_extras(orders: list, order_types: tuple[str, ...]):
    """Newest protective order of the slot plus every duplicate beyond it.

    The exchange can legitimately hold TWO bot-owned conditionals of one slot:
    the replacement was CONFIRMED but its dependent cancel FAILED (transport
    error, -2019 on a later pass, crash between the two commands). Surfacing
    only the newest order made the next reconciliation see current==target and
    converge to an empty plan with the stale level still resting.
    """
    candidates = sorted(
        (o for o in orders if o.type in order_types and _bot_owned(o)),
        key=lambda o: o.update_time,
    )
    if not candidates:
        return None, ()
    return _as_protective(candidates[-1]), tuple(_as_protective(o) for o in candidates[:-1])


class Coordinator:
    def __init__(
        self,
        followers: list[FollowerRuntime] | FollowerRegistry,
        settings: SettingsRepo,
        executor: Executor,
        trades: CopyTradeRepo,
        links: OrderLinkRepo,
        filters_provider: Callable[[], dict[str, object]],
        app_flags: SettingsRepo,
        symbol_whitelist: list[str],
        fee_buffer_pct: Decimal,
        working_type: str,
        on_notify: Callable[[str], Awaitable[None]] | None = None,
        stale_master_ms: int = 5000,
        notifier: Notifier | NotificationCallback | None = None,
        basis: SyncBasisRepo | None = None,
        snapshot_symbols: set[str] | None = None,
        leverage: LeverageManager | None = None,
        entry_admission: Callable[[], bool] | None = None,
        # PM-B7 operator safety rails (configurable; defaults keep legacy sizing).
        # operator_max_leverage caps every leverage the coordinator resolves or
        # reuses (control-plane confirmed, exchange maximum, policy/master) -
        # an operator ceiling independent of per-follower RiskPolicy.
        # liquidation_buffer_pct is the required relative margin between the
        # protective stop and the authoritative liquidation price: LONG needs
        # liquidation < stop*(1-buffer), SHORT mirrored. A violated leg is
        # reduced/flattened, never re-leveraged.
        operator_max_leverage: int | None = None,
        liquidation_buffer_pct: Decimal | str | int = Decimal("0.005"),
        max_total_risk_pct: Decimal | None = None,
    ) -> None:
        self._registry = (
            followers if isinstance(followers, FollowerRegistry) else FollowerRegistry(followers)
        )
        self._settings = settings
        self._executor = executor
        self._trades = trades
        self._links = links
        self._filters_provider = filters_provider
        self._app_flags = app_flags
        self._whitelist = set(symbol_whitelist)
        self._fee_buffer = fee_buffer_pct
        self._working_type = working_type
        # ``on_notify`` remains a narrow legacy text callback for Stage-1
        # embedders/tests.  The application composition root uses the
        # structured ``notifier`` port below; no user-facing exception text
        # crosses that boundary.
        self._on_notify = on_notify
        self._notifier = as_notifier(notifier) if notifier is not None else None
        self._on_notify_accepts_event = self._callback_accepts_event(on_notify)
        self._stale_master_ms = stale_master_ms
        self._error_streaks: dict[str, int] = {}
        self._error_incidents: dict[str, int] = {}
        self._basis = basis
        self._snapshot_symbols = snapshot_symbols
        self._snapshot_scope: ContextVar = ContextVar("snapshot_scope", default=snapshot_symbols)
        self._last_master_state: MasterState | None = None
        self._last_master_state_at: float = 0.0
        self._max_total_risk_pct = max_total_risk_pct
        self._last_postfill_recompute: dict[str, float] = {}
        self._postfill_tasks: set[asyncio.Task] = set()
        self._postfill_admission_lock = asyncio.Lock()
        self._postfill_admission_open = True
        self._generation_admission_lock = asyncio.Lock()
        self._generation_idle = asyncio.Event()
        self._generation_idle.set()
        self._generation_admission_open = True
        self._active_generations = 0
        self._notification_keys_seen: set[str] = set()
        # Control-plane for follower leverage (candidate catalog + confirmed
        # per-account state). None keeps the legacy test seam where the target
        # leverage resolves from the client/policy without a control plane.
        self._leverage = leverage
        if operator_max_leverage is not None and operator_max_leverage <= 0:
            raise ValueError("operator_max_leverage must be positive")
        self._operator_max_leverage = operator_max_leverage
        buffer = (
            liquidation_buffer_pct
            if isinstance(liquidation_buffer_pct, Decimal)
            else Decimal(str(liquidation_buffer_pct))
        )
        if not Decimal(0) <= buffer < Decimal(1):
            raise ValueError("liquidation_buffer_pct must be in [0, 1)")
        self._liquidation_buffer_pct = buffer
        # Process-local startup/reconnect boundary. It is independent of the
        # user's durable pause: false blocks only new/increased exposure while
        # closes, reductions and protection convergence continue.
        self._entry_admission = entry_admission or (lambda: True)
        self._notification_key_order: deque[str] = deque()

    @staticmethod
    def _callback_accepts_event(callback: Callable[..., Awaitable[None]] | None) -> bool:
        if callback is None or hasattr(callback, "emit"):
            return callback is not None
        try:
            parameters = list(inspect.signature(callback).parameters.values())
        except (TypeError, ValueError):
            return False
        if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
            return True
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if not positional:
            return False
        parameter = positional[0]
        annotation = parameter.annotation
        return (
            parameter.name in {"event", "notification", "notification_event"}
            or annotation is NotificationEvent
            or str(annotation).endswith("NotificationEvent")
        )

    async def on_master_delta(self, delta: MasterDelta) -> None:
        self._last_master_state = delta.state
        self._last_master_state_at = time.monotonic()
        if not await self._enter_generation():
            return
        followers = self._registry.snapshot()
        try:
            # Capture freshness once for the whole fan-out generation. A slow
            # signed follower must not make later followers reject the same
            # authoritative master delta halfway through the gather.
            master_fresh = self._state_fresh(delta.state)
            results = await asyncio.gather(
                *(
                    self._recompute_follower(fr, delta, master_fresh=master_fresh)
                    for fr in followers
                ),
                return_exceptions=True,
            )
            for fr, result in zip(followers, results, strict=True):
                if isinstance(result, Exception):
                    streak = self._error_streaks.get(fr.account_id, 0) + 1
                    self._error_streaks[fr.account_id] = streak
                    log.error(
                        "follower %s recompute failed (%d): %s", fr.account_id, streak, result
                    )
                    if streak >= 3:
                        await self._app_flags.set_flag(
                            "degraded",
                            f"{fr.account_id}:recompute:{_error_category(result)}",
                        )
                    if streak not in (1, 3):
                        continue
                    category = _error_category(result)
                    safe_account = _safe_fragment(fr.account_id)
                    if streak == 1:
                        incident = self._error_incidents.get(fr.account_id, 0) + 1
                        self._error_incidents[fr.account_id] = incident
                    else:
                        incident = self._error_incidents.get(fr.account_id, 1)
                    await self._notify(
                        (
                            f"Сверка follower {safe_account} не выполнена "
                            f"(категория: {category}, инцидент {incident}, streak {streak})."
                        ),
                        event_key=(
                            f"degraded:recompute:{safe_account}:incident-{incident}:"
                            f"streak-{streak}:{category}"
                        ),
                        account_id=fr.account_id,
                        kind="follower.recompute_failed",
                        severity=NotificationSeverity.WARN,
                    )
                else:
                    self._error_streaks[fr.account_id] = 0
        finally:
            await self._leave_generation()

    async def on_command_confirmed(self, account_id: str, action: ExecutionAction) -> None:
        """Post-fill safety net: re-run the affected follower right after an
        execution so a bad fill (worse price -> higher real risk) is capped
        without waiting for the periodic sweep.

        MUST run as a separate task: it re-enters executor.submit, whose
        per-account asyncio.Lock is still held by the confirming submit -
        awaiting inline would deadlock (asyncio.Lock is not re-entrant)."""
        if action.kind not in (ActionKind.MARKET_OPEN, ActionKind.MARKET_CLOSE):
            return
        now = time.monotonic()
        last = self._last_postfill_recompute.get(account_id, 0.0)
        if now - last < POSTFILL_RECOMPUTE_DEBOUNCE_S:
            return
        self._last_postfill_recompute[account_id] = now
        fr = self._registry.get(account_id)
        state = self._last_master_state
        captured_at = self._last_master_state_at
        if fr is None or state is None:
            return
        if time.monotonic() - captured_at > POSTFILL_STATE_MAX_AGE_S:
            # L1 live incident: the cached delta predates a REST-side fill
            # the lagging WS never delivered. Recomputing closes against it
            # flattens live legs on a phantom MASTER_CLOSED (open/close
            # oscillation). The sweep/boundary converges on authoritative
            # state seconds later; the post-fill net stays an optimization.
            log.info(
                "post-fill recompute skipped for %s: cached master state is stale",
                _safe_fragment(account_id),
            )
            return
        async with self._postfill_admission_lock:
            if not self._postfill_admission_open:
                return
            task = asyncio.create_task(self._postfill_recompute(fr, state, captured_at))
            self._postfill_tasks.add(task)
            task.add_done_callback(self._postfill_tasks.discard)

    async def _postfill_recompute(
        self, fr: FollowerRuntime, state: MasterState, captured_at: float
    ) -> None:
        try:
            # on_confirmed is called while Executor holds the account mutation
            # lock. Wait for the whole originating plan (including its new
            # protection) before taking the snapshot; otherwise a post-fill
            # task can observe the brief pre-STOP window and place a duplicate.
            await self._executor.wait_until_idle(fr.account_id)
            # Review 636dfff P1: the capture above predates the lock wait. A
            # newer authoritative refresh (REST sweep/boundary via
            # recompute_follower, or a newer WS delta) may have superseded it
            # while we waited — a TTL alone cannot see that, and recomputing
            # against the stale capture closes live legs on a phantom
            # MASTER_CLOSED. Re-read the cache after the wait and converge
            # on whatever is current (the cache only moves forward: the
            # projector discards out-of-order frames before they reach it).
            latest = self._last_master_state
            if latest is None:
                return
            if (
                latest is not state
                or latest.generation != state.generation
                or self._last_master_state_at != captured_at
            ):
                state = latest
            elif time.monotonic() - captured_at > POSTFILL_STATE_MAX_AGE_S:
                # Nothing newer arrived and the capture aged out while we
                # waited: same stale-cache hazard as at admission.
                log.info(
                    "post-fill recompute skipped for %s: cached master state aged out",
                    _safe_fragment(fr.account_id),
                )
                return
            await self._recompute_follower(fr, MasterDelta(frozenset({"*"}), state))
        except Exception as exc:  # noqa: BLE001 - notification path must not raise
            log.warning("post-fill recompute failed for %s: %s", fr.account_id, exc)

    async def close_postfill_admission(self) -> None:
        """Stop new post-fill tasks before a bounded shutdown drain."""
        lock = getattr(self, "_postfill_admission_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._postfill_admission_lock = lock
        async with lock:
            self._postfill_admission_open = False

    async def open_postfill_admission(self) -> None:
        """Re-open post-fill admission for an explicitly resumed runtime."""
        lock = getattr(self, "_postfill_admission_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._postfill_admission_lock = lock
        async with lock:
            self._postfill_admission_open = True

    async def close_generation_admission(self, timeout_s: float = 5.0) -> None:
        """Close fan-out/reconcile admission and wait for active generations."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        lock = getattr(self, "_generation_admission_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._generation_admission_lock = lock
            idle = asyncio.Event()
            idle.set()
            self._generation_idle = idle
            self._generation_admission_open = True
            self._active_generations = 0
        async with lock:
            self._generation_admission_open = False
            if self._active_generations == 0:
                self._generation_idle.set()
            idle = self._generation_idle
        await asyncio.wait_for(idle.wait(), timeout=timeout_s)

    async def open_generation_admission(self) -> None:
        """Re-open fan-out/reconcile admission after a completed pause."""
        lock = getattr(self, "_generation_admission_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._generation_admission_lock = lock
            self._generation_idle = asyncio.Event()
            self._generation_idle.set()
            self._active_generations = 0
        async with lock:
            self._generation_admission_open = True

    async def pause_barrier(self, timeout_s: float = 5.0) -> None:
        """Persist pause and wait until no admitted generation can place entries."""
        await self._app_flags.set_flag("allow_new_entries", "false")
        await self.close_generation_admission(timeout_s=timeout_s)

    async def resume_after_pause(self) -> None:
        """Persist resume before allowing another generation to start."""
        await self._app_flags.set_flag("allow_new_entries", "true")
        await self.open_generation_admission()

    async def _enter_generation(self) -> bool:
        lock = getattr(self, "_generation_admission_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._generation_admission_lock = lock
            self._generation_idle = asyncio.Event()
            self._generation_idle.set()
            self._generation_admission_open = True
            self._active_generations = 0
        async with lock:
            if not self._generation_admission_open:
                return False
            self._active_generations += 1
            self._generation_idle.clear()
            return True

    async def _leave_generation(self) -> None:
        lock = getattr(self, "_generation_admission_lock", None)
        if lock is None:
            return
        async with lock:
            self._active_generations -= 1
            if self._active_generations <= 0:
                self._active_generations = 0
                self._generation_idle.set()

    def _state_fresh(self, master: MasterState) -> bool:
        """False when the master state predates stale_master_ms; event_time==0
        (manual/snapshot state used by tests and boot) counts as fresh."""
        if master.event_time <= 0:
            return True
        return (time.time() * 1000 - master.event_time) <= self._stale_master_ms

    async def _entries_allowed(self, policy: RiskPolicy) -> bool:
        """Both the per-follower policy AND the global CLI pause must allow."""
        try:
            if not self._entry_admission():
                return False
        except Exception:  # fail closed on a malformed composition callback
            return False
        if not policy.allow_new_entries:
            return False
        flag = await self._app_flags.get_flag("allow_new_entries")
        emergency = await self._app_flags.get_flag("emergency_active")
        return flag != "false" and emergency != "true"

    async def _recompute_follower(
        self,
        fr: FollowerRuntime,
        delta: MasterDelta,
        *,
        master_fresh: bool | None = None,
    ) -> int:
        # Snapshot and mutation must be one per-follower generation. The
        # executor lock serializes exchange writes, but without this outer lock
        # a post-fill recompute could snapshot the same position concurrently
        # with a master-delta recompute and issue contradictory closes.
        async with fr.lock, self._executor.mutation_context(fr.account_id):
            from copytradeaster.trading.executor import resolve_boot_unknowns

            await resolve_boot_unknowns(
                lambda _: fr.client, self.commands, self._links, only_account=fr.account_id
            )
            return await self._recompute_follower_locked(fr, delta, master_fresh=master_fresh)

    async def _recompute_follower_locked(
        self,
        fr: FollowerRuntime,
        delta: MasterDelta,
        *,
        master_fresh: bool | None = None,
    ) -> int:
        policy = await self._settings.get_policy(fr.account_id)
        if policy is None or not policy.enabled:
            return -1
        actual = await self._snapshot(fr)
        targets, skips = await self._targets_for(
            fr.account_id,
            delta.state,
            actual,
            policy,
            client=fr.client,
            master_fresh=master_fresh,
        )
        for skip in skips:
            if skip.reason in {
                SkipReason.PAUSED,
                SkipReason.INTENTIONAL_FLAT,
                SkipReason.OWN_PROTECTIVE_FILLED,
            }:
                continue
            safe_account = _safe_fragment(fr.account_id)
            safe_symbol = _safe_fragment(skip.symbol)
            safe_reason = _safe_fragment(skip.reason.value)
            safe_pos_side = _safe_fragment(skip.pos_side.value)
            episode = self._skip_episode_token(skip, delta.state)
            skip_text = format_skip_reason(
                skip.symbol,
                skip.reason,
                skip.pos_side,
                account_id=fr.account_id,
            )
            await self._notify(
                skip_text,
                event_key=(
                    f"skip:{safe_account}:{safe_symbol}:{safe_pos_side}:{safe_reason}:{episode}"
                ),
                account_id=fr.account_id,
                kind="copy.skip",
                severity=NotificationSeverity.INFO,
            )
        plan = build_plan(
            actual,
            TargetState(account_id=fr.account_id, targets=targets),
            self._filters_provider(),  # type: ignore[arg-type]
            self._coid_factory(),
            working_type=self._working_type,
        )
        if not plan.actions:
            # already converged: remember the master magnitude we are in sync with
            await self._persist_refs(fr.account_id, targets, failed=False, master=delta.state)
            return 0
        log.info(
            "[%s] plan: %s",
            fr.account_id,
            [
                (a.kind.value, a.payload.get("quantity") or a.payload.get("leverage"))
                for a in plan.actions
            ],
        )
        plan = await self._bind_plan_context(fr.account_id, targets, plan, actual)
        results = await self._executor.submit(plan, client=fr.client)
        if any(
            action.kind in (ActionKind.MARKET_OPEN, ActionKind.MARKET_CLOSE)
            and result.status == "CONFIRMED"
            for action, result in zip(plan.actions, results, strict=True)
        ):
            safety_results = await self._post_execution_converge(
                fr, delta.state, targets, plan, results
            )
            results.extend(safety_results)
        await self._notify_command_results(fr.account_id, results)
        # SUBMITTED and BLOCKED are not converged: in particular, a blocked
        # protection cancel must keep the previous sizing basis so the next
        # reconciliation retries the complete safety transition.
        failed = any(r.status != "CONFIRMED" for r in results)
        await self._persist_refs(fr.account_id, targets, failed=failed, master=delta.state)
        return len(results)

    @staticmethod
    def _skip_episode_token(skip: SkipRecord, master: MasterState) -> str:
        """Return a stable bounded identity for one master copy episode.

        Normal projector state has a durable ``copy_trades.trade_id``.  Tests,
        boot snapshots, and manually composed coordinators may not have one;
        in that case hash stable leg state rather than the per-sweep event
        timestamp, so an unchanged actionable skip is emitted once.
        """
        if skip.trade_id is not None:
            return f"trade-{skip.trade_id}"
        position = master.positions.get(PositionKey(skip.symbol, skip.pos_side))
        material = {
            "symbol": skip.symbol,
            "pos_side": skip.pos_side.value,
            "qty": None if position is None else str(position.qty),
            "entry": None if position is None else str(position.entry_price),
            "stop": (
                None if position is None or position.stop is None else str(position.stop.stop_price)
            ),
            "tp": (
                None
                if position is None or position.take_profit is None
                else str(position.take_profit.stop_price)
            ),
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return f"state-{digest}"

    async def _notify_command_results(self, account_id: str, results: list) -> None:
        """Report non-terminal command results without exposing raw errors."""
        safe_account = _safe_fragment(account_id)
        for result in results:
            if result.status == "CONFIRMED":
                continue
            safe_status = _safe_fragment(result.status)
            safe_reason = _safe_fragment(result.error or result.status)
            await self._notify(
                (
                    f"Follower {safe_account}: команда не подтверждена "
                    f"({safe_status}, категория: {safe_reason})."
                ),
                event_key=(
                    f"degraded:command:{safe_account}:{_safe_fragment(result.command_key)}:"
                    f"{safe_status}:{safe_reason}"
                ),
                account_id=account_id,
                kind="execution.degraded",
                severity=NotificationSeverity.WARN,
            )

    async def _post_execution_converge(
        self,
        fr: FollowerRuntime,
        master: MasterState,
        targets: list[TargetPosition],
        executed_plan: ExecutionPlan,
        executed_results: list,
    ) -> list:
        """Synchronously protect the authoritative post-fill remainder.

        Market RESULT can differ from the planned quantity (partial fill or a
        concurrent protective exit). The outer follower lock is still held, so
        this snapshot and any safety mutation belong to the same generation.
        If an entry/increase cannot obtain a complete Stop after one immediate
        retry, the affected leg is flattened. For a small single-operator bot,
        closing the whole leg is safer and simpler than tracking a synthetic
        partially protected sub-position.
        """
        actual = await self._snapshot(fr)
        adjusted = self._targets_at_actual_qty(targets, actual)
        protection_plan = build_plan(
            actual,
            TargetState(account_id=fr.account_id, targets=adjusted),
            self._filters_provider(),  # type: ignore[arg-type]
            self._coid_factory(),
            working_type=self._working_type,
        )
        # Keep only protection/cancel actions. A protection here belongs to an
        # ALREADY-CONFIRMED entry whose leverage the exchange accepted, so any
        # intra-plan leverage prerequisite is dropped: a dangling reference
        # would wrongly BLOCK safety-critical post-fill protection.
        protection_plan = dc_replace(
            protection_plan,
            actions=[
                dc_replace(action, depends_on_action_id=None)
                if action.depends_on_action_id is not None
                else action
                for action in protection_plan.actions
                if action.kind
                in (ActionKind.PLACE_STOP, ActionKind.PLACE_TP, ActionKind.CANCEL_ORDER)
            ],
        )
        results: list = []
        if protection_plan.actions:
            protection_plan = await self._bind_plan_context(
                fr.account_id, adjusted, protection_plan, actual
            )
            results.extend(await self._executor.submit(protection_plan, client=fr.client))

        verified = await self._snapshot(fr)
        verified_targets = self._targets_at_actual_qty(targets, verified)
        verification = build_plan(
            verified,
            TargetState(account_id=fr.account_id, targets=verified_targets),
            self._filters_provider(),  # type: ignore[arg-type]
            self._coid_factory(),
            working_type=self._working_type,
        )
        unresolved_stop_keys = {
            PositionKey(
                action.symbol,
                PosSide(action.payload.get("positionSide", PosSide.BOTH.value)),
            )
            for action in verification.actions
            if action.kind == ActionKind.PLACE_STOP
        }
        opened_keys = {
            PositionKey(
                action.symbol,
                PosSide(action.payload.get("positionSide", PosSide.BOTH.value)),
            )
            for action, result in zip(executed_plan.actions, executed_results, strict=True)
            if action.kind == ActionKind.MARKET_OPEN and result.status == "CONFIRMED"
        }
        unsafe_opened = opened_keys & unresolved_stop_keys
        # PM-B6/B7: the same safety net covers PRE-EXISTING legs in the
        # authoritative post-fill snapshot: a naked leg (no usable master
        # stop AND no own stop) or a liquidation-vs-stop violated leg is
        # flattened even though no market action just opened it. Never
        # invent a stop level here (stop_price=None).
        safety_reasons: dict[PositionKey, ZeroReason] = {
            key: ZeroReason.SAFETY_UNPROTECTED_ENTRY for key in unsafe_opened
        }
        for key, pos in verified.positions.items():
            if pos.qty == 0 or key.symbol not in self._whitelist:
                continue
            mpos = master.positions.get(key)
            if mpos is None or mpos.qty == 0:
                continue  # master-closed legs converge through reconciliation
            master_stop = mpos.stop.stop_price if mpos.stop else None
            if not self._stop_usable(mpos, master_stop) and pos.stop is None:
                safety_reasons.setdefault(key, ZeroReason.SAFETY_NAKED)
                continue
            effective = (
                master_stop
                if self._stop_usable(mpos, master_stop)
                else (pos.stop.stop_price if pos.stop is not None else None)
            )
            if effective is not None and self._liquidation_violated(pos, effective):
                safety_reasons.setdefault(key, ZeroReason.LIQUIDATION_VIOLATION)
        if not safety_reasons:
            return results

        zero_targets = [
            TargetPosition(
                key=key,
                qty=Decimal(0),
                stop_price=None,
                tp_price=None,
                leverage=verified.positions[key].leverage,
                hold_flat_until_episode_end=False,
                zero_reason=safety_reasons[key],
            )
            for key in sorted(safety_reasons, key=lambda k: (k.symbol, k.pos_side.value))
            if key in verified.positions and verified.positions[key].qty != 0
        ]
        if zero_targets:
            close_plan = build_plan(
                verified,
                TargetState(account_id=fr.account_id, targets=zero_targets),
                self._filters_provider(),  # type: ignore[arg-type]
                self._coid_factory(),
                working_type=self._working_type,
            )
            close_plan = await self._bind_plan_context(
                fr.account_id, zero_targets, close_plan, verified
            )
            results.extend(await self._executor.submit(close_plan, client=fr.client))
        reason = ",".join(f"{key.symbol}:{key.pos_side.value}" for key in safety_reasons)
        await self._app_flags.set_flag(
            "degraded", f"{fr.account_id}:unprotected-entry-flattened:{reason}"
        )
        safe_account = _safe_fragment(fr.account_id)
        safe_reason = _safe_fragment(reason)
        master_stamp = _safe_fragment(master.event_time, "snapshot")
        await self._notify(
            f"Follower {safe_account}: небезопасный вход закрыт ({safe_reason}).",
            event_key=f"unsafe:{safe_account}:{master_stamp}:{safe_reason}",
            account_id=fr.account_id,
            kind="risk.unsafe_flatten",
            severity=NotificationSeverity.CRITICAL,
        )
        return results

    @staticmethod
    def _targets_at_actual_qty(
        targets: list[TargetPosition], actual: AccountState
    ) -> list[TargetPosition]:
        adjusted: list[TargetPosition] = []
        for target in targets:
            position = actual.positions.get(target.key)
            qty = position.qty if position is not None else Decimal(0)
            adjusted.append(dc_replace(target, qty=qty))
        return adjusted

    async def _persist_refs(
        self,
        account_id: str,
        targets: list[TargetPosition],
        *,
        failed: bool,
        master: MasterState,
    ) -> None:
        """Update the persisted sizing basis AFTER execution.

        Safe direction: crashing before this point leaves the previous ref, so
        the next sweep recomputes the same reduction (planner no-ops once done).
        On FAILED commands the ref stays stale on purpose - the transition is
        retried against the old baseline instead of being forgotten.
        """
        if self._basis is None or failed:
            return
        for t in targets:
            key = t.key
            if t.qty == 0:
                mpos = master.positions.get(key)
                master_still_open = mpos is not None and mpos.qty != 0
                if (
                    t.hold_flat_until_episode_end
                    and t.zero_reason is ZeroReason.PROPORTIONAL_REDUCTION_DUST
                    and master_still_open
                ):
                    # Sticky dust-flat INSIDE an open master episode: the
                    # same-direction proportional target rounded down to zero.
                    # Persist the marker instead of clearing the row, or the
                    # next recompute would see "master open + follower flat"
                    # and immediately re-open the position just closed.
                    # ONLY the dust reason latches: risk-cap / stop-move /
                    # flip / stale / manual / safety zeros clear below so the
                    # same episode may re-enter once its cause clears (Dust-2).
                    await self._basis.mark_zero_target(account_id, key.symbol, key.pos_side.value)
                else:
                    # Either the master episode really ended, or this zero is
                    # TRANSITIONAL (master flip: close this side now, open the
                    # other side next pass). Full cleanup - latching here would
                    # block the reversed entry forever.
                    await self._basis.clear(account_id, key.symbol, key.pos_side.value)
                continue
            mpos = master.positions.get(key)
            if mpos is not None:
                await self._basis.upsert_ref(
                    account_id, key.symbol, key.pos_side.value, abs(mpos.qty)
                )

    async def _snapshot(self, fr: FollowerRuntime) -> AccountState:
        balances = await fr.client.balance(fr.account_id)
        available = Decimal(0)
        equity = Decimal(0)
        for b in balances:
            if b.asset == "USDT":
                available = b.available_balance
                equity = getattr(b, "balance", available) + getattr(b, "cross_un_pnl", Decimal(0))
                break
        positions: dict[PositionKey, PositionState] = {}
        scope = self._snapshot_scope.get()
        if scope:
            batches = await asyncio.gather(
                *(fr.client.open_orders(fr.account_id, symbol=symbol) for symbol in sorted(scope))
            )
            orders = [order for batch in batches for order in batch]
        else:
            orders = await fr.client.open_orders(fr.account_id)  # all-symbol authoritative fetch
        orders_by_symbol: dict[str, list] = {}
        for o in orders:
            orders_by_symbol.setdefault(o.symbol, []).append(o)
        leverages_by_symbol: dict[str, set[int]] = {}
        for p in await fr.client.position_risk(fr.account_id):
            leverage = int(p.leverage)
            if leverage > 0:
                # Leverage is exchange-persisted per symbol and present on
                # every positionRisk row, INCLUDING flat legs. Dropping the
                # row used to make "no position" look like "leverage unknown"
                # and forced a redundant cold-path SET before every entry.
                leverages_by_symbol.setdefault(p.symbol, set()).add(leverage)
            key = PositionKey(symbol=p.symbol, pos_side=p.position_side)
            leg_orders = [
                o
                for o in orders_by_symbol.get(p.symbol, [])
                if o.position_side in (key.pos_side, PosSide.BOTH)
            ]
            if p.position_amt == 0 and key.pos_side == PosSide.BOTH and not leg_orders:
                continue
            stop, extra_stops = _pick_with_extras(leg_orders, STOP_TYPES)
            tp, extra_tps = _pick_with_extras(leg_orders, TP_TYPES)
            positions[key] = PositionState(
                key=key,
                qty=p.position_amt,
                entry_price=p.entry_price,
                leverage=leverage,
                mark_price=p.mark_price,
                liquidation_price=p.liquidation_price,
                stop=stop,
                take_profit=tp,
                extra_stops=extra_stops,
                extra_tps=extra_tps,
                unrealized_profit=getattr(p, "un_realized_profit", Decimal(0)),
            )
        # orphan protection: a conditional resting on the exchange WITHOUT a
        # backing positionRisk row (flat leg). Not surfaced -> never canceled.
        CONDITIONAL_TYPES = STOP_TYPES + TP_TYPES
        for o in orders:
            if o.type not in CONDITIONAL_TYPES or not _bot_owned(o):
                continue
            key = PositionKey(symbol=o.symbol, pos_side=o.position_side)
            if key in positions:
                continue
            leg_orders = [
                x for x in orders_by_symbol.get(o.symbol, []) if x.position_side == o.position_side
            ]
            stop, extra_stops = _pick_with_extras(leg_orders, STOP_TYPES)
            tp, extra_tps = _pick_with_extras(leg_orders, TP_TYPES)
            positions[key] = PositionState(
                key=key,
                qty=Decimal(0),
                entry_price=Decimal(0),
                leverage=0,
                mark_price=Decimal(0),
                liquidation_price=Decimal(0),
                stop=stop,
                take_profit=tp,
                extra_stops=extra_stops,
                extra_tps=extra_tps,
            )
        # Only an UNAMBIGUOUS per-symbol value counts as knowledge; disagreeing
        # rows (isolated per-side leverage) stay unknown and fail closed.
        symbol_leverages = {
            symbol: next(iter(values))
            for symbol, values in leverages_by_symbol.items()
            if len(values) == 1
        }
        if self._leverage is not None and symbol_leverages:
            self._leverage.observe(fr.account_id, symbol_leverages)
        return AccountState(
            account_id=fr.account_id,
            available_usdt=available,
            equity_usdt=equity,
            positions=positions,
            symbol_leverages=symbol_leverages,
            open_orders=[o.model_dump() for o in orders],
        )

    async def _targets_for(
        self,
        account_id: str,
        master: MasterState,
        actual: AccountState,
        policy: RiskPolicy,
        *,
        client: AsterClient | None = None,
        master_fresh: bool | None = None,
    ) -> tuple[list[TargetPosition], list[SkipRecord]]:
        targets: list[TargetPosition] = []
        skips: list[SkipRecord] = []
        if master_fresh is None:
            master_fresh = self._state_fresh(master)
        entries_allowed = await self._entries_allowed(policy)
        for key in actual.positions:
            if key.symbol not in self._whitelist:
                continue
            if master.positions.get(key) is None or master.positions[key].qty == 0:
                # follower holds a position the master no longer has -> close
                targets.append(
                    TargetPosition(
                        key=key,
                        qty=Decimal(0),
                        stop_price=None,
                        tp_price=None,
                        leverage=actual.positions[key].leverage,
                        zero_reason=ZeroReason.MASTER_CLOSED,
                    )
                )
        for key, master_pos in master.positions.items():
            if key.symbol not in self._whitelist:
                log.debug("symbol %s not whitelisted - skipped", key.symbol)
                continue
            master_qty = master_pos.qty
            master_stop = master_pos.stop.stop_price if master_pos.stop else None
            master_tp = master_pos.take_profit.stop_price if master_pos.take_profit else None
            master_stop_type = master_pos.stop.type if master_pos.stop else "STOP_MARKET"
            master_tp_type = (
                master_pos.take_profit.type if master_pos.take_profit else "TAKE_PROFIT_MARKET"
            )

            if master_qty == 0:
                continue  # handled by the actual-side loop above

            follower_pos = actual.positions.get(key)

            if follower_pos is None or follower_pos.qty == 0:
                if not self._stop_usable(master_pos, master_stop):
                    # stop on the wrong side of the CURRENT market: unusable as
                    # copy level. Never validated against the historical entry -
                    # that misclassified legitimate profit-locking stops.
                    skips.append(await self._skip_record(key, SkipReason.NO_STOP))
                    continue
                # expected fill price is the CURRENT market, not the master's
                # historical entry; the deviation gate compares the two
                expected = (
                    master_pos.mark_price if master_pos.mark_price > 0 else master_pos.entry_price
                )
                if (
                    master_pos.entry_price > 0
                    and expected > 0
                    and deviation_exceeded(master_pos.entry_price, expected, policy)
                ):
                    skips.append(await self._skip_record(key, SkipReason.ENTRY_DEVIATION))
                    continue
                # a-precondition: never open against a stale view of the master -
                # the position may already be closed on the exchange while the WS
                # event is still in flight (observed live on testnet)
                if not master_fresh:
                    skips.append(await self._skip_record(key, SkipReason.STALE_MASTER))
                    continue
                # a-precondition: no re-entry while the same master episode was
                # already ended by THIS follower's own protective fill (stop OR tp)
                if await self._ended_by_own_protective(account_id, key):
                    skips.append(await self._skip_record(key, SkipReason.OWN_PROTECTIVE_FILLED))
                    continue
                # a-precondition: the follower is flat on purpose INSIDE this
                # master episode - a proportional reduction target rounded down
                # to zero (dust leg). Without the marker the post-fill
                # recompute would treat the intentional close as drift and
                # immediately re-open. A NEW master episode clears it.
                if self._basis is not None and await self._basis.is_zero_target(
                    account_id, key.symbol, key.pos_side.value
                ):
                    skips.append(await self._skip_record(key, SkipReason.INTENTIONAL_FLAT))
                    continue
                if not entries_allowed:
                    skips.append(await self._skip_record(key, SkipReason.PAUSED))
                    continue
                # Leverage readiness is the final entry precondition: the
                # control plane must hold a confirmed value for THIS account
                # and generation before any new exposure. Not-ready skips the
                # entry (never blocks closes/reductions/protection) and
                # enqueues a bounded background preparation.
                if self._leverage is not None:
                    if not self._leverage.is_ready(account_id, key.symbol):
                        self._leverage.request_prepare(account_id, key.symbol)
                        skips.append(await self._skip_record(key, SkipReason.LEVERAGE_NOT_READY))
                        continue
                    leverage = self._apply_operator_cap(
                        self._leverage.confirmed(account_id, key.symbol)
                    )
                else:
                    # Legacy test seam without a composed control plane.
                    leverage = await self._target_leverage(
                        account_id,
                        key.symbol,
                        master_pos,
                        policy,
                        client=client,
                    )
                # PM-B7 entry gate: never open at a leverage whose estimated
                # liquidation already breaches the stop margin - post-fill
                # would instantly flatten it and the transient zero would
                # re-open it in a loop. Unviable sizing fails closed below
                # via the margin check (INSUFFICIENT_MARGIN skip, no loop).
                leverage = self._entry_leverage_for_stop(expected, master_stop, policy, leverage)
                decision = entry_quantity(
                    policy,
                    actual.available_usdt,
                    expected,
                    master_stop,
                    _filters_for(self._filters_provider(), key.symbol),
                    self._fee_buffer,
                    leverage=leverage,
                )
                if decision.reason is not None:
                    log.warning(
                        "DEBUG entry skip %s %s: eq=%s entry=%s stop=%s reason=%s",
                        account_id,
                        key.symbol,
                        actual.available_usdt,
                        expected,
                        master_stop,
                        decision.reason,
                    )
                    skips.append(await self._skip_record(key, decision.reason))
                    continue
                qty = decision.qty if master_qty > 0 else -decision.qty
                targets.append(
                    TargetPosition(
                        key=key,
                        qty=qty,
                        stop_price=master_stop,
                        tp_price=master_tp,
                        stop_type=master_stop_type,
                        tp_type=master_tp_type,
                        leverage=leverage,
                    )
                )
                continue

            current = follower_pos.qty
            current_abs = abs(current)
            effective_stop, _ = self._effective_stop(master_pos, master_stop, follower_pos)
            cap_stop = effective_stop if effective_stop is not None else Decimal(0)
            cap = post_fill_cap(
                policy,
                actual.available_usdt,
                follower_pos.entry_price,
                cap_stop,
                _filters_for(self._filters_provider(), key.symbol),
                self._fee_buffer,
            )
            same_direction = current * master_qty > 0
            if not same_direction:
                # master flipped; close follower side entirely this pass.
                # TRANSITIONAL zero: the new-direction entry happens on the
                # next pass (the projector closes/opens episodes around the
                # flip). No hold-flat marker here - it would block the
                # reversed position forever.
                targets.append(
                    TargetPosition(
                        key=key,
                        qty=Decimal(0),
                        stop_price=None,
                        tp_price=None,
                        leverage=follower_pos.leverage,
                        hold_flat_until_episode_end=False,
                        zero_reason=ZeroReason.FLIP_CLOSE,
                    )
                )
                continue
            if effective_stop is None and current_abs > 0:
                # PM-B6: naked existing leg - the follower holds quantity with
                # no usable master stop AND no own stop. Safety-flatten NOW;
                # never invent a stop level (stop_price=None). Transient: the
                # same episode may re-enter once protection returns.
                targets.append(
                    TargetPosition(
                        key=key,
                        qty=Decimal(0),
                        stop_price=None,
                        tp_price=None,
                        leverage=follower_pos.leverage,
                        hold_flat_until_episode_end=False,
                        zero_reason=ZeroReason.SAFETY_NAKED,
                    )
                )
                continue
            if effective_stop is not None and self._liquidation_violated(
                follower_pos, effective_stop
            ):
                # PM-B7: the authoritative liquidation price breaches the
                # stop margin (LONG needs liquidation < stop-margin, SHORT
                # mirrored). Flatten the violated leg; never re-leverage it.
                targets.append(
                    TargetPosition(
                        key=key,
                        qty=Decimal(0),
                        stop_price=None,
                        tp_price=None,
                        leverage=follower_pos.leverage,
                        hold_flat_until_episode_end=False,
                        zero_reason=ZeroReason.LIQUIDATION_VIOLATION,
                    )
                )
                continue

            prev_abs = await self._prev_master_abs(account_id, key)
            master_abs = abs(master_qty)
            from_reduce = False
            stop_move_branch = False
            if prev_abs is not None and master_abs > prev_abs:
                # branch b: master increased -> state-based risk cap; growth is
                # forbidden against a stale view for the same reason as branch a
                target_abs = cap if master_fresh else current_abs
            elif prev_abs is not None and master_abs < prev_abs:
                # branch c takes priority over a stop change when the master
                # magnitude DROPPED vs the persisted reference (incl. restart
                # recovery, where the snapshot may carry no protective order):
                # the missed reduction must replay regardless of stop state
                from_reduce = True
                target_abs = scaled_reduce(
                    current_abs,
                    prev_abs,
                    master_abs,
                    _filters_for(self._filters_provider(), key.symbol),
                )
            elif master_stop != (
                follower_pos.stop.stop_price if follower_pos.stop is not None else None
            ):
                # branch d/e: master moved the stop; recompute the risk cap from
                # the new distance. Farther stop -> smaller cap -> reduce (d).
                # Closer stop -> larger cap, but auto-increase is forbidden (e),
                # so clamp to current.
                cap_moved = post_fill_cap(
                    policy,
                    actual.available_usdt,
                    follower_pos.entry_price,
                    cap_stop,
                    _filters_for(self._filters_provider(), key.symbol),
                    self._fee_buffer,
                )
                target_abs = min(current_abs, max(cap_moved, Decimal(0)))
                stop_move_branch = True
            else:
                # branch c fallback: no ref yet -> reference equals current
                # master magnitude, i.e. nothing to do size-wise
                reference = prev_abs if prev_abs else master_abs
                from_reduce = True
                target_abs = scaled_reduce(
                    current_abs,
                    reference,
                    master_abs,
                    _filters_for(self._filters_provider(), key.symbol),
                )
            if not entries_allowed and target_abs > current_abs:
                target_abs = current_abs  # pause: increases blocked
            if (
                self._leverage is not None
                and target_abs > current_abs
                and not self._leverage.is_ready(account_id, key.symbol)
            ):
                # Not-ready control plane blocks only the risk-INCREASING part;
                # reductions, closes and protection updates keep flowing.
                self._leverage.request_prepare(account_id, key.symbol)
                target_abs = current_abs
            # post-fill protection: never hold more than the real risk cap from
            # the ACTUAL follower entry and the effective stop distance
            # PM-B5 explicit zero taxonomy. ONLY a proportional same-direction
            # reduction rounded down to zero (branch c / fallback) latches the
            # sticky dust marker. A zero from the state-based risk cap
            # (branch b / post-fill clamp) or a stop-move recompute (branch
            # d/e) is transient and MUST allow same-episode re-entry (Dust-2).
            pre_cap_dust = target_abs == 0 and current_abs > 0 and from_reduce
            # The cap clamp below cannot change provenance: pre-cap dust stays
            # sticky dust, a fresh cap zero stays transient.
            if effective_stop is not None and cap < current_abs and master_fresh:
                target_abs = min(target_abs, cap)
            if target_abs == 0 and current_abs > 0:
                if pre_cap_dust:
                    zero_reason = ZeroReason.PROPORTIONAL_REDUCTION_DUST
                    dust_flat = True
                elif stop_move_branch:
                    zero_reason = ZeroReason.STOP_MOVE
                    dust_flat = False
                else:
                    zero_reason = ZeroReason.RISK_CAP
                    dust_flat = False
            else:
                zero_reason = None
                dust_flat = False
            target_stop, target_stop_type = self._target_stop_for_existing(
                master_pos, master_stop, master_stop_type, follower_pos
            )
            leverage = follower_pos.leverage
            if target_abs > current_abs:
                if self._leverage is not None:
                    # Ready (clamped above otherwise): size against the
                    # account-confirmed value instead of a network read.
                    confirmed = self._leverage.confirmed(account_id, key.symbol)
                    if confirmed is not None:
                        capped = self._apply_operator_cap(confirmed)
                        leverage = capped if capped is not None else confirmed
                else:
                    # Legacy test seam without a composed control plane.
                    # Existing-leg reductions, closes and protection updates
                    # keep running if the bracket read is unavailable; only a
                    # real increase needs the exchange maximum.
                    leverage = await self._target_leverage(
                        account_id,
                        key.symbol,
                        master_pos,
                        policy,
                        client=client,
                    )
            qty = target_abs if master_qty > 0 else -target_abs
            targets.append(
                TargetPosition(
                    key=key,
                    qty=qty,
                    stop_price=target_stop,
                    tp_price=master_tp,
                    stop_type=target_stop_type,
                    tp_type=master_tp_type,
                    leverage=leverage,
                    hold_flat_until_episode_end=dust_flat,
                    zero_reason=zero_reason,
                )
            )
        if self._max_total_risk_pct is not None:
            from copytradeaster.risk.account_budget import reserve_account_budget

            targets = reserve_account_budget(
                actual,
                targets,
                master.positions,
                self._filters_provider(),
                max_total_risk_pct=self._max_total_risk_pct,
                fee_buffer_pct=self._fee_buffer,
            )
        return targets, skips

    async def _target_leverage(
        self,
        account_id: str,
        symbol: str,
        master_pos: PositionState,
        policy: RiskPolicy,
        *,
        client: AsterClient | None,
    ) -> int:
        """Legacy/test-seam leverage resolution (NO control plane composed).

        Production reconciliation uses ``LeverageManager`` RAM reads and never
        enters this method. ``targets_for`` remains a small public test seam
        and may be called without a runtime client, where the policy/master
        value keeps existing pure sizing tests useful; a client exposing
        ``max_leverage`` keeps the historical exchange-resolution tests valid.
        """
        if client is not None:
            resolver = getattr(client, "max_leverage", None)
            if callable(resolver):
                value = await resolver(account_id, symbol)
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError("invalid exchange maximum leverage")
                return self._apply_operator_cap(value)
            if isinstance(client, AsterClient):
                raise RuntimeError("AsterClient maximum leverage operation is unavailable")
        resolved = (
            min(master_pos.leverage, policy.max_leverage)
            if master_pos.leverage > 0
            else policy.max_leverage
        )
        return self._apply_operator_cap(resolved)

    async def _skip_record(self, key: PositionKey, reason: SkipReason) -> SkipRecord:
        """Attach the durable master episode id when one is available."""
        active = await self._trades.active_trade(key.symbol, key.pos_side.value)
        trade_id = int(active["trade_id"]) if active is not None else None
        return SkipRecord(
            symbol=key.symbol,
            reason=reason,
            pos_side=key.pos_side,
            trade_id=trade_id,
        )

    def _apply_operator_cap(self, leverage: int | None) -> int | None:
        """Clamp a resolved leverage to the operator safety ceiling (PM-B7).

        None (unconfigured ceiling, or unknown control-plane value) passes
        through untouched; a non-positive incoming value is never raised.
        """
        if leverage is None:
            return None
        if self._operator_max_leverage is not None and leverage > self._operator_max_leverage:
            return self._operator_max_leverage
        return leverage

    def _liquidation_violated(self, follower_pos: PositionState, stop: Decimal) -> bool:
        """True when the authoritative liquidation price breaches the stop
        margin (PM-B7): LONG requires liquidation < stop*(1-buffer), SHORT
        mirrored. Unknown/zero liquidation or stop never counts as a
        violation - the leg is judged by the naked-stop rule instead.

        Entry price cannot identify an invalid liquidation quote: the mark
        may have moved into profit and cross-HEDGE legs can share a price.
        Compare only the adverse direction relative to the current mark.
        A shared price in the other direction is evaluated on the other leg;
        aggregate stop risk is reserved separately by the account budget.
        """
        liq = follower_pos.liquidation_price
        if liq <= 0 or stop <= 0 or follower_pos.qty == 0:
            return False
        is_short = follower_pos.qty < 0 or follower_pos.key.pos_side == PosSide.SHORT
        reference = follower_pos.mark_price or follower_pos.entry_price
        if reference > 0:
            if is_short and liq < reference:
                return False
            if not is_short and liq > reference:
                return False
        buffer = self._liquidation_buffer_pct
        if is_short:
            return liq <= stop * (Decimal(1) + buffer)
        return liq >= stop * (Decimal(1) - buffer)

    def _entry_leverage_for_stop(
        self,
        expected_entry: Decimal,
        stop_price: Decimal,
        policy: RiskPolicy,
        leverage: int | None,
    ) -> int | None:
        """Cap entry leverage so estimated liquidation satisfies the stop margin.

        The post-fill invariant reads the authoritative liquidation price;
        this gate keeps a fresh entry from opening what that net would
        instantly flatten (open -> flatten livelock). Estimated liquidation
        distance is entry/leverage minus a maintenance-margin cushion; the
        cap solves entry/lev >= stop_distance + entry*(buffer + cushion).
        None (unknown control-plane value) stays None unless the cap binds
        below the policy maximum; sub-1 caps clamp to 1 so the downstream
        margin check fails closed with INSUFFICIENT_MARGIN instead of looping.
        """
        stop_distance = abs(expected_entry - stop_price)
        if expected_entry <= 0 or stop_distance <= 0:
            return leverage
        per_unit = stop_distance + expected_entry * (
            self._liquidation_buffer_pct + LIQUIDATION_MMR_CUSHION
        )
        invariant_cap = int(expected_entry // per_unit)
        operator_cap = self._operator_max_leverage
        cap = invariant_cap if operator_cap is None else min(invariant_cap, operator_cap)
        if leverage is None:
            if cap >= policy.max_leverage:
                return None
            return max(1, cap)
        return max(1, min(leverage, cap))

    def _stop_usable(self, master_pos: PositionState, stop: Decimal | None) -> bool:
        """Usable = sits on the protective side of the CURRENT market price.

        Entry price is irrelevant: a long-stop above the entry is a valid
        profit lock, not an invalid level."""
        if stop is None:
            return False
        reference = master_pos.mark_price if master_pos.mark_price > 0 else master_pos.entry_price
        if reference <= 0:
            return False
        if master_pos.qty > 0:
            return stop < reference
        return stop > reference

    def _effective_stop(
        self, master_pos: PositionState, master_stop: Decimal | None, follower_pos: PositionState
    ) -> tuple[Decimal | None, str]:
        """Stop level to size against: master's when usable, else the follower's
        own current protection (never the historical entry)."""
        if self._stop_usable(master_pos, master_stop):
            return master_stop, master_pos.stop.type if master_pos.stop else "STOP_MARKET"
        if follower_pos.stop is not None:
            return follower_pos.stop.stop_price, follower_pos.stop.type
        return None, "STOP_MARKET"

    def _target_stop_for_existing(
        self,
        master_pos: PositionState,
        master_stop: Decimal | None,
        master_stop_type: str,
        follower_pos: PositionState,
    ) -> tuple[Decimal | None, str]:
        """Protection policy for an EXISTING follower position:
        - usable master level -> mirror it (incl. profit-locking stops);
        - master protection absent/unusable -> keep the follower's current
          protection (safety-first; cancelling it would leave them naked)."""
        if self._stop_usable(master_pos, master_stop):
            return master_stop, master_stop_type
        if follower_pos.stop is not None:
            return follower_pos.stop.stop_price, follower_pos.stop.type
        return None, master_stop_type

    async def _prev_master_abs(self, account_id: str, key: PositionKey) -> Decimal | None:
        if self._basis is None:
            return None
        row = await self._basis.get(account_id, key.symbol, key.pos_side.value)
        if row is None:
            return None
        value = Decimal(str(row["master_ref_abs"]))
        return value if value > 0 else None

    async def _ended_by_own_protective(self, account_id: str, key: PositionKey) -> bool:
        """True when a protective order (stop OR take-profit) of the ACTIVE
        master episode already FILLED on this follower - the episode ended for
        them and re-entry is forbidden until a new master episode opens."""
        trade = await self._trades.active_trade(key.symbol, key.pos_side.value)
        if trade is None:
            return False
        rows = await self._links.by_trade(int(trade["trade_id"]))
        links = [
            r
            for r in rows
            if r["account_id"] == account_id and r["kind"] in ("PLACE_STOP", "PLACE_TP")
        ]
        if not links:
            return False
        client = self._client_for(account_id)
        if client is None:
            return False
        for row in links:
            coid = str(row["follower_client_order_id"])
            try:
                order = await client.query_order(account_id, key.symbol, orig_client_order_id=coid)
            except Exception as exc:  # noqa: BLE001 - defensive
                log.warning("protective query failed for %s: %s", coid, exc)
                continue
            if order is not None and order.status == "FILLED":
                return True
        return False

    def _client_for(self, account_id: str):
        for fr in self._registry.snapshot():
            if fr.account_id == account_id:
                return fr.client
        return None

    def _coid_factory(self):
        def factory(kind, symbol, seq):
            return f"ct-{symbol}-{kind.value.lower()[:8]}-{seq:04d}"

        return factory

    async def _bind_plan_context(
        self,
        account_id: str,
        targets: list[TargetPosition],
        plan,
        actual: AccountState,
    ):
        """Per-action trade attribution + idempotency revision.

        trade_id: active episode of the action's own leg; fallback to the latest
        (closed) episode so close actions racing the projector still attribute
        correctly. Fixes multi-symbol/multi-leg plans writing every link under
        the first target's episode.
        revision: bumped iff the desired state for that leg differs from the
        last issued one; retries/reconciles of the same desired state reuse the
        same logical command keys.
        """
        target_by_leg = {(t.key.symbol, t.key.pos_side.value): t for t in targets}
        actions_by_leg: dict[tuple[str, str], list[tuple[int, ExecutionAction, int | None]]] = {}
        action_trade_ids: dict[int, int | None] = {}
        for index, action in enumerate(plan.actions):
            pos_side_raw = str(action.payload.get("positionSide", action.position_side.value))
            trade_id = await self._resolve_trade(action.symbol, pos_side_raw)
            action_trade_ids[index] = trade_id
            actions_by_leg.setdefault((action.symbol, pos_side_raw), []).append(
                (index, action, trade_id)
            )

        issued_revisions: dict[tuple[str, str], int] = {}
        for leg, t in target_by_leg.items():
            leg_actions = actions_by_leg.get(leg, [])
            if not leg_actions:
                continue
            canonical = _canonical_target(t)
            row = await self._basis.get(account_id, leg[0], leg[1]) if self._basis else None
            if row is None or row["issued_target"] != canonical:
                if self._basis is not None:
                    issued_revisions[leg] = await self._basis.bump_revision(
                        account_id, leg[0], leg[1], canonical
                    )
                else:
                    issued_revisions[leg] = 0
                continue

            revision = int(row["revision"])
            # A target is not enough to identify a reconciliation operation.
            # If the exchange has drifted after the old command was confirmed,
            # a new generation is required even though the desired target is
            # byte-for-byte unchanged. In-flight/FAILED commands keep their
            # revision so the executor can recover/retry the same operation.
            drifted_confirmed = False
            for _index, action, trade_id in leg_actions:
                if await self._confirmed_command_has_actual_drift(
                    plan,
                    action,
                    trade_id if trade_id is not None else plan.trade_id,
                    revision,
                    actual,
                    t,
                ):
                    drifted_confirmed = True
                    break
            if drifted_confirmed and self._basis is not None:
                revision = await self._basis.bump_revision(account_id, leg[0], leg[1], canonical)
            issued_revisions[leg] = revision

        new_actions: list[ExecutionAction] = []
        for index, action in enumerate(plan.actions):
            pos_side_raw = str(action.payload.get("positionSide", action.position_side.value))
            trade_id = action_trade_ids[index]
            new_actions.append(
                dc_replace(
                    action,
                    trade_id=trade_id if trade_id is not None else plan.trade_id,
                    revision=issued_revisions.get((action.symbol, pos_side_raw), 0),
                )
            )
        return dc_replace(plan, actions=new_actions)

    async def _confirmed_command_has_actual_drift(
        self,
        plan,
        action: ExecutionAction,
        trade_id: int | None,
        revision: int,
        actual: AccountState,
        target: TargetPosition,
    ) -> bool:
        candidate = dc_replace(action, trade_id=trade_id, revision=revision)
        key = command_key_of(plan, candidate)
        row = await self.commands.get(key)
        if row is None or str(row["status"]) != "CONFIRMED":
            return False
        return self._action_actual_drifted(action, actual, target)

    @staticmethod
    def _action_actual_drifted(
        action: ExecutionAction, actual: AccountState, target: TargetPosition
    ) -> bool:
        """Return whether a previously confirmed action's effect is absent.

        This deliberately inspects the fresh follower snapshot rather than
        inventing a second desired-state store. It is used only to create a new
        execution generation; ordinary FAILED/SUBMITTED rows still retry under
        their original command key.
        """
        position = actual.positions.get(target.key)
        kind = action.kind
        if kind == ActionKind.SET_LEVERAGE:
            return position is None or position.leverage != target.leverage
        if kind == ActionKind.MARKET_OPEN:
            return position is None or abs(position.qty) < abs(target.qty)
        if kind == ActionKind.MARKET_CLOSE:
            return position is not None and abs(position.qty) > abs(target.qty)
        if kind in (ActionKind.PLACE_STOP, ActionKind.PLACE_TP):
            current = None
            if position is not None:
                current = position.stop if kind == ActionKind.PLACE_STOP else position.take_profit
            wanted = Decimal(action.payload["stopPrice"])
            return current is None or current.stop_price != wanted
        if kind == ActionKind.CANCEL_ORDER:
            old_coid = action.replaces_client_order_id or action.payload.get("origClientOrderId")
            if old_coid is None:
                return False
            if position is not None and any(
                prot is not None and prot.client_order_id == old_coid
                for prot in (position.stop, position.take_profit)
            ):
                return True
            for raw in actual.open_orders:
                if not isinstance(raw, dict):
                    continue
                if raw.get("client_order_id", raw.get("clientOrderId")) == old_coid:
                    return True
        return False

    async def _resolve_trade(self, symbol: str, pos_side_value: str) -> int | None:
        row = await self._trades.active_trade(symbol, pos_side_value)
        if row is None:
            row = await self._trades.latest_trade(symbol, pos_side_value)
        return int(row["trade_id"]) if row is not None else None

    async def _notify(
        self,
        text: str,
        *,
        event_key: str | None = None,
        account_id: str | None = None,
        kind: str = "engine.info",
        severity: NotificationSeverity = NotificationSeverity.INFO,
    ) -> None:
        """Emit one structured event without making trading depend on it.

        ``event_key`` is supplied by each decision boundary and remains stable
        across retries/reconciles.  The legacy text callback is retained only
        for existing Stage-1 callers; the normal App path always uses the
        structured notifier.
        """
        notification_key = event_key or f"{kind}:{_safe_fragment(account_id)}"
        if notification_key in self._notification_keys_seen:
            return
        if self._notifier is not None:
            event = NotificationEvent(
                event_key=notification_key,
                account_id=account_id,
                kind=kind,
                severity=severity,
                text=text,
            )
            try:
                await self._notifier.emit(event)
            except Exception:  # noqa: BLE001 - sink failures are isolated
                log.exception(
                    "notification enqueue failed (event_key=%s kind=%s account_id=%s)",
                    event.event_key,
                    event.kind,
                    event.account_id,
                )
                return
            self._remember_notification_key(notification_key)
            return
        if self._on_notify is not None:
            try:
                if self._on_notify_accepts_event:
                    await self._on_notify(
                        NotificationEvent(
                            event_key=notification_key,
                            account_id=account_id,
                            kind=kind,
                            severity=severity,
                            text=text,
                        )
                    )
                else:
                    await self._on_notify(text)
            except Exception:  # noqa: BLE001 - legacy sink is also best-effort
                log.exception("legacy notification callback failed")
                return
            self._remember_notification_key(notification_key)

    def _remember_notification_key(self, event_key: str) -> None:
        self._notification_keys_seen.add(event_key)
        self._notification_key_order.append(event_key)
        while len(self._notification_key_order) > NOTIFICATION_KEY_CACHE_MAX:
            expired = self._notification_key_order.popleft()
            self._notification_keys_seen.discard(expired)

    @property
    def followers(self) -> list[FollowerRuntime]:
        return list(self._registry.snapshot())

    @property
    def registry(self) -> FollowerRegistry:
        return self._registry

    async def drain_postfill_tasks(
        self, timeout_s: float = 5.0, *, close_admission: bool = True
    ) -> None:
        """Finish spawned safety recomputes before transport/storage shutdown.

        Closing admission is the default so a task cannot be added between the
        final set inspection and resource teardown.  Callers that only want a
        non-shutdown drain may opt out explicitly.
        """
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if close_admission:
            await self.close_postfill_admission()
        deadline = time.monotonic() + timeout_s
        while self._postfill_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tasks = tuple(self._postfill_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                return
            tasks = tuple(self._postfill_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=remaining
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                return

    @property
    def commands(self):
        return self._executor._commands

    async def recompute_follower(
        self, fr: FollowerRuntime, delta: MasterDelta, *, snapshot_scope: set[str] | None = None
    ) -> int:
        if not await self._enter_generation():
            return -1
        # Authoritative refresh (REST sweep/boundary): supersede the WS delta
        # cache so a post-fill task spawned earlier converges on this newer
        # state instead of a stale flat capture (review 636dfff P1).
        self._last_master_state = delta.state
        self._last_master_state_at = time.monotonic()
        token = self._snapshot_scope.set(snapshot_scope)
        try:
            return await self._recompute_follower(fr, delta)
        finally:
            self._snapshot_scope.reset(token)
            await self._leave_generation()

    async def snapshot_follower(self, fr: FollowerRuntime) -> AccountState:
        """Public read boundary for application status and safety checks."""
        async with fr.lock:
            return await self._snapshot(fr)

    async def snapshot_follower_locked(self, fr: FollowerRuntime) -> AccountState:
        """Snapshot boundary for callers already holding ``fr.lock``.

        The lifecycle context acquires ``FollowerRuntime.lock`` before the
        executor account lock and uses this method without reacquiring the
        non-reentrant asyncio lock.
        """
        return await self._snapshot(fr)

    async def targets_for(
        self,
        account_id: str,
        master: MasterState,
        actual: AccountState,
        policy: RiskPolicy,
        *,
        client: AsterClient | None = None,
    ) -> tuple[list[TargetPosition], list[SkipRecord]]:
        return await self._targets_for(account_id, master, actual, policy, client=client)
