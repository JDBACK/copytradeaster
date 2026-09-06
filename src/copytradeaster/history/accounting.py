"""Evidence-based episode cash accounting; no fee-cap estimates or FX guesses.

Income is the canonical signed cashflow source. Fill commission only checks
completeness; it is never added on top of COMMISSION income. Callers must prove
coverage/attribution before opting into an exact net result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from copytradeaster.aster.models import IncomeDTO, UserTradeDTO
from copytradeaster.history.models import Episode

CashKey = tuple[str, str]  # incomeType + tranId, within one authenticated account/environment.


@dataclass(frozen=True, slots=True)
class AccountingProof:
    income_complete: bool = False
    builder_policy: Literal["unknown", "included_in_commission", "separate"] = "unknown"
    builder_complete: bool = False


@dataclass(frozen=True, slots=True)
class EpisodeMoney:
    gross: Decimal
    commission: Decimal
    funding: Decimal
    builder: Decimal
    net: Decimal | None
    issues: tuple[str, ...]


def account_episode(
    episode: Episode,
    fills: Sequence[UserTradeDTO],
    cashflows: Sequence[IncomeDTO],
    *,
    proof: AccountingProof,
    attributed_flows: Mapping[CashKey, str] | None = None,
    builder_income_types: frozenset[str] = frozenset(),
) -> EpisodeMoney:
    """Inputs are already account/environment-owned. Attribution maps proven whole flows.

    A flip fill shared by episodes is not assigned a guessed exact commission:
    report the allocation issue until an explicit split-accounting policy exists.
    """
    links = attributed_flows or {}
    if builder_income_types & {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}:
        raise ValueError("builder flow types must be separate from standard cashflows")
    facts: dict[int, UserTradeDTO] = {}
    for fill in fills:
        if fill.symbol != episode.symbol:
            continue
        previous = facts.get(fill.trade_id)
        if previous is not None and previous != fill:
            raise ValueError("conflicting fill identity")
        facts[fill.trade_id] = fill
    flows: dict[CashKey, IncomeDTO] = {}
    for flow in cashflows:
        key = (flow.income_type, flow.tran_id)
        if key in flows and flows[key] != flow:
            raise ValueError("conflicting cashflow identity")
        flows[key] = flow
    commissions: dict[tuple[str, str], list[IncomeDTO]] = {}
    for flow in flows.values():
        if flow.income_type == "COMMISSION":
            commissions.setdefault((flow.symbol, flow.trade_id), []).append(flow)
    issues: set[str] = set()
    if episode.quality != "complete":
        issues.add("partial_history")
    if not proof.income_complete:
        issues.add("income_incomplete")
    if proof.builder_policy == "unknown" or (
        proof.builder_policy == "separate" and not proof.builder_complete
    ):
        issues.add("builder_unknown")
    totals = {"commission": Decimal(0), "funding": Decimal(0), "builder": Decimal(0)}
    allocated: dict[int, Decimal] = {}
    for allocation in episode.allocations:
        allocated[allocation.trade_id] = (
            allocated.get(allocation.trade_id, Decimal(0)) + allocation.quantity
        )
    with localcontext() as context:
        context.prec = 80
        for trade_id, quantity in allocated.items():
            fill = facts.get(trade_id)
            if fill is None:
                issues.add("fill_missing")
                continue
            if quantity != fill.qty:
                issues.add("shared_fill_commission")
                continue
            matched = commissions.get((episode.symbol, str(trade_id)), [])
            if not matched:
                if fill.commission != 0:
                    issues.add("commission_missing")
                continue
            if fill.commission_asset != "USDT" or any(flow.asset != "USDT" for flow in matched):
                issues.add("non_usdt_cost")
                continue
            signed = sum((flow.income for flow in matched), Decimal(0))
            if abs(signed) != abs(fill.commission):
                issues.add("commission_mismatch")
            totals["commission"] += signed
        for key, flow in flows.items():
            if flow.income_type not in {"FUNDING_FEE", *builder_income_types}:
                continue  # REALIZED_PNL is already counted from fills; transfers are not trade PnL.
            assigned = links.get(key)
            within_episode = (
                flow.symbol == episode.symbol
                and (episode.opened_at is None or flow.time >= episode.opened_at)
                and (episode.closed_at is None or flow.time <= episode.closed_at)
            )
            if assigned != episode.public_ref:
                if assigned is None and within_episode:
                    issues.add(
                        "funding_unattributed"
                        if flow.income_type == "FUNDING_FEE"
                        else "builder_unattributed"
                    )
                continue
            if not within_episode:
                raise ValueError("cashflow attribution outside episode")
            if flow.asset != "USDT":
                issues.add("non_usdt_cost")
                continue
            field = "funding" if flow.income_type == "FUNDING_FEE" else "builder"
            if field == "builder" and proof.builder_policy != "separate":
                issues.add("builder_policy_conflict")
                continue
            totals[field] += flow.income
        net = episode.realized_pnl_gross + sum(totals.values(), Decimal(0)) if not issues else None
    return EpisodeMoney(
        episode.realized_pnl_gross,
        totals["commission"],
        totals["funding"],
        totals["builder"],
        net,
        tuple(sorted(issues)),
    )
