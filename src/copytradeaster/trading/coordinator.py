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
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from decimal import Decimal

from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    ExecutionAction,
    PositionKey,
    PositionState,
    PosSide,
    RiskPolicy,
    SkipReason,
    SymbolFilters,
    TargetPosition,
    TargetState,
)
from copytradeaster.logging_utils import get_logger
from copytradeaster.master.projector import MasterDelta, MasterState
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
from copytradeaster.trading.executor import Executor
from copytradeaster.trading.planner import build_plan

log = get_logger(__name__)

STOP_TYPES = ("STOP_MARKET", "STOP")
TP_TYPES = ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")

POSTFILL_RECOMPUTE_DEBOUNCE_S = 2.0


@dataclass(slots=True)
class FollowerRuntime:
    account_id: str
    client: AsterClient
    identity: object
    lock: asyncio.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()


@dataclass(slots=True)
class SkipRecord:
    symbol: str
    reason: SkipReason


def _canonical_target(t: TargetPosition) -> str:
    return json.dumps(
        {
            "qty": str(t.qty),
            "stop_price": None if t.stop_price is None else str(t.stop_price),
            "tp_price": None if t.tp_price is None else str(t.tp_price),
            "stop_type": t.stop_type,
            "tp_type": t.tp_type,
            "leverage": t.leverage,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _filters_for(filters_by_symbol: dict[str, SymbolFilters], symbol: str) -> SymbolFilters:
    filters = filters_by_symbol.get(symbol)
    if filters is None:
        raise KeyError(f"no filters for symbol {symbol}")
    return filters


def _pick(orders: list, order_types: tuple[str, ...]):
    """Newest protective order across flavour variants (STOP_MARKET/STOP etc.)."""
    candidates = [o for o in orders if o.type in order_types]
    if not candidates:
        return None
    return max(candidates, key=lambda o: o.update_time)


class Coordinator:
    def __init__(
        self,
        followers: list[FollowerRuntime],
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
        basis: SyncBasisRepo | None = None,
    ) -> None:
        self._followers = followers
        self._settings = settings
        self._executor = executor
        self._trades = trades
        self._links = links
        self._filters_provider = filters_provider
        self._app_flags = app_flags
        self._whitelist = set(symbol_whitelist)
        self._fee_buffer = fee_buffer_pct
        self._working_type = working_type
        self._on_notify = on_notify
        self._stale_master_ms = stale_master_ms
        self._error_streaks: dict[str, int] = {}
        self._basis = basis
        self._last_master_state: MasterState | None = None
        self._last_postfill_recompute: dict[str, float] = {}
        self._postfill_tasks: set[asyncio.Task] = set()

    async def on_master_delta(self, delta: MasterDelta) -> None:
        self._last_master_state = delta.state
        results = await asyncio.gather(
            *(self._recompute_follower(fr, delta) for fr in self._followers),
            return_exceptions=True,
        )
        for fr, result in zip(self._followers, results, strict=True):
            if isinstance(result, Exception):
                streak = self._error_streaks.get(fr.account_id, 0) + 1
                self._error_streaks[fr.account_id] = streak
                log.error("follower %s recompute failed (%d): %s", fr.account_id, streak, result)
                if streak >= 3:
                    await self._app_flags.set_flag(
                        "degraded", f"{fr.account_id}:recompute:{result}"
                    )
                await self._notify(f"[{fr.account_id}] recompute error: {result}")
            else:
                self._error_streaks[fr.account_id] = 0

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
        fr = next((f for f in self._followers if f.account_id == account_id), None)
        state = self._last_master_state
        if fr is None or state is None:
            return
        task = asyncio.create_task(self._postfill_recompute(fr, state))
        self._postfill_tasks.add(task)
        task.add_done_callback(self._postfill_tasks.discard)

    async def _postfill_recompute(self, fr: FollowerRuntime, state: MasterState) -> None:
        try:
            await self._recompute_follower(fr, MasterDelta(frozenset({"*"}), state))
        except Exception as exc:  # noqa: BLE001 - notification path must not raise
            log.warning("post-fill recompute failed for %s: %s", fr.account_id, exc)

    def _state_fresh(self, master: MasterState) -> bool:
        """False when the master state predates stale_master_ms; event_time==0
        (manual/snapshot state used by tests and boot) counts as fresh."""
        if master.event_time <= 0:
            return True
        return (time.time() * 1000 - master.event_time) <= self._stale_master_ms

    async def _entries_allowed(self, policy: RiskPolicy) -> bool:
        """Both the per-follower policy AND the global CLI pause must allow."""
        if not policy.allow_new_entries:
            return False
        flag = await self._app_flags.get_flag("allow_new_entries")
        return flag != "false"

    async def _recompute_follower(self, fr: FollowerRuntime, delta: MasterDelta) -> None:
        policy = await self._settings.get_policy(fr.account_id)
        if policy is None or not policy.enabled:
            return
        actual = await self._snapshot(fr)
        targets, skips = await self._targets_for(fr.account_id, delta.state, actual, policy)
        for skip in skips:
            await self._notify(f"[{fr.account_id}] SKIP {skip.symbol}: {skip.reason}")
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
            return
        log.info(
            "[%s] plan: %s",
            fr.account_id,
            [
                (a.kind.value, a.payload.get("quantity") or a.payload.get("leverage"))
                for a in plan.actions
            ],
        )
        plan = await self._bind_plan_context(fr.account_id, targets, plan)
        results = await self._executor.submit(plan)
        failed = any(r.status == "FAILED" for r in results)
        await self._persist_refs(fr.account_id, targets, failed=failed, master=delta.state)

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
        for b in balances:
            if b.asset == "USDT":
                available = b.available_balance
                break
        positions: dict[PositionKey, PositionState] = {}
        orders = await fr.client.open_orders(fr.account_id)  # single fetch: weight 40, not 80
        orders_by_symbol: dict[str, list] = {}
        for o in orders:
            orders_by_symbol.setdefault(o.symbol, []).append(o)
        for p in await fr.client.position_risk(fr.account_id):
            key = PositionKey(symbol=p.symbol, pos_side=p.position_side)
            leg_orders = [
                o for o in orders_by_symbol.get(p.symbol, [])
                if o.position_side in (key.pos_side, PosSide.BOTH)
            ]
            if p.position_amt == 0 and key.pos_side == PosSide.BOTH and not leg_orders:
                continue
            positions[key] = PositionState(
                key=key,
                qty=p.position_amt,
                entry_price=p.entry_price,
                leverage=int(p.leverage),
                mark_price=p.mark_price,
                liquidation_price=p.liquidation_price,
                stop=_pick(leg_orders, STOP_TYPES),
                take_profit=_pick(leg_orders, TP_TYPES),
            )
        # orphan protection: a conditional resting on the exchange WITHOUT a
        # backing positionRisk row (flat leg). Not surfaced -> never canceled.
        CONDITIONAL_TYPES = STOP_TYPES + TP_TYPES
        for o in orders:
            if o.type not in CONDITIONAL_TYPES:
                continue
            key = PositionKey(symbol=o.symbol, pos_side=o.position_side)
            if key in positions:
                continue
            leg_orders = [
                x for x in orders_by_symbol.get(o.symbol, [])
                if x.position_side == o.position_side
            ]
            positions[key] = PositionState(
                key=key,
                qty=Decimal(0),
                entry_price=Decimal(0),
                leverage=0,
                mark_price=Decimal(0),
                liquidation_price=Decimal(0),
                stop=_pick(leg_orders, STOP_TYPES),
                take_profit=_pick(leg_orders, TP_TYPES),
            )
        return AccountState(
            account_id=fr.account_id,
            available_usdt=available,
            positions=positions,
            open_orders=[o.model_dump() for o in orders],
        )

    async def _targets_for(
        self, account_id: str, master: MasterState, actual: AccountState, policy: RiskPolicy
    ) -> tuple[list[TargetPosition], list[SkipRecord]]:
        targets: list[TargetPosition] = []
        skips: list[SkipRecord] = []
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
            leverage = (
                min(master_pos.leverage, policy.max_leverage)
                if master_pos.leverage > 0
                else policy.max_leverage
            )

            if follower_pos is None or follower_pos.qty == 0:
                if not self._stop_usable(master_pos, master_stop):
                    # stop on the wrong side of the CURRENT market: unusable as
                    # copy level. Never validated against the historical entry -
                    # that misclassified legitimate profit-locking stops.
                    skips.append(SkipRecord(key.symbol, SkipReason.NO_STOP))
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
                    skips.append(SkipRecord(key.symbol, SkipReason.ENTRY_DEVIATION))
                    continue
                # a-precondition: never open against a stale view of the master -
                # the position may already be closed on the exchange while the WS
                # event is still in flight (observed live on testnet)
                if not self._state_fresh(master):
                    skips.append(SkipRecord(key.symbol, SkipReason.STALE_MASTER))
                    continue
                # a-precondition: no re-entry while the same master episode was
                # already ended by THIS follower's own protective fill (stop OR tp)
                if await self._ended_by_own_protective(account_id, key):
                    skips.append(SkipRecord(key.symbol, SkipReason.OWN_PROTECTIVE_FILLED))
                    continue
                if not entries_allowed:
                    skips.append(SkipRecord(key.symbol, SkipReason.PAUSED))
                    continue
                decision = entry_quantity(
                    policy,
                    actual.available_usdt,
                    expected,
                    master_stop,
                    _filters_for(self._filters_provider(), key.symbol),
                    self._fee_buffer,
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
                    skips.append(SkipRecord(key.symbol, decision.reason))
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
                # master flipped; close follower side entirely this pass
                targets.append(
                    TargetPosition(
                        key=key, qty=Decimal(0), stop_price=None, tp_price=None, leverage=leverage
                    )
                )
                continue

            prev_abs = await self._prev_master_abs(account_id, key)
            master_abs = abs(master_qty)
            if prev_abs is not None and master_abs > prev_abs:
                # branch b: master increased -> state-based risk cap; growth is
                # forbidden against a stale view for the same reason as branch a
                target_abs = cap if self._state_fresh(master) else current_abs
            elif prev_abs is not None and master_abs < prev_abs:
                # branch c takes priority over a stop change when the master
                # magnitude DROPPED vs the persisted reference (incl. restart
                # recovery, where the snapshot may carry no protective order):
                # the missed reduction must replay regardless of stop state
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
            else:
                # branch c fallback: no ref yet -> reference equals current
                # master magnitude, i.e. nothing to do size-wise
                reference = prev_abs if prev_abs else master_abs
                target_abs = scaled_reduce(
                    current_abs,
                    reference,
                    master_abs,
                    _filters_for(self._filters_provider(), key.symbol),
                )
            if not entries_allowed and target_abs > current_abs:
                target_abs = current_abs  # pause: increases blocked
            # post-fill protection: never hold more than the real risk cap from
            # the ACTUAL follower entry and the effective stop distance
            if effective_stop is not None and cap < current_abs and self._state_fresh(master):
                target_abs = min(target_abs, cap)
            target_stop, target_stop_type = self._target_stop_for_existing(
                master_pos, master_stop, master_stop_type, follower_pos
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
                )
            )
        return targets, skips

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
        for fr in self._followers:
            if fr.account_id == account_id:
                return fr.client
        return None

    def _coid_factory(self):
        def factory(kind, symbol, seq):
            return f"ct-{symbol}-{kind.value.lower()[:8]}-{seq:04d}"

        return factory

    async def _bind_plan_context(self, account_id: str, targets: list[TargetPosition], plan):
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
        issued_revisions: dict[tuple[str, str], int] = {}
        for leg, t in target_by_leg.items():
            canonical = _canonical_target(t)
            row = await self._basis.get(account_id, leg[0], leg[1]) if self._basis else None
            if row is not None and row["issued_target"] == canonical:
                issued_revisions[leg] = int(row["revision"])
            elif self._basis is not None:
                issued_revisions[leg] = await self._basis.bump_revision(
                    account_id, leg[0], leg[1], canonical
                )
            else:
                issued_revisions[leg] = 0

        new_actions: list[ExecutionAction] = []
        for a in plan.actions:
            pos_side_raw = str(a.payload.get("positionSide", PosSide.BOTH.value))
            trade_id = await self._resolve_trade(a.symbol, pos_side_raw)
            new_actions.append(
                dc_replace(
                    a,
                    trade_id=trade_id if trade_id is not None else plan.trade_id,
                    revision=issued_revisions.get((a.symbol, pos_side_raw), 0),
                )
            )
        return dc_replace(plan, actions=new_actions)

    async def _resolve_trade(self, symbol: str, pos_side_value: str) -> int | None:
        row = await self._trades.active_trade(symbol, pos_side_value)
        if row is None:
            row = await self._trades.latest_trade(symbol, pos_side_value)
        return int(row["trade_id"]) if row is not None else None

    async def _notify(self, text: str) -> None:
        if self._on_notify is not None:
            await self._on_notify(text)

    @property
    def followers(self) -> list[FollowerRuntime]:
        return list(self._followers)

    @property
    def commands(self):
        return self._executor._commands

    async def recompute_follower(self, fr: FollowerRuntime, delta: MasterDelta) -> None:
        await self._recompute_follower(fr, delta)

    async def targets_for(
        self, account_id: str, master: MasterState, actual: AccountState, policy: RiskPolicy
    ) -> tuple[list[TargetPosition], list[SkipRecord]]:
        return await self._targets_for(account_id, master, actual, policy)
