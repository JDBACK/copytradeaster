"""Shared fakes for aster adapter unit tests."""

from __future__ import annotations

import pytest
from eth_account import Account

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.domain.models import TradingIdentity

KEY = "0x" + "33" * 32
MAIN_KEY = "0x" + "44" * 32
USER = "0x" + "aa" * 20
SIGNER = Account.from_key(KEY).address
MAIN_ADDRESS = Account.from_key(MAIN_KEY).address


class FakeIdentities:
    def identity(self, account_id: str) -> TradingIdentity:
        return TradingIdentity(account_id=account_id, user=USER, signer=SIGNER, chain_id=714)


class FakeKeys:
    def __init__(self, main_key: str | None = None) -> None:
        self._main = main_key

    def private_key(self, account_id: str) -> str:
        return KEY

    def main_private_key(self, account_id: str) -> str | None:
        return self._main


class FakeNonces:
    def __init__(self) -> None:
        self.n = 10_000

    async def next_nonce(self, signer: str) -> int:
        self.n += 1
        return self.n


@pytest.fixture
def limiter() -> RateLimits:
    return RateLimits(weight_per_min=10**9, orders_per_min=10**9, orders_per_10s=10**9)
