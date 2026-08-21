"""Coordinator: master deltas -> per-follower target states -> plans -> executor.

Fan-out is concurrent and isolated: one follower's error never blocks the rest;
3 consecutive follower errors raise the app-level degraded flag.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.domain.models import (
    AccountState,
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
from copytradeaster.risk.engine import entry_quantity, post_fill_cap, scaled_reduce
from copytradeaster.storage.repositories import CopyTradeRepo, OrderLinkRepo, SettingsRepo
from copytradeaster.trading.executor import Executor
from copytradeaster.trading.planner import build_plan

log = get_logger(__name__)


@dataclass(slots=True)
class FollowerRuntime:
    account_id: str
    client: AsterClient
    identity: object
    lock: asyncio.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()


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
        self._master_abs: dict[tuple[str, str], Decimal] = {}

    async def on_master_delta(self, delta: MasterDelta) -> None:
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

    def _state_fresh(self, master: MasterState) -> bool:
        """False when the master state predates stale_master_ms; event_time==0
        (manual/snapshot state used by tests and boot) counts as fresh."""
        if master.event_time <= 0:
            return True
        import time

        return (time.time() * 1000 - master.event_time) <= self._stale_master_ms

    def _remember_single(self, account_id: str, master: MasterState) -> None:
        for key, pos in master.positions.items():
            self._master_abs[(account_id, f"{key.symbol}:{key.pos_side}")] = abs(pos.qty)

    async def _recompute_follower(self, fr: FollowerRuntime, delta: MasterDelta) -> None:
        policy = await self._settings.get_policy(fr.account_id)
        if policy is None or not policy.enabled:
            return
        actual = await self._snapshot(fr)
        targets, skips = await self._targets_for(fr.account_id, delta.state, actual, policy)
        # advance the master-magnitude tracker on EVERY recompute (delta and
        # sweep alike); sweeps against a stale magnitude misclassify growth and
        # oscillate targets against the WS-driven ones
        self._remember_single(fr.account_id, delta.state)
        for skip in skips:
            await self._notify(f"[{fr.account_id}] SKIP {skip.symbol}: {skip.reason}")
        plan = build_plan(
            actual,
            TargetState(account_id=fr.account_id, targets=targets),
            self._filters_provider(),  # type: ignore[arg-type]
            self._coid_factory(),
            working_type=self._working_type,
        )
        plan = await self._stamp_trade_id(fr.account_id, targets, plan)
        if plan.actions:
            log.info(
                "[%s] plan: %s",
                fr.account_id,
                [
                    (a.kind.value, a.payload.get("quantity") or a.payload.get("leverage"))
                    for a in plan.actions
                ],
            )
            await self._executor.submit(plan)

    async def _snapshot(self, fr: FollowerRuntime) -> AccountState:
        balances = await fr.client.balance(fr.account_id)
        available = Decimal(0)
        for b in balances:
            if b.asset == "USDT":
                available = b.available_balance
                break
        positions: dict[PositionKey, PositionState] = {}
        orders_by_symbol: dict[str, list] = {}
        for o in await fr.client.open_orders(fr.account_id):
            orders_by_symbol.setdefault(o.symbol, []).append(o)
        for p in await fr.client.position_risk(fr.account_id):
            key = PositionKey(symbol=p.symbol, pos_side=p.position_side)
            if p.position_amt == 0 and key.pos_side == PosSide.BOTH:
                continue
            same_side = [
                o
                for o in orders_by_symbol.get(p.symbol, [])
                if o.position_side in (key.pos_side, PosSide.BOTH)
            ]
            stop = _pick(same_side, "STOP_MARKET")
            tp = _pick(same_side, "TAKE_PROFIT_MARKET")
            positions[key] = PositionState(
                key=key,
                qty=p.position_amt,
                entry_price=p.entry_price,
                leverage=int(p.leverage),
                mark_price=p.mark_price,
                liquidation_price=p.liquidation_price,
                stop=stop,
                take_profit=tp,
            )
        return AccountState(
            account_id=fr.account_id,
            available_usdt=available,
            positions=positions,
            open_orders=[o.model_dump() for o in await fr.client.open_orders(fr.account_id)],
        )

    async def _targets_for(
        self, account_id: str, master: MasterState, actual: AccountState, policy: RiskPolicy
    ) -> tuple[list[TargetPosition], list[SkipRecord]]:
        targets: list[TargetPosition] = []
        skips: list[SkipRecord] = []
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

            if master_qty == 0:
                continue  # handled by the actual-side loop above

            if master_stop is None:
                skips.append(SkipRecord(key.symbol, SkipReason.NO_STOP))
                continue

            follower_pos = actual.positions.get(key)
            leverage = min(master_pos.leverage, policy.max_leverage)

            if follower_pos is None or follower_pos.qty == 0:
                # a-precondition: never open against a stale view of the master -
                # the position may already be closed on the exchange while the WS
                # event is still in flight (observed live on testnet)
                if not self._state_fresh(master):
                    skips.append(SkipRecord(key.symbol, SkipReason.STALE_MASTER))
                    continue
                # a-precondition: no re-entry while the same master episode ended
                # by THIS follower's own stop fill
                if await self._closed_by_own_stop(account_id, key, master):
                    skips.append(SkipRecord(key.symbol, SkipReason.NO_STOP))
                    continue
                if not policy.allow_new_entries:
                    skips.append(SkipRecord(key.symbol, SkipReason.PAUSED))
                    continue
                decision = entry_quantity(
                    policy,
                    actual.available_usdt,
                    master_pos.entry_price,
                    master_stop,
                    _filters_for(self._filters_provider(), key.symbol),
                    self._fee_buffer,
                )
                if decision.reason is not None:
                    skips.append(SkipRecord(key.symbol, decision.reason))
                    continue
                qty = decision.qty if master_qty > 0 else -decision.qty
                targets.append(
                    TargetPosition(
                        key=key, qty=qty, stop_price=master_stop, tp_price=None, leverage=leverage
                    )
                )
                continue

            current = follower_pos.qty
            current_abs = abs(current)
            cap = post_fill_cap(
                policy,
                actual.available_usdt,
                follower_pos.entry_price,
                master_stop,
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

            master_abs = abs(master_qty)
            prev_abs = self._master_abs.get((account_id, f"{key.symbol}:{key.pos_side}"))
            if prev_abs is not None and master_abs > prev_abs:
                # branch b: master increased -> state-based risk cap; growth is
                # forbidden against a stale view for the same reason as branch a
                target_abs = cap if self._state_fresh(master) else current_abs
            elif master_stop != (
                follower_pos.stop.stop_price if follower_pos.stop is not None else None
            ):
                # branch d/e: master moved the stop; recompute the risk cap from the
                # new distance. Farther stop -> smaller cap -> reduce (d).
                # Closer stop -> larger cap, but auto-increase is forbidden (e),
                # so clamp to current.
                cap_moved = post_fill_cap(
                    policy,
                    actual.available_usdt,
                    follower_pos.entry_price,
                    master_stop,
                    _filters_for(self._filters_provider(), key.symbol),
                    self._fee_buffer,
                )
                target_abs = min(current_abs, max(cap_moved, Decimal(0)))
            else:
                # branch c: master scaled down proportionally
                reference = prev_abs if prev_abs else master_abs
                target_abs = scaled_reduce(
                    current_abs,
                    reference,
                    master_abs,
                    _filters_for(self._filters_provider(), key.symbol),
                )
            if not policy.allow_new_entries and target_abs > current_abs:
                target_abs = current_abs  # pause: increases blocked
            qty = target_abs if master_qty > 0 else -target_abs
            targets.append(
                TargetPosition(
                    key=key, qty=qty, stop_price=master_stop, tp_price=None, leverage=leverage
                )
            )
        return targets, skips

    async def _closed_by_own_stop(
        self, account_id: str, key: PositionKey, master: MasterState
    ) -> bool:
        """True when the follower's protective stop of the ACTIVE master episode
        already FILLED (re-entry into this episode is forbidden)."""
        trade = await self._trades.active_trade(key.symbol, key.pos_side.value)
        if trade is None:
            return False
        rows = await self._links.by_trade(int(trade["trade_id"]))
        stop_links = [
            r for r in rows if r["account_id"] == account_id and r["kind"] == "PLACE_STOP"
        ]
        if not stop_links:
            return False
        client = self._client_for(account_id)
        if client is None:
            return False
        for row in stop_links:
            coid = str(row["follower_client_order_id"])
            try:
                order = await client.query_order(account_id, key.symbol, orig_client_order_id=coid)
            except Exception as exc:  # noqa: BLE001 - defensive
                log.warning("self-stop query failed for %s: %s", coid, exc)
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

    async def _stamp_trade_id(self, account_id: str, targets: list[TargetPosition], plan):
        """Associate the plan with the active master episode (order_links FK)."""
        from dataclasses import replace as dc_replace

        trade_id = None
        for tp in targets:
            row = await self._trades.active_trade(tp.key.symbol, tp.key.pos_side.value)
            if row is not None:
                trade_id = int(row["trade_id"])
                break
        return dc_replace(plan, trade_id=trade_id)

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


@dataclass(slots=True)
class SkipRecord:
    symbol: str
    reason: SkipReason


def _filters_for(filters_by_symbol: dict[str, SymbolFilters], symbol: str) -> SymbolFilters:
    filters = filters_by_symbol.get(symbol)
    if filters is None:
        raise KeyError(f"no filters for symbol {symbol}")
    return filters


def _pick(orders: list, order_type: str):
    candidates = [o for o in orders if o.type == order_type]
    if not candidates:
        return None
    return max(candidates, key=lambda o: o.update_time)
