"""Transport-neutral emergency control workflow.

The controller deliberately knows nothing about Telegram, SQLite, or an
``App`` composition root.  A caller supplies an account lifecycle factory, the
executor seam, and durable state callbacks.  This keeps the destructive
workflow identical for CLI and the future application service.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from copytradeaster.domain.models import (
    AccountState,
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PosSide,
    TargetPosition,
)


class EmergencyScope(StrEnum):
    ACCOUNT = "account"
    GLOBAL = "global"


class EmergencyFailure(StrEnum):
    NO_ACCOUNTS = "no_accounts"
    LIFECYCLE_FAILED = "lifecycle_failed"
    SNAPSHOT_FAILED = "snapshot_failed"
    COMMAND_FAILED = "command_failed"
    NOT_FLAT = "not_flat"


@dataclass(frozen=True, slots=True)
class EmergencyAccountReport:
    account_id: str
    success: bool
    passes: int
    flat_proofs: int
    commands_confirmed: int = 0
    failure: EmergencyFailure | None = None


@dataclass(frozen=True, slots=True)
class EmergencyReport:
    operation_id: str
    scope: EmergencyScope
    account_ids: tuple[str, ...]
    success: bool
    passes: int
    flat_proofs: int
    entries_paused: bool
    emergency_active: bool
    accounts: tuple[EmergencyAccountReport, ...]
    failure: EmergencyFailure | None = None


class EmergencyRuntime(Protocol):
    account_id: str
    client: object


class EmergencyLifecycle(Protocol):
    runtime: EmergencyRuntime

    async def snapshot(self) -> AccountState: ...


class EmergencyExecutor(Protocol):
    async def submit_under_mutation_lock(
        self, plan: ExecutionPlan, *, client: object
    ) -> list[object]: ...


class EmergencyState(Protocol):
    """Durable state adapter; implementations must never re-enable entries."""

    async def pause_account(self, account_id: str) -> None: ...

    async def begin_global(self, operation_id: str) -> None: ...

    async def record_result(self, report: EmergencyReport) -> None: ...


LifecycleFactory = Callable[[str], AbstractAsyncContextManager[EmergencyLifecycle]]
Sleep = Callable[[float], Awaitable[None]]


def build_close_plan(
    account_id: str,
    actual: AccountState,
    targets: Iterable[TargetPosition] | None = None,
    *,
    operation_id: str | None = None,
    revision: int = 0,
) -> ExecutionPlan:
    """Build the existing cancel-protections then reduce-only/hedge-close plan."""
    actions: list[ExecutionAction] = []
    sequence = 0
    if targets is None:
        targets = [
            TargetPosition(
                key=key,
                qty=0,
                stop_price=None,
                tp_price=None,
                leverage=position.leverage,
            )
            for key, position in actual.positions.items()
        ]
    for target in sorted(targets, key=lambda item: (item.key.symbol, item.key.pos_side.value)):
        key = target.key
        position = actual.positions.get(key)
        if position is None:
            continue
        for protection in (
            position.stop,
            position.take_profit,
            *position.extra_stops,
            *position.extra_tps,
        ):
            if protection is not None:
                actions.append(
                    ExecutionAction(
                        kind=ActionKind.CANCEL_ORDER,
                        symbol=key.symbol,
                        payload={
                            "symbol": key.symbol,
                            "origClientOrderId": protection.client_order_id,
                        },
                        client_order_id=None,
                        replaces_client_order_id=protection.client_order_id,
                        priority=1,
                        revision=revision,
                        position_side=key.pos_side,
                    )
                )
        if position.qty == 0:
            continue
        hedge = key.pos_side != PosSide.BOTH
        quantity = abs(position.qty)
        side = "SELL" if position.qty > 0 else "BUY"
        if hedge:
            side = "SELL" if key.pos_side == PosSide.LONG else "BUY"
        payload = {
            "symbol": key.symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "newOrderRespType": "RESULT",
        }
        if hedge:
            payload["positionSide"] = key.pos_side.value
        else:
            payload["reduceOnly"] = "true"
        sequence += 1
        actions.append(
            ExecutionAction(
                kind=ActionKind.MARKET_CLOSE,
                symbol=key.symbol,
                payload=payload,
                client_order_id=f"ct-emergency-{key.symbol}-{sequence}",
                replaces_client_order_id=None,
                priority=2,
                revision=revision,
                position_side=key.pos_side,
            )
        )
    actions.sort(key=lambda action: action.priority)
    return ExecutionPlan(
        account_id=account_id,
        trade_id=None,
        actions=actions,
        operation_id=operation_id,
    )


def is_flat_without_orders(actual: AccountState) -> bool:
    """Require an authoritative flat position and no exchange open orders."""
    if actual.open_orders:
        return False
    for position in actual.positions.values():
        if position.qty != 0:
            return False
        if any(
            protection is not None
            for protection in (
                position.stop,
                position.take_profit,
                *position.extra_stops,
                *position.extra_tps,
            )
        ):
            return False
    return True


class EmergencyController:
    """Run per-account or global emergency flattening with safe reports."""

    def __init__(
        self,
        *,
        lifecycle_factory: LifecycleFactory,
        executor: EmergencyExecutor,
        state: EmergencyState,
        max_passes: int = 5,
        required_flat_proofs: int = 2,
        pass_delay_s: float = 0.25,
        sleep: Sleep = asyncio.sleep,
        operation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_passes <= 0:
            raise ValueError("max_passes must be positive")
        if required_flat_proofs <= 0:
            raise ValueError("required_flat_proofs must be positive")
        if pass_delay_s < 0:
            raise ValueError("pass_delay_s must not be negative")
        self._lifecycle_factory = lifecycle_factory
        self._executor = executor
        self._state = state
        self._max_passes = max_passes
        self._required_flat_proofs = required_flat_proofs
        self._pass_delay_s = pass_delay_s
        self._sleep = sleep
        self._operation_id_factory = operation_id_factory or (lambda: uuid.uuid4().hex)

    async def emergency_account(
        self, account_id: str, *, operation_id: str | None = None
    ) -> EmergencyReport:
        return await self._run((account_id,), EmergencyScope.ACCOUNT, operation_id=operation_id)

    async def emergency_global(
        self,
        account_ids: Iterable[str],
        *,
        operation_id: str | None = None,
    ) -> EmergencyReport:
        ids = tuple(dict.fromkeys(str(account_id) for account_id in account_ids))
        return await self._run(ids, EmergencyScope.GLOBAL, operation_id=operation_id)

    async def _run(
        self,
        account_ids: tuple[str, ...],
        scope: EmergencyScope,
        *,
        operation_id: str | None,
    ) -> EmergencyReport:
        operation_id = operation_id or self._operation_id_factory()
        if scope == EmergencyScope.GLOBAL:
            await self._state.begin_global(operation_id)
        else:
            for account_id in account_ids:
                await self._state.pause_account(account_id)
        if not account_ids:
            report = EmergencyReport(
                operation_id=operation_id,
                scope=scope,
                account_ids=(),
                success=False,
                passes=0,
                flat_proofs=0,
                entries_paused=scope == EmergencyScope.GLOBAL,
                emergency_active=scope == EmergencyScope.GLOBAL,
                accounts=(),
                failure=EmergencyFailure.NO_ACCOUNTS,
            )
            await self._state.record_result(report)
            return report

        results = await asyncio.gather(
            *(self._run_account(account_id, operation_id) for account_id in account_ids)
        )
        reports = tuple(results)
        report = EmergencyReport(
            operation_id=operation_id,
            scope=scope,
            account_ids=account_ids,
            success=all(item.success for item in reports),
            passes=max((item.passes for item in reports), default=0),
            flat_proofs=min((item.flat_proofs for item in reports), default=0),
            entries_paused=True,
            emergency_active=scope == EmergencyScope.GLOBAL,
            accounts=reports,
        )
        await self._state.record_result(report)
        return report

    async def _run_account(self, account_id: str, operation_id: str) -> EmergencyAccountReport:
        passes = 0
        flat_proofs = 0
        commands_confirmed = 0
        failure: EmergencyFailure | None = None
        try:
            async with self._lifecycle_factory(account_id) as lifecycle:
                runtime = lifecycle.runtime
                for pass_index in range(1, self._max_passes + 1):
                    passes = pass_index
                    try:
                        actual = await lifecycle.snapshot()
                    except Exception:  # noqa: BLE001 - safe typed report only
                        failure = EmergencyFailure.SNAPSHOT_FAILED
                        flat_proofs = 0
                        await self._delay(pass_index)
                        continue

                    try:
                        plan = build_close_plan(
                            account_id,
                            actual,
                            operation_id=operation_id,
                            revision=pass_index,
                        )
                        if plan.actions:
                            results = await self._executor.submit_under_mutation_lock(
                                plan, client=runtime.client
                            )
                            commands_confirmed += sum(
                                1
                                for result in results
                                if getattr(result, "status", None) == "CONFIRMED"
                            )
                            if any(
                                getattr(result, "status", None) != "CONFIRMED" for result in results
                            ):
                                failure = EmergencyFailure.COMMAND_FAILED
                        verified = await lifecycle.snapshot()
                    except Exception:  # noqa: BLE001 - safe typed report only
                        failure = EmergencyFailure.COMMAND_FAILED
                        flat_proofs = 0
                        await self._delay(pass_index)
                        continue

                    if is_flat_without_orders(verified):
                        flat_proofs += 1
                        failure = None
                        if flat_proofs >= self._required_flat_proofs:
                            return EmergencyAccountReport(
                                account_id=account_id,
                                success=True,
                                passes=passes,
                                flat_proofs=flat_proofs,
                                commands_confirmed=commands_confirmed,
                            )
                    else:
                        flat_proofs = 0
                        failure = EmergencyFailure.NOT_FLAT
                    await self._delay(pass_index)
        except Exception:  # noqa: BLE001 - lifecycle failure is isolated
            failure = EmergencyFailure.LIFECYCLE_FAILED
        return EmergencyAccountReport(
            account_id=account_id,
            success=False,
            passes=passes,
            flat_proofs=flat_proofs,
            commands_confirmed=commands_confirmed,
            failure=failure or EmergencyFailure.LIFECYCLE_FAILED,
        )

    async def _delay(self, pass_index: int) -> None:
        if pass_index < self._max_passes and self._pass_delay_s:
            await self._sleep(self._pass_delay_s)


__all__ = [
    "EmergencyAccountReport",
    "EmergencyController",
    "EmergencyFailure",
    "EmergencyReport",
    "EmergencyScope",
    "build_close_plan",
    "is_flat_without_orders",
]
