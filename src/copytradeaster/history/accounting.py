"""Evidence-based episode cash accounting; no fee-cap estimates or FX guesses.

Income is the canonical signed cashflow source. Fill commission only checks
completeness; it is never added on top of COMMISSION income. Callers must prove
coverage/attribution before opting into an exact net result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any, Literal

from copytradeaster.aster.models import IncomeDTO, UserTradeDTO
from copytradeaster.history.models import Episode

if TYPE_CHECKING:
    from copytradeaster.storage.episodes import StoredEpisode
    from copytradeaster.storage.history import HistoryScope

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
    facts: dict[tuple[str, str], UserTradeDTO] = {}
    for fill in fills:
        key = (fill.symbol, str(fill.trade_id))
        previous = facts.get(key)
        if previous is not None and previous != fill:
            raise ValueError("conflicting fill identity")
        facts[key] = fill
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
    shares: dict[tuple[str, str], list[tuple[str, Decimal]]] = {}
    for episode in episodes:
        for allocation in episode.allocations:
            shares.setdefault((episode.symbol, str(allocation.trade_id)), []).append(
                (episode.public_ref, allocation.quantity)
            )
    with localcontext() as context:
        context.prec = 80
        for key, claimants in shares.items():
            fill = facts.get(key)
            if fill is None:
                for ref, _ in claimants:
                    issues[ref].add("fill_missing")
                continue
            claimed = sum((qty for _, qty in claimants), Decimal(0))
            if claimed != fill.qty:
                for ref, _ in claimants:
                    issues[ref].add("shared_fill_commission")
                continue
            matched = commissions.get(key, [])
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


@dataclass(frozen=True, slots=True)
class EpisodeMoneyBatch:
    """One consistent money read for the requested episode refs.

    ``money`` holds the batch accountant output for the requested canonical
    refs only (siblings are used for the exact shared-fill split but are not
    returned). ``proof`` is the durable proof behind the batch, ``revisions``
    the per-ref episode revisions read, ``facts_revision`` the maximum durable
    import revision over the involved symbols.
    """

    money: dict[str, EpisodeMoney]
    proof: AccountingProof
    facts_revision: int
    revisions: dict[str, int]


class EpisodeMoneyStale(ValueError):
    """Durable facts moved under a money read; the caller must retry."""


async def load_episode_money(
    scope: HistoryScope,
    refs: Sequence[str],
    *,
    history_repo: Any,
    episodes_repo: Any,
    builder_income_types: frozenset[str] = frozenset(),
) -> EpisodeMoneyBatch:
    """Read one consistent money slice and run the batch accountant.

    The read covers episodes, sibling allocations, fills, cashflows and
    coverage with no network inside; consistency is fixed by a revision check
    before/after (durable import revisions plus per-episode revisions). A
    concurrent publication raises :class:`EpisodeMoneyStale` instead of
    returning mixed-revision money.

    Shared fills expand to ALL sibling allocations even when the sibling
    episode is outside the requested page, so the signed COMMISSION total is
    counted exactly once with a deterministic pro-rata remainder. Funding
    without a unique episode stays unattributed; the builder cap never
    becomes an expense (only proven USDT flows count).
    """
    requested = tuple(refs)
    if (
        not requested
        or any(not isinstance(ref, str) or not ref for ref in requested)
        or len(set(requested)) != len(requested)
    ):
        raise ValueError("episode refs must be unique nonempty strings")
    if builder_income_types & {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}:
        raise ValueError("builder flow types must be separate from standard cashflows")
    stored_by_canonical: dict[str, StoredEpisode] = {}
    requested_canonical: dict[str, str] = {}
    for ref in requested:
        stored = await episodes_repo.get(scope, ref)
        if stored is None:
            raise ValueError("trade is unavailable")
        canonical = stored.episode.public_ref
        stored_by_canonical[canonical] = stored
        requested_canonical[ref] = canonical
    symbols = sorted({stored.episode.symbol for stored in stored_by_canonical.values()})
    rev_before: dict[str, int] = {}
    for symbol in symbols:
        rev_before[symbol] = int(await history_repo.facts_revision(scope, symbol))
    wanted_keys = sorted(
        {
            (episode.symbol, str(allocation.trade_id))
            for stored in stored_by_canonical.values()
            for episode in (stored.episode,)
            for allocation in episode.allocations
        }
    )
    if wanted_keys:
        siblings = await history_repo.sibling_refs(scope, wanted_keys)
        for sibling in siblings:
            if sibling not in stored_by_canonical:
                extra = await episodes_repo.get(scope, sibling)
                if extra is not None:
                    stored_by_canonical[extra.episode.public_ref] = extra
    all_episodes = [stored.episode for _, stored in sorted(stored_by_canonical.items())]
    all_symbols = sorted({episode.symbol for episode in all_episodes})
    for symbol in all_symbols:
        if symbol not in rev_before:
            rev_before[symbol] = int(await history_repo.facts_revision(scope, symbol))
    all_keys = sorted(
        {
            (episode.symbol, str(allocation.trade_id))
            for episode in all_episodes
            for allocation in episode.allocations
        }
    )
    fills = await history_repo.fills_by_trade_ids(scope, all_keys) if all_keys else []
    cashflows = await history_repo.cashflows_for_symbols(scope, all_symbols) if all_symbols else []
    income_rows = await history_repo.windows(scope, "income", "")
    policy_raw = await history_repo.builder_policy(scope)
    policy: Literal["unknown", "included_in_commission", "separate"] = (
        policy_raw if policy_raw in ("unknown", "included_in_commission", "separate") else "unknown"
    )
    proof = AccountingProof(
        income_complete=_income_covers_all(income_rows, all_episodes),
        builder_policy=policy,
        builder_complete=False,
    )
    universe: dict[str, list[Episode]] = {}
    for symbol in all_symbols:
        listed = await episodes_repo.list_symbol(scope, symbol)
        universe[symbol] = [item.episode for item in listed]
    attributed: dict[CashKey, str] = {}
    for flow in cashflows:
        if flow.income_type not in {"FUNDING_FEE", *builder_income_types}:
            continue
        if flow.asset != "USDT":
            continue
        candidates = [
            episode for episode in universe.get(flow.symbol, ()) if _contains(episode, flow.time)
        ]
        if len(candidates) == 1 and candidates[0].public_ref in stored_by_canonical:
            attributed[(flow.income_type, flow.tran_id)] = candidates[0].public_ref
    money_all = account_episodes(
        all_episodes,
        fills,
        cashflows,
        proof=proof,
        attributed_flows=attributed,
        builder_income_types=builder_income_types,
    )
    rev_after: dict[str, int] = {}
    for symbol in all_symbols:
        rev_after[symbol] = int(await history_repo.facts_revision(scope, symbol))
    if rev_after != rev_before:
        raise EpisodeMoneyStale("facts moved under money read")
    for canonical, stored in stored_by_canonical.items():
        if canonical in {requested_canonical[ref] for ref in requested}:
            fresh = await episodes_repo.get(scope, canonical)
            if fresh is None or fresh.revision != stored.revision:
                raise EpisodeMoneyStale("episode moved under money read")
    money = {
        ref: money_all[requested_canonical[ref]]
        for ref in requested
        if requested_canonical[ref] in money_all
    }
    revisions = {
        requested_canonical[ref]: stored_by_canonical[requested_canonical[ref]].revision
        for ref in requested
        if requested_canonical[ref] in stored_by_canonical
    }
    return EpisodeMoneyBatch(
        money=money,
        proof=proof,
        facts_revision=max(rev_after.values(), default=0),
        revisions=revisions,
    )


def _contains(episode: Episode, moment_ms: int) -> bool:
    return (episode.opened_at is None or moment_ms >= episode.opened_at) and (
        episode.closed_at is None or moment_ms <= episode.closed_at
    )


def _income_covers_all(rows: Sequence[Any], episodes: Sequence[Episode]) -> bool:
    """Durable income completeness over every episode interval.

    A non-empty income response never means complete: only ``complete``
    windows count, and every episode must be closed with both bounds inside
    the proven union. Open or partial episodes keep ``income_complete``
    False so their net stays null with a concrete gap.
    """
    complete: list[tuple[int, int]] = []
    for row in rows:
        try:
            state = row["state"]
            start = int(row["start_ms"])
            end = int(row["end_ms"])
        except (TypeError, ValueError, KeyError):
            continue
        if state != "complete" or end < start:
            continue
        complete.append((start, end))
    if not complete:
        return False
    complete.sort()
    merged: list[list[int]] = []
    for start, end in complete:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for episode in episodes:
        if episode.opened_at is None or episode.closed_at is None:
            return False
        start = int(episode.opened_at)
        end = int(episode.closed_at)
        reach: int | None = None
        for cover_start, cover_end in merged:
            if cover_start > end:
                break
            if cover_end < start:
                continue
            if reach is None:
                if cover_start > start:
                    return False
                reach = cover_end
            elif cover_start > reach + 1:
                return False
            else:
                reach = max(reach, cover_end)
            if reach >= end:
                break
        if reach is None or reach < end:
            return False
    return True
