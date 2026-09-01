"""Shared testnet helper utilities for live Builder tests."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import prepare_signed_request
from copytradeaster.domain.models import TradingIdentity
from copytradeaster.storage.repositories import NonceStore

WALLET_FILE = Path("wallet.txt")
UNKNOWN_READ_CODES = frozenset({-1000, -1006, -1007})


def _wallet_sections() -> dict[str, dict[str, str]]:
    if not WALLET_FILE.exists():
        pytest.skip("wallet.txt not found")
    text = WALLET_FILE.read_text(encoding="utf-8")
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith(("evm", "api", "privte")):
            current = stripped
            sections.setdefault(current, {})
        elif current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            sections[current][key.strip().lower()] = value.strip()
    return sections


def load_wallet_section(name: str) -> tuple[str, str]:
    """Return (main_evm_address, main_evm_private_key) from wallet.txt."""
    section = _wallet_sections().get(name) or {}
    address = section.get("evm adress", "")
    if not address.startswith("0x"):
        pytest.skip(f"wallet.txt section {name!r} has no evm adress")
    hex_keys = [v for v in section.values() if v.startswith("0x") and len(v) == 66]
    for candidate in hex_keys:
        try:
            if Account.from_key(candidate).address.lower() == address.lower():
                return address, candidate
        except Exception:
            continue
    pytest.skip(f"wallet.txt section {name!r} has no main-key matching its evm address")


def load_api_wallet_section(name: str) -> tuple[str, str, str]:
    """Return (main user, API signer, API private key) from wallet.txt.

    The values are validated by deriving the signer address from the private
    key. They are never included in assertion messages or logs.
    """
    section = _wallet_sections().get(name) or {}
    user_address = section.get("evm adress", "")
    signer_address = section.get("api wallet adress", "")
    if not user_address.startswith("0x") or not signer_address.startswith("0x"):
        pytest.skip(f"wallet.txt section {name!r} has no user/API wallet pair")
    for candidate in section.values():
        if not candidate.startswith("0x") or len(candidate) != 66:
            continue
        try:
            if Account.from_key(candidate).address.casefold() == signer_address.casefold():
                return user_address, signer_address, candidate
        except Exception:
            continue
    pytest.skip(f"wallet.txt section {name!r} has no key matching its API wallet")


def ensure_testnet_guard(
    base_url: str,
    trading_chain_id: int = 714,
    registration_chain_id: int = 56,
) -> None:
    """Fail closed before state-changing calls if base URL or chain is not testnet."""
    if "fapi.asterdex-testnet.com" not in base_url.lower():
        pytest.fail(f"Safety guard: refusing non-testnet host {base_url!r}")
    if trading_chain_id != 714:
        pytest.fail(f"Safety guard: refusing non-testnet trading chain {trading_chain_id}")
    if registration_chain_id != 56:
        pytest.fail(
            f"Safety guard: refusing non-testnet registration chain {registration_chain_id}"
        )


async def get_live_account_state(
    http: httpx.AsyncClient,
    base_url: str,
    identity: TradingIdentity,
    private_key: str,
    nonce_store: NonceStore,
    limiter: RateLimits,
    *,
    include_agent: bool = True,
) -> dict[str, Any]:
    """Read agent, balance, positions, open orders, dual side position."""
    agent = None
    if include_agent:
        agent = await signed_read_json(
            http,
            base_url,
            "/fapi/v3/agent",
            {},
            identity,
            private_key,
            nonce_store,
            limiter,
            weight=5,
            allowed_error_codes=frozenset({-2015, -2016}),
        )
    balances = await signed_read_json(
        http,
        base_url,
        "/fapi/v3/balance",
        {},
        identity,
        private_key,
        nonce_store,
        limiter,
        weight=5,
    )
    positions = await signed_read_json(
        http,
        base_url,
        "/fapi/v3/positionRisk",
        {},
        identity,
        private_key,
        nonce_store,
        limiter,
        weight=5,
    )
    open_orders = await signed_read_json(
        http,
        base_url,
        "/fapi/v3/openOrders",
        {"symbol": "BTCUSDT"},
        identity,
        private_key,
        nonce_store,
        limiter,
        weight=1,
    )
    dual = await signed_read_json(
        http,
        base_url,
        "/fapi/v3/positionSide/dual",
        {},
        identity,
        private_key,
        nonce_store,
        limiter,
        weight=30,
    )

    if not isinstance(balances, list):
        pytest.fail("signed balance read returned a non-list payload")
    if not isinstance(positions, list):
        pytest.fail("signed positionRisk read returned a non-list payload")
    if not isinstance(open_orders, list):
        pytest.fail("signed openOrders read returned a non-list payload")
    if not isinstance(dual, dict):
        pytest.fail("signed position-mode read returned a non-object payload")

    return {
        "agent": agent,
        "balances": balances,
        "positions": positions,
        "open_orders": open_orders,
        "dual_side_position": bool(dual.get("dualSidePosition")),
    }


async def signed_read_json(
    http: httpx.AsyncClient,
    base_url: str,
    path: str,
    params: dict[str, str],
    identity: TradingIdentity,
    private_key: str,
    nonce_store: NonceStore,
    limiter: RateLimits,
    *,
    weight: int,
    attempts: int = 8,
    allowed_error_codes: frozenset[int] = frozenset(),
) -> dict[str, Any] | list[Any] | None:
    """Perform a retryable signed GET with a fresh nonce per attempt.

    Only idempotent reads are retried. Definitive exchange rejections fail
    immediately unless the caller explicitly allows their numeric code.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    last_category = "unknown"
    for attempt in range(1, attempts + 1):
        await limiter.acquire_request(weight)
        nonce = await nonce_store.next_nonce(identity.signer)
        _, full = prepare_signed_request(params, identity, private_key, nonce)
        try:
            response = await http.get(f"{base_url.rstrip('/')}{path}?{full}")
        except httpx.TransportError as exc:
            last_category = type(exc).__name__
        else:
            try:
                body = response.json()
            except ValueError:
                body = None
                code = response.status_code
                retryable = response.status_code >= 500 or response.status_code < 400
            else:
                code = int(body.get("code", 0) or 0) if isinstance(body, dict) else 0
                if code in allowed_error_codes:
                    return None
                retryable = response.status_code >= 500 or code in UNKNOWN_READ_CODES
                if response.status_code < 400 and code >= 0:
                    if isinstance(body, (dict, list)):
                        return body
                    pytest.fail(f"signed GET {path} returned an unsupported payload type")
            last_category = f"http={response.status_code},code={code}"
            if not retryable:
                pytest.fail(f"signed GET {path} was rejected ({last_category})")
        if attempt < attempts:
            await asyncio.sleep(min(0.5 * attempt, 1.5))
    pytest.fail(
        f"signed GET {path} remained unavailable after {attempts} attempts ({last_category})"
    )


