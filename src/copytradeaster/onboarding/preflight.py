"""Unified Mainnet preflight checklist (PM-B4 Package C).

Read-only by construction: every check uses signed GET endpoints
(``agent`` / ``builder`` / ``balance`` / ``positionRisk`` / ``openOrders`` /
``positionSide/dual`` / ``time`` / ``exchangeInfo`` / ``leverageBracket``) or a
local command-store read.  No order writes exist on this path — the reader
protocols expose no mutating method, so a preflight run cannot place, move, or
cancel an order on any network.

Fail-closed semantics: any unreadable or mismatched check blocks readiness,
with one deliberate exception carried over from ``App.start`` (app.py:440):
a position-mode metadata failure on TESTNET only warns, because a wrong-sided
order is still rejected by the exchange.  On MAINNET the same failure blocks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from eth_utils import is_address

from copytradeaster.aster.models import (
    AgentDTO,
    ExchangeInfo,
    LeverageBracketResponse,
)
from copytradeaster.aster.network_profile import AsterNetworkProfile
from copytradeaster.logging_utils import get_logger
from copytradeaster.onboarding.builder import validate_builder_record
from copytradeaster.onboarding.direct import validate_agent_permissions
from copytradeaster.onboarding.errors import OnboardingError
from copytradeaster.onboarding.models import DirectOnboardingConfig
from copytradeaster.onboarding.providers import SignedReadClient

log = get_logger(__name__)

CLOCK_DRIFT_MAX_MS = 2000


class PreflightExchangeReads(SignedReadClient, Protocol):
    """GET-only exchange surface needed by the checklist (no order writes)."""

    async def server_time(self) -> int: ...

    async def exchange_info(self, force: bool = False) -> ExchangeInfo: ...

    async def leverage_bracket(self, account_id: str, symbol: str) -> LeverageBracketResponse: ...


class PreflightSettings(Protocol):
    """Structural view of Settings; avoids importing config into onboarding."""

    rest_base_url: str
    ws_user_base: str
    position_mode: str
    allow_mainnet: bool
    symbol_whitelist: list[str]


class PreflightCommands(Protocol):
    async def in_flight(self) -> list: ...


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One checklist row; ``detail`` is operator-safe (no secrets/keys)."""

    name: str
    ok: bool
    blocked: bool
    detail: str


@dataclass(frozen=True, slots=True)
class MainnetPreflightReport:
    profile: str
    account_id: str
    checks: tuple[PreflightCheck, ...]

    @property
    def blocked(self) -> bool:
        return any(check.blocked for check in self.checks)

    @property
    def ready(self) -> bool:
        return not self.blocked and all(check.ok for check in self.checks)

    def blocked_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if check.blocked)


def _pass(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, ok=True, blocked=False, detail=detail)


def _block(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, ok=False, blocked=True, detail=detail)


def _warn(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, ok=False, blocked=False, detail=detail)


