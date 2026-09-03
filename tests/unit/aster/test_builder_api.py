"""Contract tests for signed GET /fapi/v3/builder (P2.6).

The adapter returns only typed ``BuilderDTO`` records — never raw dicts —
so the attestation validator above it cannot accidentally trust transport
shapes.  Malformed wire payloads fail the whole read closed.
"""

import httpx
import pytest
import respx

from copytradeaster.aster.models import BuilderDTO
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient, AsterError
from tests.unit.aster.conftest import MAIN_ADDRESS, SIGNER, FakeIdentities, FakeKeys, FakeNonces

BASE = "https://fapi.asterdex-testnet.com"
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"


def make_client() -> AsterClient:
    return AsterClient(
        http=httpx.AsyncClient(),
        base_url=BASE,
        limiter=RateLimits(10**9, 10**9, 10**9),
        identities=FakeIdentities(),
        keys=FakeKeys(),
        nonces=FakeNonces(),
    )


def _record(**changes: object) -> dict:
    values: dict[str, object] = {
        "userAddress": MAIN_ADDRESS,
        "builderAddress": BUILDER,
        "builderName": "copytradeaster",
        "maxFeeRate": "0.0001",
    }
    values.update(changes)
    return values


@respx.mock
async def test_builder_returns_typed_records():
    route = respx.get(f"{BASE}/fapi/v3/builder").respond(json={"data": [_record()]})
    client = make_client()

    result = await client.builder("copyc1")

    assert route.called
    assert len(result) == 1
    assert isinstance(result[0], BuilderDTO)
    assert result[0].builder_address == BUILDER
    assert result[0].max_fee_rate == "0.0001"
    sent = dict(route.calls[0].request.url.params)
    assert sent["signer"] == SIGNER
    assert sent["nonce"]
    assert sent["signature"]


@respx.mock
async def test_builder_accepts_bare_list_shape():
    respx.get(f"{BASE}/fapi/v3/builder").respond(json=[_record(), _record()])
    client = make_client()

    result = await client.builder("copyc1")

    assert len(result) == 2
    assert all(isinstance(record, BuilderDTO) for record in result)


@respx.mock
async def test_builder_coerces_numeric_fee_rate_to_string():
    # The live wire sends maxFeeRate as a JSON number; the DTO keeps the
    # shortest round-trip string so fee math never touches float.
    respx.get(f"{BASE}/fapi/v3/builder").respond(json={"data": [_record(maxFeeRate=0.0001)]})
    client = make_client()

    (record,) = await client.builder("copyc1")

    assert record.max_fee_rate == "0.0001"
    assert isinstance(record.max_fee_rate, str)


@respx.mock
async def test_builder_never_returns_raw_dicts():
    respx.get(f"{BASE}/fapi/v3/builder").respond(json={"data": [_record()]})
    client = make_client()

    (record,) = await client.builder("copyc1")

    assert not isinstance(record, dict)
    assert record.model_dump(by_alias=True)["builderAddress"] == BUILDER


@respx.mock
async def test_builder_wrong_typed_record_fails_closed():
    respx.get(f"{BASE}/fapi/v3/builder").respond(json={"data": [_record(), {"userAddress": 123}]})
    client = make_client()

    with pytest.raises(AsterError) as exc_info:
        await client.builder("copyc1")

    assert exc_info.value.code == -1000


@respx.mock
async def test_builder_unknown_shape_surfaces_as_empty_typed_record():
    # HTTP 200 without an error envelope or known wrapper: documented
    # boundary — the read returns an empty typed record (never a raw dict)
    # and the attestation validator rejects it fail-closed downstream.
    respx.get(f"{BASE}/fapi/v3/builder").respond(json={"nope": True})
    client = make_client()

    (record,) = await client.builder("copyc1")

    assert isinstance(record, BuilderDTO)
    assert record.user_address is None
    assert record.builder_address is None
    assert record.max_fee_rate is None


@respx.mock
async def test_builder_error_message_carries_no_secret():
    respx.get(f"{BASE}/fapi/v3/builder").respond(
        400, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
    )
    client = make_client()

    with pytest.raises(AsterError) as exc_info:
        await client.builder("no-such-account")

    assert "secret" not in str(exc_info.value).lower()
    assert "private" not in str(exc_info.value).lower()


@respx.mock
async def test_builder_non_dict_record_fails_closed():
    respx.get(f"{BASE}/fapi/v3/builder").respond(json={"data": ["0xdead"]})
    client = make_client()

    with pytest.raises(AsterError) as exc_info:
        await client.builder("copyc1")

    assert exc_info.value.code == -1000


@respx.mock
async def test_builder_scalar_body_fails_closed():
    respx.get(f"{BASE}/fapi/v3/builder").respond(text="42")
    client = make_client()

    with pytest.raises(AsterError) as exc_info:
        await client.builder("copyc1")

    assert exc_info.value.code == -1000
