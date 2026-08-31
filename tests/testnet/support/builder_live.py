"""Shared testnet helper utilities for live Builder tests."""

from __future__ import annotations

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


def load_wallet_section(name: str) -> tuple[str, str]:
    """Return (main_evm_address, main_evm_private_key) from wallet.txt."""
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
    section = sections.get(name) or {}
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
) -> dict[str, Any]:
    """Read agent, balance, positions, open orders, dual side position."""
    await limiter.acquire_request(5)
    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request({}, identity, private_key, nonce)
    agent_resp = await http.get(f"{base_url}/fapi/v3/agent?{full}")
    agent = agent_resp.json() if agent_resp.status_code == 200 else None

    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request({}, identity, private_key, nonce)
    bal_resp = await http.get(f"{base_url}/fapi/v3/balance?{full}")
    balances = bal_resp.json() if bal_resp.status_code == 200 else []

    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request({}, identity, private_key, nonce)
    pos_resp = await http.get(f"{base_url}/fapi/v3/positionRisk?{full}")
    positions = pos_resp.json() if pos_resp.status_code == 200 else []

    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request({}, identity, private_key, nonce)
    orders_resp = await http.get(f"{base_url}/fapi/v3/openOrders?{full}")
    open_orders = orders_resp.json() if orders_resp.status_code == 200 else []

    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request({}, identity, private_key, nonce)
    dual_resp = await http.get(f"{base_url}/fapi/v3/positionSide/dual?{full}")
    dual = dual_resp.json() if dual_resp.status_code == 200 else {}

    return {
        "agent": agent,
        "balances": balances,
        "positions": positions,
        "open_orders": open_orders,
        "dual_side_position": bool(dual.get("dualSidePosition")),
    }


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