async def run_mainnet_preflight(
    *,
    profile: AsterNetworkProfile,
    settings: PreflightSettings,
    reads: PreflightExchangeReads,
    commands: PreflightCommands,
    account_id: str,
    expected_signer_address: str,
    user_address: str,
    agent_config: DirectOnboardingConfig,
    builder_address: str = "",
    builder_max_fee_rate: str = "0.0001",
    now_ms: Callable[[], int],
    clock_drift_max_ms: int = CLOCK_DRIFT_MAX_MS,
) -> MainnetPreflightReport:
    """Evaluate the unified checklist; never raises, never writes orders."""
    checks: list[PreflightCheck] = []

    # 1. host/profile: the composed Settings must point at the profile's hosts,
    # and mainnet additionally requires the explicit allow_mainnet override.
    rest_host = (urlsplit(settings.rest_base_url).hostname or "").lower().rstrip(".")
    ws_host = (urlsplit(settings.ws_user_base).hostname or "").lower().rstrip(".")
    profile_rest = (urlsplit(profile.rest_base_url).hostname or "").lower().rstrip(".")
    profile_ws = (urlsplit(profile.ws_user_base).hostname or "").lower().rstrip(".")
    if rest_host != profile_rest or ws_host != profile_ws:
        checks.append(_block("host_profile", f"settings hosts do not match {profile.name} profile"))
    elif profile.is_mainnet and not settings.allow_mainnet:
        checks.append(_block("host_profile", "MAINNET profile requires allow_mainnet"))
    else:
        checks.append(_pass("host_profile", f"hosts match {profile.name} profile"))

    # 2. trading chain: re-assert the profile invariant defensively.
    want_chain = 1666 if profile.is_mainnet else 714
    if profile.trading_chain_id != want_chain or profile.registration_chain_id != 56:
        checks.append(
            _block(
                "trading_chain",
                f"{profile.name} profile carries unexpected chains",
            )
        )
    else:
        checks.append(
            _pass(
                "trading_chain",
                f"trading={profile.trading_chain_id} registration=56",
            )
        )

    # 3. clock: exchange time must be inside the nonce window.
    try:
        server_ms = int(await reads.server_time())
        drift = abs(server_ms - int(now_ms()))
        if drift > clock_drift_max_ms:
            checks.append(_block("clock", f"drift {drift}ms exceeds {clock_drift_max_ms}ms"))
        else:
            checks.append(_pass("clock", f"drift {drift}ms"))
    except Exception as exc:
        log.warning("preflight clock read failed (%s)", type(exc).__name__)
        checks.append(_block("clock", "server time unreadable"))

    # 4-5. signer + Agent perms share one signed agent read.
    agent: AgentDTO | None = None
    try:
        agent = await reads.agent(account_id)
    except Exception as exc:
        log.warning("preflight agent read failed (%s)", type(exc).__name__)
        checks.append(_block("signer", "agent record unreadable"))
        checks.append(_block("agent_perms", "agent record unreadable"))
    else:
        actual = (agent.agent_address or "").strip().lower()
        expected = expected_signer_address.strip().lower()
        if not actual or not is_address(expected_signer_address.strip()) or actual != expected:
            checks.append(_block("signer", "agent/signer mismatch"))
        else:
            checks.append(_pass("signer", "agent address matches expected signer"))
        if agent is not None and any(c.name == "signer" and c.ok for c in checks):
            try:
                validate_agent_permissions(
                    agent,
                    expected_signer_address,
                    agent_config,
                    checked_at_ms=int(now_ms()),
                )
            except OnboardingError as exc:
                checks.append(_block("agent_perms", f"agent policy rejected: {exc.code.value}"))
            except Exception:
                checks.append(_block("agent_perms", "agent policy check failed"))
            else:
                checks.append(_pass("agent_perms", "read+perp granted, no withdraw, unexpired"))
        elif not any(c.name == "agent_perms" for c in checks):
            checks.append(_block("agent_perms", "agent/signer mismatch"))

    # 6. Builder proof: exact record required only when a builder is configured.
    if not builder_address:
        checks.append(_pass("builder_proof", "no builder configured"))
    elif agent is None:
        checks.append(_block("builder_proof", "agent record unreadable"))
    else:
        try:
            records = await reads.builder(account_id)
            validate_builder_record(
                list(records),
                user_address=user_address,
                builder_address=builder_address,
                max_fee_rate=builder_max_fee_rate,
            )
        except OnboardingError as exc:
            checks.append(_block("builder_proof", f"builder proof rejected: {exc.code.value}"))
        except Exception:
            log.warning("preflight builder read failed")
            checks.append(_block("builder_proof", "builder record unreadable"))
        else:
            checks.append(_pass("builder_proof", "builder record verified"))

    # 7. position mode: fail CLOSED on MAINNET (app.py:440 is fail-open today).
    want_hedge = settings.position_mode.strip().upper() == "HEDGE"
    try:
        is_hedge = bool(await reads.get_position_mode(account_id))
    except Exception:
        log.warning("preflight position-mode read failed")
        if profile.is_mainnet:
            checks.append(_block("position_mode", "mode metadata unreadable on MAINNET"))
        else:
            checks.append(
                _warn("position_mode", "mode metadata unreadable; exchange rejects wrong-side")
            )
    else:
        if is_hedge != want_hedge:
            checks.append(
                _block(
                    "position_mode",
                    f"exchange={('HEDGE' if is_hedge else 'ONE_WAY')} "
                    f"want={settings.position_mode.strip().upper()}",
                )
            )
        else:
            checks.append(_pass("position_mode", settings.position_mode.strip().upper()))

    # 8. follower readable: balances, positions, open orders.
    try:
        balances = list(await reads.balance(account_id))
        positions = list(await reads.position_risk(account_id))
        open_orders = list(await reads.open_orders(account_id))
    except Exception:
        log.warning("preflight follower reads failed")
        checks.append(_block("follower_readable", "balance/positions/orders unreadable"))
    else:
        open_qty = sum(1 for p in positions if p.position_amt != 0)
        checks.append(
            _pass(
                "follower_readable",
                f"balances={len(balances)} open_positions={open_qty} "
                f"open_orders={len(open_orders)}",
            )
        )

    # 9. exchangeInfo readable with tradable symbols.
    try:
        info = await reads.exchange_info()
        symbols = list(getattr(info, "symbols", []) or [])
        tradable = [s for s in symbols if getattr(s, "status", "") == "TRADING"]
    except Exception:
        log.warning("preflight exchangeInfo read failed")
        checks.append(_block("exchange_info", "exchangeInfo unreadable"))
    else:
        if not tradable:
            checks.append(_block("exchange_info", "exchangeInfo has no TRADING symbols"))
        else:
            checks.append(_pass("exchange_info", f"TRADING symbols={len(tradable)}"))

    # 10. leverage prep: bracket readable for the first whitelisted symbol.
    # Warn-only: the control-plane worker retries and the executor P0 gate
    # fails closed per order, so an unreadable bracket must not block boot.
    whitelist = list(settings.symbol_whitelist or [])
    if not whitelist:
        checks.append(_block("leverage_prep", "symbol whitelist is empty"))
    else:
        try:
            bracket = await reads.leverage_bracket(account_id, whitelist[0])
            count = len(getattr(bracket, "brackets", []) or [])
        except Exception:
            log.warning("preflight leverage bracket read failed")
            checks.append(_warn("leverage_prep", f"bracket unreadable for {whitelist[0]}"))
        else:
            checks.append(_pass("leverage_prep", f"{whitelist[0]} brackets={count}"))

    # 11. no unresolved SENDING/UNKNOWN: any in-flight row blocks readiness.
    try:
        rows = list(await commands.in_flight())
    except Exception:
        log.warning("preflight in-flight read failed")
        checks.append(_block("no_inflight", "command journal unreadable"))
    else:
        if rows:
            checks.append(_block("no_inflight", f"{len(rows)} unresolved command(s)"))
        else:
            checks.append(_pass("no_inflight", "no unresolved commands"))

    return MainnetPreflightReport(profile=profile.name, account_id=account_id, checks=tuple(checks))


__all__ = [
    "CLOCK_DRIFT_MAX_MS",
    "MainnetPreflightReport",
    "PreflightCheck",
    "PreflightCommands",
    "PreflightExchangeReads",
    "PreflightSettings",
    "run_mainnet_preflight",
]
