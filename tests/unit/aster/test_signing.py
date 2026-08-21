from urllib.parse import parse_qsl

from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.signing import (
    EIP712_DOMAIN_BASE,
    TRADING_CHAIN_ID_MAINNET,
    TRADING_CHAIN_ID_TESTNET,
    build_query_string,
    next_nonce,
    prepare_signed_request,
    sign_registration,
    sign_trading,
)
from copytradeaster.domain.models import TradingIdentity

KEY = "0x" + "11" * 32
MAIN_KEY = "0x" + "22" * 32
ADDRESS = Account.from_key(KEY).address
MAIN_ADDRESS = Account.from_key(MAIN_KEY).address

IDENTITY = TradingIdentity(
    account_id="copyc1", user="0xuser", signer=ADDRESS, chain_id=TRADING_CHAIN_ID_TESTNET
)


class TestBuildQueryString:
    def test_preserves_insertion_order(self):
        pairs = [("symbol", "BTCUSDT"), ("side", "SELL"), ("quantity", "0.002")]
        qs = build_query_string(pairs)
        assert list(parse_qsl(qs, keep_blank_values=True)) == pairs

    def test_encodes_specials(self):
        qs = build_query_string([("a b", "c&d=e")])
        assert dict(parse_qsl(qs)) == {"a b": "c&d=e"}
        assert "+" not in qs.split("=", 1)[1].replace("%26", "").replace("+", " ")

    def test_matches_urlencode_of_dict_order(self):
        d = {"symbol": "ETHUSDT", "type": "MARKET", "quantity": "1"}
        assert build_query_string(list(d.items())) == "symbol=ETHUSDT&type=MARKET&quantity=1"


class TestSignTrading:
    def test_deterministic(self):
        qs = "symbol=BTCUSDT&side=BUY&nonce=1"
        assert sign_trading(qs, KEY, 714) == sign_trading(qs, KEY, 714)

    def test_recovers_signer_address(self):
        qs = "symbol=BTCUSDT&side=BUY&nonce=2"
        sig = sign_trading(qs, KEY, TRADING_CHAIN_ID_TESTNET)
        assert not sig.startswith("0x")
        typed = encode_typed_data(
            domain_data={**EIP712_DOMAIN_BASE, "chainId": TRADING_CHAIN_ID_TESTNET},
            message_types={"Message": [{"name": "msg", "type": "string"}]},
            message_data={"msg": qs},
        )
        recovered = Account.recover_message(typed, signature="0x" + sig)
        assert recovered == ADDRESS

    def test_chain_id_changes_signature(self):
        qs = "symbol=BTCUSDT&side=BUY"
        s714 = sign_trading(qs, KEY, TRADING_CHAIN_ID_TESTNET)
        s1666 = sign_trading(qs, KEY, TRADING_CHAIN_ID_MAINNET)
        assert s714 != s1666


class TestPrepareSignedRequest:
    def test_canonical_order_user_signer_nonce_last(self):
        params = {"symbol": "BTCUSDT", "side": "BUY"}
        qs, full = prepare_signed_request(params, IDENTITY, KEY, nonce=42)
        keys = [k for k, _ in parse_qsl(qs)]
        assert keys == ["symbol", "side", "user", "signer", "nonce"]
        assert full.startswith(qs + "&signature=")
        assert len(full.split("&signature=")[1]) == 130  # 65 bytes hex without 0x

    def test_signature_covers_params(self):
        params = {"symbol": "BTCUSDT", "side": "BUY"}
        qs, _ = prepare_signed_request(params, IDENTITY, KEY, nonce=1)
        sig = sign_trading(qs, KEY, IDENTITY.chain_id)
        _, full = prepare_signed_request(params, IDENTITY, KEY, nonce=1)
        assert full.endswith(sig)


class TestNextNonce:
    def test_monotonic_in_memory(self):
        a = next_nonce()
        b = next_nonce()
        assert b > a


class TestSignRegistration:
    def test_structure_and_recovery(self):
        params = {
            "agentName": "copytradeaster",
            "agentAddress": ADDRESS,
            "expired": 1893456000000,
            "canPerpTrade": True,
            "canSpotTrade": False,
            "asterChain": "Testnet",
            "user": "0xuser",
            "nonce": 123,
        }
        out, qs = sign_registration(params, MAIN_KEY, primary_type="ApproveAgent")
        assert out["AgentName"] == "copytradeaster"  # capitalization
        assert out["signatureChainId"] == "56"
        assert "signature" in out
        sent = dict(parse_qsl(qs))
        assert sent["CanPerpTrade"] == "true"  # bool -> lowercase string
        assert sent["Nonce"] == "123"
        assert sent["Expired"] == "1893456000000"  # int -> uint256 field
        # signature recovers the MAIN wallet address over the capitalized message
        msg = {k: v for k, v in out.items() if k not in ("signature", "signatureChainId")}
        typed = encode_typed_data(
            domain_data={**EIP712_DOMAIN_BASE, "chainId": 56},
            message_types={
                "ApproveAgent": [
                    {"name": "AgentName", "type": "string"},
                    {"name": "AgentAddress", "type": "string"},
                    {"name": "Expired", "type": "uint256"},
                    {"name": "CanPerpTrade", "type": "bool"},
                    {"name": "CanSpotTrade", "type": "bool"},
                    {"name": "AsterChain", "type": "string"},
                    {"name": "User", "type": "string"},
                    {"name": "Nonce", "type": "uint256"},
                ],
            },
            message_data=msg,
        )
        recovered = Account.recover_message(typed, signature="0x" + out["signature"])
        assert recovered == MAIN_ADDRESS

    def test_builder_fields_pass_through(self):
        params = {
            "builder": "0xbuilder",
            "maxFeeRate": "0.0001",
            "builderName": "x",
            "asterChain": "Testnet",
            "user": "0xu",
            "nonce": 5,
        }
        out, _ = sign_registration(params, MAIN_KEY, primary_type="ApproveBuilder")
        assert out["Builder"] == "0xbuilder"
        assert out["MaxFeeRate"] == "0.0001"
        assert out["BuilderName"] == "x"
        assert out["signatureChainId"] == "56"
