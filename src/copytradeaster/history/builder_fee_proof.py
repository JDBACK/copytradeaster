"""Measure the real Builder fee policy on an exchange account (signed proof).

The money layer refuses to claim a net trade result for a Builder account
while the Builder fee's source is unproven (``storage/history.py:builder_policy``).
This module produces that proof from the exchange instead of from
configuration: it runs two identical round trips — one Builder-attributed with
``feeRate``, one plain — and compares the effective commission rate of each.

Proven outcome (REAL Aster Testnet, 2026-09-13, and re-checked by
``tests/testnet/test_builder_fee_accounting_live.py``):

* the attributed fills carry exactly ``feeRate`` more than the control fills;
* ``GET /fapi/v3/income`` reports the whole amount as one standard
  ``COMMISSION`` row per fill, with no separate Builder cashflow.

Only that measurement is allowed to write ``accounts.builder_fee_policy``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from copytradeaster.aster.trading_api import AsterClient, PlaceOrderRequest
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)

LEDGER_WAIT_S = 12.0
ROUND_TRIP_PAUSE_S = 4.0
Policy = Literal["included_in_commission", "separate", "unknown"]


@dataclass(frozen=True, slots=True)
class BuilderFeeProof:
    """One measured verdict: rates, the delta and the raw fill facts."""

    policy: Policy
    attributed_rate: Decimal
    control_rate: Decimal
    fee_rate: Decimal
    attributed_fills: int
    control_fills: int
    income_types: tuple[str, ...]
    detail: str

    @property
    def matches_fee_rate(self) -> bool:
        return self.attributed_rate == self.control_rate + self.fee_rate


class BuilderFeeProofError(RuntimeError):
    """The measurement could not be taken (flatness, approval, filters)."""


def _rate(commission: Decimal, notional: Decimal) -> Decimal:
    if notional == 0:
        raise BuilderFeeProofError("fill has zero notional")
    return (commission / notional).quantize(Decimal("0.00000001"))


async def _flat_without_orders(client: AsterClient, account_id: str, symbol: str) -> bool:
    positions = await client.position_risk(account_id, symbol)
    orders = await client.open_orders(account_id, symbol)
    return all(position.position_amt == 0 for position in positions) and not orders


async def _flatten(client: AsterClient, account_id: str, symbol: str) -> None:
    """Best-effort flat: cancel leftovers, then reduce any residual position."""
    for order in await client.open_orders(account_id, symbol):
        try:
            await client.cancel_order(
                account_id, symbol, orig_client_order_id=order.client_order_id
            )
        except Exception:
            log.warning("builder-fee proof cancel failed account_id=%s", account_id)
    for position in await client.position_risk(account_id, symbol):
        if position.position_amt == 0:
            continue
        params = dict(
            symbol=symbol,
            side=(Side.SELL if position.position_amt > 0 else Side.BUY),
            type="MARKET",
            quantity=abs(position.position_amt),
            new_order_resp_type="RESULT",
            new_client_order_id=f"feeproofflat-{int(time.time() * 1000)}",
        )
        if position.position_side is not PosSide.BOTH:
            params["position_side"] = position.position_side
        else:
            params["reduce_only"] = True
        try:
            await client.place_order(account_id, PlaceOrderRequest(**params))
        except Exception:
            log.warning("builder-fee proof flatten failed account_id=%s", account_id)


async def _round_trip(
    client: AsterClient,
    account_id: str,
    symbol: str,
    quantity: Decimal,
    side: PosSide,
    *,
    builder: str | None,
    fee_rate: Decimal | None,
    tag: str,
) -> list[str]:
    order_ids: list[str] = []
    for leg, order_side in (("open", Side.BUY), ("close", Side.SELL)):
        params: dict[str, Any] = dict(
            symbol=symbol,
            side=order_side,
            type="MARKET",
            quantity=quantity,
            new_order_resp_type="RESULT",
            new_client_order_id=f"feeproof-{tag}-{leg}-{int(time.time() * 1000)}",
        )
        if side is PosSide.BOTH and leg == "close":
            params["reduce_only"] = True
        else:
            params["position_side"] = side
        if builder is not None and fee_rate is not None:
            params["builder"] = builder
            params["fee_rate"] = fee_rate
        order = await client.place_order(account_id, PlaceOrderRequest(**params))
        order_ids.append(str(order.order_id))
        if leg == "open":
            for _ in range(20):
                positions = await client.position_risk(account_id, symbol)
                if any(position.position_amt != 0 for position in positions):
                    break
                await asyncio.sleep(0.5)
            else:
                raise BuilderFeeProofError("attributed open order produced no position")
    return order_ids


async def measure_builder_fee_policy(
    *,
    client: AsterClient,
    account_id: str,
    symbol: str,
    quantity: Decimal,
    builder_address: str,
    fee_rate: Decimal,
    hedge: bool,
) -> BuilderFeeProof:
    """Measure the policy with two round trips on a flat, approved account."""
    if not builder_address.startswith("0x"):
        raise BuilderFeeProofError("builder address is not configured")
    if not fee_rate.is_finite() or fee_rate <= 0:
        raise BuilderFeeProofError("builder fee rate must be a positive Decimal")
    if not await _flat_without_orders(client, account_id, symbol):
        raise BuilderFeeProofError(
            f"{symbol} must be flat with no open orders before the measurement"
        )

    info = await client.exchange_info()
    filters = info.tradable_filters().get(symbol)
    if filters is None or filters.step_size <= 0:
        raise BuilderFeeProofError(f"no tradable filters for {symbol}")
    sized = (quantity // filters.step_size) * filters.step_size
    if sized <= 0:
        raise BuilderFeeProofError(f"quantity is below the {symbol} step size")
    side = PosSide.LONG if hedge else PosSide.BOTH
    start_ms = int(time.time() * 1000) - 60_000

    attributed_ids: list[str] = []
    control_ids: list[str] = []
    try:
        attributed_ids = await _round_trip(
            client,
            account_id,
            symbol,
            sized,
            side,
            builder=builder_address,
            fee_rate=fee_rate,
            tag="b",
        )
        await asyncio.sleep(ROUND_TRIP_PAUSE_S)
        control_ids = await _round_trip(
            client, account_id, symbol, sized, side, builder=None, fee_rate=None, tag="c"
        )
    finally:
        if not await _flat_without_orders(client, account_id, symbol):
            await _flatten(client, account_id, symbol)

    await asyncio.sleep(LEDGER_WAIT_S)
    end_ms = int(time.time() * 1000) + 1_000
    fills = await client.user_trades(
        account_id, symbol, start_time=start_ms, end_time=end_ms, limit=1000
    )
    flows = await client.income_history(
        account_id, start_time=start_ms, end_time=end_ms, limit=1000
    )

    attributed_set = set(attributed_ids)
    control_set = set(control_ids)
    attributed = [fill for fill in fills if str(fill.order_id) in attributed_set]
    control = [fill for fill in fills if str(fill.order_id) in control_set]
    if not attributed or not control:
        raise BuilderFeeProofError("the attributed or the control fills were not reported back")

    attributed_rate = _rate(attributed[0].commission, attributed[0].quote_qty)
    control_rate = _rate(control[0].commission, control[0].quote_qty)
    for fill in attributed:
        if _rate(fill.commission, fill.quote_qty) != attributed_rate:
            raise BuilderFeeProofError("attributed fills disagree on the effective rate")
    for fill in control:
        if _rate(fill.commission, fill.quote_qty) != control_rate:
            raise BuilderFeeProofError("control fills disagree on the effective rate")

    income_by_trade: dict[str, Decimal] = {}
    for flow in flows:
        if flow.income_type == "COMMISSION" and flow.trade_id:
            income_by_trade[flow.trade_id] = (
                income_by_trade.get(flow.trade_id, Decimal(0)) + flow.income
            )
    for fill in attributed + control:
        signed = income_by_trade.get(str(fill.trade_id))
        if signed is None:
            raise BuilderFeeProofError(f"no COMMISSION income for trade {fill.trade_id}")
        if fill.commission != abs(signed):
            raise BuilderFeeProofError(
                f"fill commission {fill.commission} does not match COMMISSION income {signed}"
            )

    income_types = tuple(sorted({flow.income_type for flow in flows}))
    delta = attributed_rate - control_rate
    if delta == fee_rate:
        policy: Policy = "included_in_commission"
        detail = (
            f"attributed {attributed_rate} = control {control_rate} + feeRate {fee_rate}; "
            f"income types {', '.join(income_types)}"
        )
    elif delta == 0:
        policy = "unknown"
        detail = (
            f"attributed and control rates are identical ({control_rate}); the attributed "
            "feeRate was not charged, so no policy can be claimed"
        )
    else:
        policy = "separate"
        detail = (
            f"attributed {attributed_rate} differs from control {control_rate} by {delta}, "
            f"which is not feeRate {fee_rate}"
        )
    return BuilderFeeProof(
        policy=policy,
        attributed_rate=attributed_rate,
        control_rate=control_rate,
        fee_rate=fee_rate,
        attributed_fills=len(attributed),
        control_fills=len(control),
        income_types=income_types,
        detail=detail,
    )


def policy_stamp(proof: BuilderFeeProof, environment: str) -> str:
    """Durable stamp for ``accounts.builder_fee_policy``: policy, scope, epoch.

    Epoch seconds keep the stamp colon-free, so the ``policy:environment:measured_at``
    shape stays unambiguous for the reader that splits it on ``:``.
    """
    if proof.policy == "unknown":
        raise BuilderFeeProofError("an unknown policy must never be stamped")
    return f"{proof.policy}:{environment}:{int(time.time())}"


__all__ = [
    "BuilderFeeProof",
    "BuilderFeeProofError",
    "measure_builder_fee_policy",
    "policy_stamp",
]
