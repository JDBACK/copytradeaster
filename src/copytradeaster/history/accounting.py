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


def account_episodes(
    episodes: Sequence[Episode],
    fills: Sequence[UserTradeDTO],
    cashflows: Sequence[IncomeDTO],
    *,
    proof: AccountingProof,
    attributed_flows: Mapping[CashKey, str] | None = None,
    builder_income_types: frozenset[str] = frozenset(),
) -> dict[str, EpisodeMoney]:
    """Batch commission policy with exact sum preservation.

    Inputs are already account/environment-owned. Attribution maps proven
    whole flows. One fill shared by several episodes splits its signed
    COMMISSION income pro-rata over the allocated quantities; the shares add
    up to the signed total exactly (the last share takes the remainder, so
    no rounding leaks value). A fill whose allocated total does not match
    the exchange quantity keeps the ``shared_fill_commission`` issue on the
    involved episodes instead of a guessed split.
    """
    links = attributed_flows or {}
    if builder_income_types & {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}:
        raise ValueError("builder flow types must be separate from standard cashflows")
    facts: dict[int, UserTradeDTO] = {}
    for fill in fills:
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
    issues: dict[str, set[str]] = {}
    totals: dict[str, dict[str, Decimal]] = {}
    for episode in episodes:
        own = set()
        if episode.quality != "complete":
            own.add("partial_history")
        if not proof.income_complete:
            own.add("income_incomplete")
        if proof.builder_policy == "unknown" or (
            proof.builder_policy == "separate" and not proof.builder_complete
        ):
            own.add("builder_unknown")
        issues[episode.public_ref] = own
        totals[episode.public_ref] = {
            "commission": Decimal(0),
            "funding": Decimal(0),
            "builder": Decimal(0),
        }
    shares: dict[int, list[tuple[str, Decimal]]] = {}
    symbols: dict[int, str] = {}
    for episode in episodes:
        for allocation in episode.allocations:
            shares.setdefault(allocation.trade_id, []).append(
                (episode.public_ref, allocation.quantity)
            )
            symbols.setdefault(allocation.trade_id, episode.symbol)
    with localcontext() as context:
        context.prec = 80
        for trade_id, claimants in shares.items():
            fill = facts.get(trade_id)
            if fill is None or fill.symbol != symbols[trade_id]:
                for ref, _ in claimants:
                    issues[ref].add("fill_missing")
                continue
            claimed = sum((qty for _, qty in claimants), Decimal(0))
            if claimed != fill.qty:
                for ref, _ in claimants:
                    issues[ref].add("shared_fill_commission")
                continue
            matched = commissions.get((fill.symbol, str(trade_id)), [])
            if not matched:
                if fill.commission != 0:
                    for ref, _ in claimants:
                        issues[ref].add("commission_missing")
                continue
            if fill.commission_asset != "USDT" or any(flow.asset != "USDT" for flow in matched):
                for ref, _ in claimants:
                    issues[ref].add("non_usdt_cost")
                continue
            signed = sum((flow.income for flow in matched), Decimal(0))
            if abs(signed) != abs(fill.commission):
                for ref, _ in claimants:
                    issues[ref].add("commission_mismatch")
            ordered = sorted(claimants)
            paid = Decimal(0)
            for ref, qty in ordered[:-1]:
                share = signed * qty / fill.qty if fill.qty != 0 else Decimal(0)
                totals[ref]["commission"] += share
                paid += share
            totals[ordered[-1][0]]["commission"] += signed - paid
        for episode in episodes:
            _attribute_flows(
                episode,
                flows,
                links,
                proof,
                builder_income_types,
                totals[episode.public_ref],
                issues[episode.public_ref],
            )
    return {
        episode.public_ref: EpisodeMoney(
            episode.realized_pnl_gross,
            totals[episode.public_ref]["commission"],
            totals[episode.public_ref]["funding"],
            totals[episode.public_ref]["builder"],
            episode.realized_pnl_gross + sum(totals[episode.public_ref].values(), Decimal(0))
            if not issues[episode.public_ref]
            else None,
            tuple(sorted(issues[episode.public_ref])),
        )
        for episode in episodes
    }


def account_episode(
    episode: Episode,
    fills: Sequence[UserTradeDTO],
    cashflows: Sequence[IncomeDTO],
    *,
    proof: AccountingProof,
    attributed_flows: Mapping[CashKey, str] | None = None,
    builder_income_types: frozenset[str] = frozenset(),
) -> EpisodeMoney:
    """Single-episode view of the batch policy.

    Without sibling episodes a partially allocated fill cannot be split, so
    it keeps the ``shared_fill_commission`` issue; pass the whole batch to
    :func:`account_episodes` for the exact shared split.
    """
    return account_episodes(
        [episode],
        fills,
        cashflows,
        proof=proof,
        attributed_flows=attributed_flows,
        builder_income_types=builder_income_types,
    )[episode.public_ref]


def _attribute_flows(
    episode: Episode,
    flows: Mapping[CashKey, IncomeDTO],
    links: Mapping[CashKey, str],
    proof: AccountingProof,
    builder_income_types: frozenset[str],
    totals: dict[str, Decimal],
    issues: set[str],
) -> None:
    """Attribute funding/builder flows to one episode; transfers stay out."""
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
