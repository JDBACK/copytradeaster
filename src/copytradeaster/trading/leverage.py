"""Leverage control-plane: pre-provisioned follower leverage, zero hot-path I/O.

Design (docs/ARCHITECTURE.md §4, `risk/` + `trading/`):

* **Exchange capability** and **account configuration** are distinct state.
  The global RAM catalog normally keeps the advertised maximum per symbol.
  If that metadata endpoint is unavailable, a unanimous set of fresh
  follower ``positionRisk`` observations may conservatively seed the already
  configured value instead.  The only truth a trade may size against remains
  the *account-confirmed* value for ``(account_id, symbol)``.
* The exchange itself persists the configured leverage; this module is a RAM
  model of that state, never a source of truth.  SQLite deliberately stays out
  (not business state, stale after downtime, re-verified from the exchange at
  boot through ``positionRisk`` snapshots).
* Data plane: ``confirmed()`` / ``is_ready()`` are pure RAM reads used by the
  coordinator while planning.  No ``leverageBracket`` GET and no ``POST``
  leverage ever runs on the entry hot path; preparation happens at
  onboarding/resume (explicit) or in the background maintenance loop
  (:meth:`LeverageManager.run`), which refreshes the candidate catalog
  stale-while-revalidate with bounded concurrency and pacing so it can never
  approach the shared request budget of trading traffic.
* Fail-closed: an account/symbol that is not ready for the current catalog
  generation blocks only NEW exposure and increases.  Closes, reductions,
  protection convergence and emergency flows never consult this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from copytradeaster.aster.trading_api import AsterError
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)

CATALOG_REFRESH_S = 300.0
MAINTENANCE_INTERVAL_S = 10.0
PREPARE_PACING_S = 0.25
DEFAULT_CONCURRENCY = 2


class LeverageError(RuntimeError):
    """Safe control-plane failure; details stay in local logs."""


UNKNOWN_EXCHANGE_CODES = {-1000, -1006, -1007}
UNKNOWN_HTTP_STATUSES = {503, 504}


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, AsterError):
        return f"aster:{exc.code}" if exc.code else "aster"
    return type(exc).__name__


def _is_unknown_outcome(exc: BaseException) -> bool:
    """True when a leverage write may or may not have reached the exchange.

    AsterClient maps transport failures to -1006/-1007-style errors and
    gateways answer -1000 with 503/504; only a deterministic exchange
    rejection (any other code) classifies the outcome from the error alone.
    Any non-exchange failure leaves the outcome unknown by definition.
    """
    if isinstance(exc, AsterError):
        return exc.code in UNKNOWN_EXCHANGE_CODES or exc.http_status in UNKNOWN_HTTP_STATUSES
    return True


def _max_initial_leverage(response: Any) -> int:
    """Extract the maximum positive initialLeverage from a bracket response."""
    values = [
        bracket.initial_leverage
        for bracket in response.brackets
        if isinstance(bracket.initial_leverage, int)
        and not isinstance(bracket.initial_leverage, bool)
        and bracket.initial_leverage > 0
    ]
    if not values:
        raise LeverageError("empty leverage bracket")
    return max(values)


@dataclass(frozen=True, slots=True)
class CandidateLeverage:
    """Desired provisioned leverage for one symbol in the current generation."""

    leverage: int
    observed_at: float
    revision: int


@dataclass(frozen=True, slots=True)
class ConfirmedLeverage:
    """Account-specific configured leverage validated against one generation."""

    leverage: int
    confirmed_at: float
    catalog_revision: int
    # Monotonic state-version stamp. Callers capture a ticket before waiting
    # on the singleflight lock; a confirmation whose stamp covers the ticket
    # means another coroutine already performed this network work.
    sequence: int = -1


@dataclass(frozen=True, slots=True)
class PrepareReport:
    """Outcome of a bulk :meth:`LeverageManager.prepare_account` call."""

    confirmed: dict[str, int]
    failed: dict[str, str]  # symbol -> safe error category


class LeverageManager:
    """RAM control-plane for candidate and confirmed follower leverage."""

    def __init__(
        self,
        *,
        client_for: Callable[[str], Any],
        symbols: Collection[str],
        accounts: Callable[[], Collection[str]],
        catalog_account: str = "main",
        concurrency: int = DEFAULT_CONCURRENCY,
        refresh_after_s: float = CATALOG_REFRESH_S,
        maintenance_interval_s: float = MAINTENANCE_INTERVAL_S,
        pacing_s: float = PREPARE_PACING_S,
        mutation_context: Callable | None = None,
        operator_max_leverage: int | None = None,
    ) -> None:
        self._client_for = client_for
        self._mutation_context = mutation_context
        self._operator_max_leverage = operator_max_leverage
        self._symbols = tuple(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        self._accounts = accounts
        self._catalog_account = catalog_account
        self._refresh_after_s = refresh_after_s
        self._maintenance_interval_s = maintenance_interval_s
        self._pacing_s = pacing_s
        self._candidate: dict[str, CandidateLeverage] = {}
        self._confirmed: dict[tuple[str, str], ConfirmedLeverage] = {}
        self._revision = 0
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._pending: set[tuple[str, str]] = set()
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._catalog_attempted_at: float | None = None
        self._state_seq = 0

    def _next_seq(self) -> int:
        self._state_seq += 1
        return self._state_seq

    # --- pure reads (trading data plane; never perform I/O) -----------------------

    def confirmed(self, account_id: str, symbol: str) -> int | None:
        item = self._confirmed.get((account_id, symbol))
        return item.leverage if item is not None else None

    def is_ready(self, account_id: str, symbol: str) -> bool:
        """True when the confirmation belongs to the current catalog generation."""
        item = self._confirmed.get((account_id, symbol))
        return item is not None and item.catalog_revision == self._revision_of(symbol)

    def pending_count(self) -> int:
        return len(self._pending)

    def _revision_of(self, symbol: str) -> int | None:
        candidate = self._candidate.get(symbol)
        return candidate.revision if candidate is not None else None

    async def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
        return lock

    def invalidate(self, account_id: str, symbol: str) -> None:
        """Drop the confirmation; the pair stays not-ready until re-confirmed."""
        self._confirmed.pop((account_id, symbol), None)

    def observe(self, account_id: str, symbol_leverages: Mapping[str, int]) -> None:
        """Fold authoritative positionRisk leverages into the confirmed map.

        The candidate and operator setting are ceilings, not desired upshifts.
        Any observed value within ``[1, ceilings]`` validates this catalog
        generation. In particular, a stop-driven downshift survives maintenance
        and restart, and values above the ceiling remain unready for
        increases (preparation pulls them down to policy).

        Effective-is-truth (review 636dfff): the exchange-reported
        configuration is authoritative for what the account runs TODAY,
        including 1x. 1x is a legitimate effective state — a stop-driven
        downshift the execution DAG provisioned, an operator cap of 1, or a
        deliberate operator setting — not an unset marker. Entries size from
        the EFFECTIVE leverage, so opening at an observed 1x with 1x sizing
        is safe; and whenever the entry target differs from actual, the
        planner still emits SET_LEVERAGE, so no entry can silently run
        economics its sizing did not assume. The control plane therefore
        never upshifts an observed value toward the ceiling: that "optimize"
        re-provisioned 20x over a live stop-driven 1x every maintenance
        pass (SET-tennis plus a readiness flap blocking every other entry).
        """
        for symbol, raw in symbol_leverages.items():
            try:
                leverage = int(raw)
            except (TypeError, ValueError):
                continue
            if leverage <= 0:
                continue
            key = (account_id, symbol)
            current = self._confirmed.get(key)
            candidate = self._candidate.get(symbol)
            if (
                current is not None
                and current.leverage == leverage
                and current.catalog_revision == (candidate.revision if candidate else -1)
            ):
                continue
            ceiling = candidate.leverage if candidate is not None else None
            if ceiling is not None and self._operator_max_leverage:
                ceiling = min(ceiling, self._operator_max_leverage)
            agrees_with_candidate = (
                candidate is not None and ceiling is not None and leverage <= ceiling
            )
            self._confirmed[key] = ConfirmedLeverage(
                leverage=leverage,
                confirmed_at=time.monotonic(),
                catalog_revision=candidate.revision if agrees_with_candidate else -1,
                sequence=self._next_seq(),
            )

    def request_prepare(self, account_id: str, symbol: str) -> None:
        """Enqueue a background preparation; safe to call from the hot path."""
        self._pending.add((account_id, symbol))

    # --- control plane (background/explicit; never called while planning) ----------

    async def refresh_catalog(self, symbols: Iterable[str] | None = None) -> None:
        """Stale-while-revalidate refresh of the global candidate catalog.

        A failed probe keeps serving the last known candidates (RAM never
        expires); entries simply stay unready for generations they cannot be
        validated against.
        """
        wanted = list(symbols) if symbols is not None else list(self._symbols)
        try:
            client = self._client_for(self._catalog_account)
        except Exception as exc:  # noqa: BLE001 - catalog probe is best-effort
            log.warning("leverage catalog client unavailable: %s", _error_category(exc))
            self._catalog_attempted_at = time.monotonic()
            return
        for symbol in wanted:
            try:
                async with self._sem:
                    response = await client.leverage_bracket(self._catalog_account, symbol)
                maximum = _max_initial_leverage(response)
            except Exception as exc:  # noqa: BLE001 - keep previous candidates
                seeded = self._seed_candidate_from_observed_accounts(symbol)
                if seeded is None:
                    log.warning(
                        "leverage catalog refresh failed for %s: %s",
                        symbol,
                        _error_category(exc),
                    )
                else:
                    log.warning(
                        "leverage bracket unavailable for %s (%s); using unanimous "
                        "current follower configuration=%s",
                        symbol,
                        _error_category(exc),
                        seeded.leverage,
                    )
                await self._pace()
                continue
            current = self._candidate.get(symbol)
            revision = current.revision if current is not None else None
            if current is None or current.leverage != maximum:
                if current is not None:
                    log.info(
                        "leverage candidate revised for %s: %s -> %s",
                        symbol,
                        current.leverage,
                        maximum,
                    )
                revision = self._next_revision()
            self._candidate[symbol] = CandidateLeverage(
                leverage=maximum,
                observed_at=time.monotonic(),
                revision=revision if revision is not None else self._next_revision(),
            )
            await self._pace()
        self._catalog_attempted_at = time.monotonic()

    def _seed_candidate_from_observed_accounts(self, symbol: str) -> CandidateLeverage | None:
        """Use only a unanimous, current-boot follower configuration.

        ``_confirmed`` is RAM-only and is populated from authoritative
        positionRisk snapshots during this process boot.  Requiring every
        enabled REAL follower to have the same positive value prevents a
        partial snapshot or account-specific cap from becoming a global
        target.  A pre-existing catalog candidate always wins.
        """
        if symbol in self._candidate:
            return None
        account_ids = tuple(dict.fromkeys(self._accounts()))
        if not account_ids:
            return None
        observed: list[ConfirmedLeverage] = []
        for account_id in account_ids:
            item = self._confirmed.get((account_id, symbol))
            if item is None or item.leverage <= 0:
                return None
            observed.append(item)
        values = {item.leverage for item in observed}
        if len(values) != 1:
            return None
        leverage = values.pop()
        if leverage < 2 and self._operator_max_leverage != 1:
            # A unanimous 1x fleet is ambiguous: exchange unset default or a
            # deliberate fleet-wide setting. Refuse to bless it as the global
            # candidate — unless the operator policy explicitly wants 1x,
            # in which case there is no ambiguity to resolve.
            return None
        revision = self._next_revision()
        candidate = CandidateLeverage(
            leverage=leverage,
            observed_at=time.monotonic(),
            revision=revision,
        )
        self._candidate[symbol] = candidate
        for account_id, item in zip(account_ids, observed, strict=True):
            self._confirmed[(account_id, symbol)] = ConfirmedLeverage(
                leverage=item.leverage,
                confirmed_at=item.confirmed_at,
                catalog_revision=revision,
                sequence=self._next_seq(),
            )
        return candidate

    def _next_revision(self) -> int:
        self._revision += 1
        return self._revision

    async def prepare_symbol(self, account_id: str, symbol: str) -> int:
        """Provision and confirm one (account, symbol); returns the leverage.

        The POST doubles as validation: success proves the exchange accepted
        this level for THIS account, so no bracket GET is needed. Only a
        rejection falls back to the account-specific signed bracket read.
        """
        ticket = self._next_seq()
        async with await self._lock_for((account_id, symbol)):  # singleflight
            candidate = self._candidate.get(symbol)
            current = self._confirmed.get((account_id, symbol))
            if (
                candidate is not None
                and current is not None
                and current.catalog_revision == candidate.revision
                and current.sequence > ticket
            ):
                # A concurrent caller finished this exact preparation while we
                # waited on the lock: reuse it, send nothing.
                return current.leverage
            async with contextlib.AsyncExitStack() as stack:
                if self._mutation_context is not None:
                    await stack.enter_async_context(self._mutation_context(account_id))
                return await self._prepare_locked(account_id, symbol)

    async def _prepare_locked(self, account_id: str, symbol: str) -> int:
        candidate = self._candidate.get(symbol)
        if candidate is None:
            await self.refresh_catalog([symbol])
            candidate = self._candidate.get(symbol)
        if candidate is None:
            raise LeverageError(f"{symbol}: candidate unavailable")
        client = self._client_for(account_id)
        observed = await self._observed_leverage(client, account_id, symbol)
        if observed is None:
            self.invalidate(account_id, symbol)
            raise LeverageError(f"{symbol}: configuration unreadable")
        ceiling = min(candidate.leverage, self._operator_max_leverage or candidate.leverage)
        if observed <= ceiling:
            # Effective configuration already within policy: confirm it and
            # send nothing. In particular a legitimate 1x (stop-driven
            # downshift, operator cap 1, deliberate setting) is returned as
            # ready instead of being "optimized" back up to the ceiling.
            self.observe(account_id, {symbol: observed})
            return observed
        # Over policy: pull down to the ceiling. This is the ONLY provision
        # direction; upshifts never happen here (see observe()).
        candidate = CandidateLeverage(ceiling, candidate.observed_at, candidate.revision)
        try:
            await self._paced_set_leverage(client, account_id, symbol, candidate.leverage)
        except Exception as exc:  # noqa: BLE001 - classified below
            first_category = _error_category(exc)
            self.invalidate(account_id, symbol)
            if not _is_unknown_outcome(exc):
                # Deterministic rejection: the write provably did not apply,
                # so the account-specific bracket fallback is safe.
                return await self._prepare_at_account_maximum(
                    client, account_id, symbol, candidate.revision, first_category
                )
            # Outcome-unknown write: read authoritative state BEFORE any
            # further write. A blind second POST could push an account past
            # its bracket when the first POST actually landed.
            observed = await self._observed_leverage(client, account_id, symbol)
            if observed is None:
                log.warning(
                    "leverage outcome unknown and read failed for %s/%s (%s); failing closed",
                    account_id,
                    symbol,
                    first_category,
                )
                raise LeverageError(f"{symbol}: outcome-unknown read failed") from exc
            if observed == candidate.leverage:
                self._confirmed[(account_id, symbol)] = ConfirmedLeverage(
                    leverage=candidate.leverage,
                    confirmed_at=time.monotonic(),
                    catalog_revision=candidate.revision,
                    sequence=self._next_seq(),
                )
                log.info(
                    "leverage prepared (unknown outcome confirmed by read) %s/%s=%s",
                    account_id,
                    symbol,
                    candidate.leverage,
                )
                return candidate.leverage
            # Authoritatively not applied: proceed exactly like a rejection.
            log.info(
                "leverage outcome unknown but read shows %s (target %s) for %s/%s; "
                "treating as rejected",
                observed,
                candidate.leverage,
                account_id,
                symbol,
            )
            return await self._prepare_at_account_maximum(
                client, account_id, symbol, candidate.revision, first_category
            )
        self._confirmed[(account_id, symbol)] = ConfirmedLeverage(
            leverage=candidate.leverage,
            confirmed_at=time.monotonic(),
            catalog_revision=candidate.revision,
            sequence=self._next_seq(),
        )
        return candidate.leverage

    async def _prepare_at_account_maximum(
        self,
        client: Any,
        account_id: str,
        symbol: str,
        revision: int,
        first_category: str,
    ) -> int:
        try:
            response = await self._paced_bracket(client, account_id, symbol)
            fallback = min(
                _max_initial_leverage(response),
                self._candidate[symbol].leverage,
                self._operator_max_leverage or self._candidate[symbol].leverage,
            )
            await self._paced_set_leverage(client, account_id, symbol, fallback)
        except Exception as fallback_exc:  # noqa: BLE001 - fail closed
            category = _error_category(fallback_exc)
            log.warning(
                "leverage prepare failed for %s/%s (%s, fallback %s)",
                account_id,
                symbol,
                first_category,
                category,
            )
            raise LeverageError(f"{symbol}: {category}") from fallback_exc
        self._confirmed[(account_id, symbol)] = ConfirmedLeverage(
            leverage=fallback,
            confirmed_at=time.monotonic(),
            catalog_revision=revision,
            sequence=self._next_seq(),
        )
        log.info(
            "leverage prepared at account maximum %s/%s=%s",
            account_id,
            symbol,
            fallback,
        )
        return fallback

    async def _observed_leverage(self, client: Any, account_id: str, symbol: str) -> int | None:
        """Authoritative configured leverage from positionRisk; None if unreadable."""
        try:
            async with self._sem:
                rows = await client.position_risk(account_id, symbol)
        except Exception as exc:  # noqa: BLE001 - unreadable means fail closed
            log.warning(
                "positionRisk read failed during leverage recovery for %s/%s: %s",
                account_id,
                symbol,
                _error_category(exc),
            )
            return None
        values: set[int] = set()
        for row in rows:
            try:
                value = int(row.leverage)
            except (TypeError, ValueError, AttributeError):
                return None
            if value > 0:
                values.add(value)
        return values.pop() if len(values) == 1 else None

    async def prepare_account(self, account_id: str, symbols: Collection[str]) -> PrepareReport:
        """Prepare every requested symbol under the shared bounded semaphore."""
        confirmed: dict[str, int] = {}
        failed: dict[str, str] = {}
        for symbol in symbols:
            try:
                confirmed[symbol] = await self.prepare_symbol(account_id, symbol)
            except LeverageError as exc:
                failed[symbol] = str(exc).partition(": ")[2] or "unknown"
            await self._pace()
        return PrepareReport(confirmed=confirmed, failed=failed)

    async def _maintain_once(self) -> None:
        if self._catalog_due():
            await self.refresh_catalog()
        work: list[tuple[str, str]] = list(self._pending)
        self._pending.clear()
        for account_id in self._accounts():
            for symbol in self._symbols:
                if (account_id, symbol) in work:
                    continue
                if not self.is_ready(account_id, symbol):
                    work.append((account_id, symbol))
        if not work:
            return
        results = await asyncio.gather(
            *(self.prepare_symbol(account_id, symbol) for account_id, symbol in work),
            return_exceptions=True,
        )
        for (account_id, symbol), outcome in zip(work, results, strict=True):
            if isinstance(outcome, BaseException) and not isinstance(outcome, LeverageError):
                log.error(
                    "leverage maintenance crashed for %s/%s: %s",
                    account_id,
                    symbol,
                    _error_category(outcome),
                )

    def _catalog_due(self) -> bool:
        if not self._symbols or self._catalog_attempted_at is None:
            return True
        jittered = self._refresh_after_s * (0.8 + 0.4 * random.random())
        return time.monotonic() - self._catalog_attempted_at >= jittered

    async def run(self, stop_event: asyncio.Event) -> None:
        """Background maintenance loop; a managed task like sweep/heartbeat."""
        log.info("leverage maintenance started (%d symbols)", len(self._symbols))
        while not stop_event.is_set():
            try:
                await self._maintain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.error("leverage maintenance failed: %s", _error_category(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._maintenance_interval_s * (0.9 + 0.2 * random.random()),
                )
        log.info("leverage maintenance stopped")

    async def _pace(self) -> None:
        if self._pacing_s <= 0:
            return
        await asyncio.sleep(self._pacing_s)

    async def _paced_set_leverage(self, client, account_id: str, symbol: str, lev: int) -> None:
        async with self._sem:
            await client.set_leverage(account_id, symbol, lev)
        await self._pace()

    async def _paced_bracket(self, client, account_id: str, symbol: str):
        async with self._sem:
            return await client.leverage_bracket(account_id, symbol)


__all__ = [
    "CandidateLeverage",
    "ConfirmedLeverage",
    "LeverageError",
    "LeverageManager",
    "PrepareReport",
]