async def best_effort_flatten(
    http: httpx.AsyncClient,
    base_url: str,
    identity: TradingIdentity,
    private_key: str,
    nonce_store: NonceStore,
    limiter: RateLimits,
    symbol: str = "BTCUSDT",
) -> None:
    """Best-effort cancel orders and close non-zero positions for test cleanliness."""
    try:
        # Cancel all open orders on symbol
        await limiter.acquire_request(1)
        nonce = await nonce_store.next_nonce(identity.signer)
        _, full = prepare_signed_request({"symbol": symbol}, identity, private_key, nonce)
        await http.delete(f"{base_url}/fapi/v3/allOpenOrders?{full}")
    except Exception:
        pass

    try:
        # Check positions and close if non-zero
        await limiter.acquire_request(5)
        nonce = await nonce_store.next_nonce(identity.signer)
        _, full = prepare_signed_request({"symbol": symbol}, identity, private_key, nonce)
        resp = await http.get(f"{base_url}/fapi/v3/positionRisk?{full}")
        positions = resp.json() if resp.status_code == 200 else []
        for p in positions:
            amt = Decimal(str(p.get("positionAmt", "0")))
            if amt != 0:
                side = "SELL" if amt > 0 else "BUY"
                pos_side = str(p.get("positionSide", "BOTH"))
                params = {
                    "symbol": symbol,
                    "side": side,
                    "type": "MARKET",
                    "quantity": str(abs(amt)),
                }
                if pos_side in {"LONG", "SHORT"}:
                    params["positionSide"] = pos_side
                else:
                    params["reduceOnly"] = "true"
                nonce = await nonce_store.next_nonce(identity.signer)
                _, full = prepare_signed_request(params, identity, private_key, nonce)
                await http.post(f"{base_url}/fapi/v3/order?{full}", content=b"")
    except Exception:
        pass
