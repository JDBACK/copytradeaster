"""Leverage control-plane: pre-provisioned follower leverage, zero hot-path I/O.

Design (docs/ARCHITECTURE.md §4, `risk/` + `trading/`):

* **Exchange capability** and **account configuration** are distinct state.
  The global RAM catalog keeps only a *candidate* maximum per symbol
  (``POST``-validated optimistic target).  The only truth a trade may size
  against is the *account-confirmed* value for ``(account_id, symbol)``.
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


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, AsterError):
        return f"aster:{exc.code}" if exc.code else "aster"
    return type(exc).__name__


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
    """Exchange-advertised maximum for one symbol (optimistic target)."""

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
    ) -> None:
        self._client_for = client_for
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

        The exchange read wins over any cached guess for the VALUE. The
        generation stamp, however, follows agreement: an observed value equal
        to the current candidate proves the account sits exactly where this
        catalog generation wants it (this is how a restart reconstructs
        readiness without network writes). A DIFFERENT value keeps the pair
        unready for NEW exposure until the worker re-provisions it - otherwise
        any pre-downshift leverage found on the exchange would silently count
        as prepared.
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
            agrees_with_candidate = candidate is not None and candidate.leverage == leverage
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
                log.warning(
                    "leverage catalog refresh failed for %s: %s",
                    symbol,
                    _error_category(exc),
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
            return await self._prepare_locked(account_id, symbol)

    async def _prepare_locked(self, account_id: str, symbol: str) -> int:
        candidate = self._candidate.get(symbol)
        if candidate is None:
            await self.refresh_catalog([symbol])
            candidate = self._candidate.get(symbol)
        if candidate is None:
            raise LeverageError(f"{symbol}: candidate unavailable")
        client = self._client_for(account_id)
        try:
            await self._paced_set_leverage(client, account_id, symbol, candidate.leverage)
        except Exception as exc:  # noqa: BLE001 - classified below
            first_category = _error_category(exc)
            self.invalidate(account_id, symbol)
            try:
                response = await self._paced_bracket(client, account_id, symbol)
                fallback = _max_initial_leverage(response)
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
                catalog_revision=candidate.revision,
                sequence=self._next_seq(),
            )
            log.info(
                "leverage prepared at account maximum %s/%s=%s (candidate %s)",
                account_id,
                symbol,
                fallback,
                candidate.leverage,
            )
            return fallback
        self._confirmed[(account_id, symbol)] = ConfirmedLeverage(
            leverage=candidate.leverage,
            confirmed_at=time.monotonic(),
            catalog_revision=candidate.revision,
            sequence=self._next_seq(),
        )
        return candidate.leverage

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
