"""Contract tests for signed GET /fapi/v3/agent."""

import httpx
import pytest
import respx

from copytradeaster.aster.models import AgentDTO
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError
from tests.unit.aster.conftest import SIGNER, FakeIdentities, FakeKeys, FakeNonces

BASE = "https://fapi.asterdex-testnet.com"


def make_client() -> AsterClient:
    return AsterClient(
        http=httpx.AsyncClient(),
        base_url=BASE,
        limiter=RateLimits(10**9, 10**9, 10**9),
        identities=FakeIdentities(),
        keys=FakeKeys(),
        nonces=FakeNonces(),
    )


AGENT = {
    "agentAddress": SIGNER,
    "agentName": "copytradeaster",
    "expired": 1_900_000_000_000,
    "ipWhitelist": "203.0.113.7/32",
    "canRead": True,
    "canSpotTrade": False,
    "canPerpTrade": True,
    "canWithdraw": False,
}


@respx.mock
async def test_agent_returns_matching_typed_record():
    route = respx.get(f"{BASE}/fapi/v3/agent").respond(json={"data": [AGENT]})
    client = make_client()

    result = await client.agent("copyc1")

    assert route.called
    assert isinstance(result, AgentDTO)
    assert result.agent_address == SIGNER
    assert result.can_perp_trade is True
    assert result.can_withdraw is False
    sent = dict(route.calls[0].request.url.params)
    assert sent["user"]
    assert sent["signer"] == SIGNER
    assert sent["nonce"]
    assert sent["signature"]


@respx.mock
async def test_agent_does_not_return_a_different_signer():
    route = respx.get(f"{BASE}/fapi/v3/agent").respond(
        json=[{**AGENT, "agentAddress": "0x" + "11" * 20}]
    )
    client = make_client()

    with pytest.raises(AsterError) as exc_info:
        await client.agent("copyc1")

    assert exc_info.value.code == -2015
    assert "secret" not in str(exc_info.value).lower()
    assert route.called
