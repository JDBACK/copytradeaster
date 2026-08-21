from urllib.parse import parse_qsl, urlsplit

import httpx
import respx
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.management_api import ManagementClient
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.config import Settings
from tests.unit.aster.conftest import (
    MAIN_ADDRESS,
    MAIN_KEY,
    SIGNER,
    USER,
    FakeIdentities,
    FakeKeys,
    FakeNonces,
)


def query_of(call) -> dict:
    return dict(parse_qsl(urlsplit(str(call.request.url)).query, keep_blank_values=True))


BASE = "https://fapi.test"


def make_client(builder_address="0x" + "bb" * 20, with_main=True):
    cfg = Settings(encryption_key="ab" * 32, builder_address=builder_address)
    http = httpx.AsyncClient()
    return ManagementClient(
        http=http,
        base_url=BASE,
        limiter=RateLimits(10**9, 10**9, 10**9),
        identities=FakeIdentities(),
        keys=FakeKeys(main_key=MAIN_KEY if with_main else None),
        nonces=FakeNonces(),
        cfg=cfg,
    )


APPROVE_OK = {"code": 0, "msg": "success"}


class TestApproveAgent:
    @respx.mock
    async def test_typed_data_structure_and_recovery(self):
        route = respx.post(f"{BASE}/fapi/v3/approveAgent").respond(json=APPROVE_OK)
        client = make_client()
        body = await client.approve_agent(
            "copyc1", agent_name="copytradeaster", expired_ms=1893456000000
        )
        assert body == APPROVE_OK
        sent = query_of(route.calls[0])
        # capitalized business fields
        assert sent["AgentName"] == "copytradeaster"
        assert sent["AgentAddress"] == SIGNER
        assert sent["CanPerpTrade"] == "true"
        assert sent["CanSpotTrade"] == "false"
        assert sent["CanWithdraw"] == "false"
        assert sent["Expired"] == "1893456000000"
        assert sent["AsterChain"] == "Testnet"
        assert sent["User"] == USER
        assert "Nonce" in sent
        assert sent["signatureChainId"] == "56"
        # builder fields included when configured
        assert sent["Builder"] == "0x" + "bb" * 20
        assert sent["MaxFeeRate"] == "0.0001"
        assert sent["BuilderName"] == "copytradeaster"
        # signature recovers main wallet over capitalized message
        msg = {k: v for k, v in sent.items() if k not in ("signature", "signatureChainId")}
        # exact field order as built by approve_agent:
        # business fields, builder triple, asterChain, user, nonce
        types = {
            "ApproveAgent": [
                {"name": "AgentName", "type": "string"},
                {"name": "AgentAddress", "type": "string"},
                {"name": "IpWhitelist", "type": "string"},
                {"name": "Expired", "type": "uint256"},
                {"name": "CanSpotTrade", "type": "bool"},
                {"name": "CanPerpTrade", "type": "bool"},
                {"name": "CanWithdraw", "type": "bool"},
                {"name": "Builder", "type": "string"},
                {"name": "MaxFeeRate", "type": "string"},
                {"name": "BuilderName", "type": "string"},
                {"name": "AsterChain", "type": "string"},
                {"name": "User", "type": "string"},
                {"name": "Nonce", "type": "uint256"},
            ]
        }
        typed = encode_typed_data(
            domain_data={
                "name": "AsterSignTransaction",
                "version": "1",
                "chainId": 56,
                "verifyingContract": "0x0000000000000000000000000000000000000000",
            },
            message_types=types,
            message_data=msg,
        )
        recovered = Account.recover_message(typed, signature="0x" + sent["signature"])
        assert recovered == MAIN_ADDRESS

    @respx.mock
    async def test_with_builder_false_omits_builder_fields(self):
        route = respx.post(f"{BASE}/fapi/v3/approveAgent").respond(json=APPROVE_OK)
        client = make_client()
        await client.approve_agent("copyc1", agent_name="a", expired_ms=1, with_builder=False)
        sent = query_of(route.calls[0])
        assert not any(k.startswith("Builder") or k == "MaxFeeRate" for k in sent)

    @respx.mock
    async def test_empty_builder_address_omits_builder_fields(self):
        route = respx.post(f"{BASE}/fapi/v3/approveAgent").respond(json=APPROVE_OK)
        client = make_client(builder_address="")
        await client.approve_agent("copyc1", agent_name="a", expired_ms=1)
        sent = query_of(route.calls[0])
        assert "Builder" not in sent


class TestApproveBuilder:
    @respx.mock
    async def test_sends_max_fee_rate_and_signature(self):
        route = respx.post(f"{BASE}/fapi/v3/approveBuilder").respond(json=APPROVE_OK)
        client = make_client()
        await client.approve_builder("copyc1", max_fee_rate="0.0005")
        sent = query_of(route.calls[0])
        assert sent["MaxFeeRate"] == "0.0005"
        assert sent["signatureChainId"] == "56"
        assert sent["User"] == USER

    async def test_requires_main_key(self):
        client = make_client(with_main=False)
        try:
            await client.approve_builder("copyc1")
        except Exception as exc:
            assert "no main private key" in str(exc).lower() or "main" in str(exc).lower()
        else:
            raise AssertionError("must fail without main key")


class TestSettingsValidation:
    def test_encryption_key_required(self, monkeypatch):
        monkeypatch.delenv("CT_ENCRYPTION_KEY", raising=False)
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # no encryption_key -> validation error
