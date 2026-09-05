"""EIP-712 signing for Aster V3: trading context (Message) and registration
context (ApproveAgent/ApproveBuilder), ported from the official V3 reference
and aster-code-demo (utils.js.signEIP712Main / demo.py).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode

from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.network_profile import AsterNetworkProfile

EIP712_DOMAIN_BASE = {
    "name": "AsterSignTransaction",
    "version": "1",
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
TRADING_CHAIN_ID_MAINNET = 1666
TRADING_CHAIN_ID_TESTNET = 714


def next_nonce() -> int:
    """Microsecond-resolution nonce: seconds*1e6 + counter within the second.

    The DB-backed NonceStore is authoritative for persistence; this helper is
    the in-memory fallback used by scripts/tests.
    """
    counter = getattr(next_nonce, "_counter", 0) + 1
    next_nonce._counter = counter
    candidate = int(time.time()) * 1_000_000 + counter - 1
    last = getattr(next_nonce, "_last", 0)
    result = max(candidate, last + 1)
    next_nonce._last = result
    return result


def build_query_string(params: Sequence[tuple[str, str]]) -> str:
    """urlencoded query string; insertion order preserved — it IS the signed string."""
    return urlencode(list(params))


def trading_chain_id_for_profile(profile: AsterNetworkProfile) -> int:
    """Return the EIP-712 trading chain id carried by a network profile."""
    return profile.trading_chain_id


def assert_profile_chain(chain_id: int, profile: AsterNetworkProfile) -> None:
    """Fail closed when a caller mixes a chain id with the wrong profile."""
    if chain_id != profile.trading_chain_id:
        raise ValueError(
            f"refusing chain {chain_id} on {profile.name} profile (want {profile.trading_chain_id})"
        )


def sign_trading_with_profile(qs: str, private_key_hex: str, profile: AsterNetworkProfile) -> str:
    """Sign a trading query string under the profile's chain id (no 0x prefix)."""
    assert_profile_chain(profile.trading_chain_id, profile)
    return sign_trading(qs, private_key_hex, profile.trading_chain_id)


def sign_trading(qs: str, private_key_hex: str, chain_id: int) -> str:
    """Signs a trading query string with the signer key. Returns hex without 0x."""
    typed_data = encode_typed_data(
        domain_data={**EIP712_DOMAIN_BASE, "chainId": chain_id},
        message_types={"Message": [{"name": "msg", "type": "string"}]},
        message_data={"msg": qs},
    )
    signed = Account.sign_message(typed_data, private_key_hex)
    return signed.signature.hex()


def prepare_signed_request(
    params: dict[str, str],
    identity: Any,
    private_key_hex: str,
    nonce: int,
) -> tuple[str, str]:
    """Canonical order: business params as passed, then user, signer, nonce.

    Returns (signed_query_string, full_query_with_signature).
    """
    pairs = [(str(k), str(v)) for k, v in params.items()]
    pairs += [("user", identity.user), ("signer", identity.signer), ("nonce", str(nonce))]
    qs = build_query_string(pairs)
    sig = sign_trading(qs, private_key_hex, identity.chain_id)
    return qs, f"{qs}&signature={sig}"


def _capitalize_keys(params: dict[str, Any]) -> dict[str, Any]:
    return {k[0].upper() + k[1:]: v for k, v in params.items()}


def _field_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "uint256"
    return "string"


def stringify_param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def registration_typed_data(
    params: dict[str, Any],
    primary_type: str,
    chain_id: int = 56,
):
    """Build the exact EIP-712 typed data the user's main wallet must sign.

    Same capitalization/type inference as ``sign_registration``; used by the
    Builder flow both to hand the payload to a frontend wallet and to recover
    the signer address from the returned signature.
    """
    capitalized = _capitalize_keys(dict(params))
    return encode_typed_data(
        domain_data={**EIP712_DOMAIN_BASE, "chainId": chain_id},
        message_types={
            primary_type: [
                {"name": name, "type": _field_type(value)} for name, value in capitalized.items()
            ]
        },
        message_data=dict(capitalized),
    )


def recover_registration(
    params: dict[str, Any],
    signature_hex: str,
    primary_type: str,
    chain_id: int = 56,
) -> str:
    """Recover the signing address from a registration-context signature."""
    signature = signature_hex if signature_hex.startswith("0x") else f"0x{signature_hex}"
    typed_data = registration_typed_data(params, primary_type, chain_id)
    return Account.recover_message(typed_data, signature=signature)


def eip712_json_payload(
    params: dict[str, Any],
    primary_type: str,
    chain_id: int = 56,
) -> dict[str, Any]:
    """JSON structure for ``eth_signTypedData_v4`` from a browser wallet.

    Must stay byte-consistent with :func:`registration_typed_data`; unit
    tests assert that signing this payload recovers under the same key.
    """
    capitalized = _capitalize_keys(dict(params))
    message_fields = [
        {"name": name, "type": _field_type(value)} for name, value in capitalized.items()
    ]
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            primary_type: message_fields,
        },
        "primaryType": primary_type,
        "domain": {**EIP712_DOMAIN_BASE, "chainId": chain_id},
        "message": capitalized,
    }


def sign_registration(
    params: dict[str, Any],
    main_private_key_hex: str,
    primary_type: str,
    chain_id: int = 56,
) -> tuple[dict[str, str], str]:
    """Registration-context signature (ApproveAgent/ApproveBuilder).

    Signed by the account's MAIN wallet; field keys are capitalized and types
    inferred dynamically (bool->bool, int->uint256, else string). Domain chainId
    is the registration chain (56 for EVM); `signatureChainId` is appended to
    the request alongside the signature.

    Caller pre-populates params with business fields + asterChain + user + nonce.
    Returns (params incl. signature+signatureChainId, urlencoded query string).
    """
    capitalized = _capitalize_keys(dict(params))
    typed_data = registration_typed_data(params, primary_type, chain_id)
    signed = Account.sign_message(typed_data, main_private_key_hex)
    sig = signed.signature.hex()
    out = {k: stringify_param_value(v) for k, v in capitalized.items()}
    out["signature"] = sig
    out["signatureChainId"] = str(chain_id)
    return out, build_query_string(list(out.items()))
