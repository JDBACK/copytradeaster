"""Full Mini App part-1 flow over the real HTTP stack against a fake exchange.

Proves the layer boundary that unit tests stub out: aiohttp transport (real
HMAC initData check) -> UserApplicationService -> BuilderOnboardingService
(real EIP-712 wallet signing) -> httpx (MockTransport exchange emulation) ->
encrypted SQLite commit + paused activation.  No real network anywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.app import App
from copytradeaster.application.adapters import build_stage2_composition
from copytradeaster.config import Settings
from copytradeaster.miniapp.server import create_miniapp_app
from copytradeaster.storage.database import Database
from copytradeaster.trading.leverage import PrepareReport

BOT_TOKEN = "123456:TEST-TOKEN"
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
EGRESS_IP = "203.0.113.7"
USER_WALLET = Account.create()
USER_ADDRESS = USER_WALLET.address


def _init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF" + "0" * 10,
        "user": f'{{"id":{user_id},"first_name":"T"}}',
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


class FakeExchange:
    """The smallest honest testnet emulator for the Builder preflight path."""

    def __init__(self) -> None:
        self.approvals: list[dict[str, str]] = []
        self.lose_next_approval_response = False

    def agent_for(self, user: str) -> dict | None:
        for params in self.approvals:
            if params.get("user") == user:
                return {
                    "agentAddress": params["agentAddress"],
                    "agentName": params.get("agentName", "copytradeaster"),
                    "expired": int(params["expired"]),
                    "ipWhitelist": f"{EGRESS_IP}/32",
                    "canRead": True,
                    "canSpotTrade": params["canSpotTrade"] == "true",
                    "canPerpTrade": params["canPerpTrade"] == "true",
                    "canWithdraw": params["canWithdraw"] == "true",
                }
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = {k: v for k, v in request.url.params.items()}
        path = request.url.path
        if request.method == "POST" and path == "/fapi/v3/approveAgent":
            self.approvals.append(params)
            if self.lose_next_approval_response:
                self.lose_next_approval_response = False
                raise httpx.ReadTimeout(
                    "approveAgent response lost after remote acceptance",
                    request=request,
                )
            return httpx.Response(200, json={"code": 0, "msg": "ok"})
        if request.method == "GET" and path == "/fapi/v3/agent":
            agent = self.agent_for(params.get("user", ""))
            if agent is None:
                return httpx.Response(
                    200,
                    json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."},
                )
            return httpx.Response(200, json=agent)
        if request.method == "GET" and path == "/fapi/v3/balance":
            return httpx.Response(
                200,
                json=[
                    {
                        "asset": "USDT",
                        "balance": "1000",
                        "crossWalletBalance": "1000",
                        "crossUnPnl": "0",
                        "availableBalance": "1000",
                        "maxWithdrawAmount": "0",
                    }
                ],
            )
        if request.method == "GET" and path == "/fapi/v3/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0",
                        "entryPrice": "0",
                        "markPrice": "65000",
                        "leverage": 3,
                        "liquidationPrice": "0",
                        "marginType": "CROSSED",
                        "positionSide": "BOTH",
                        "unRealizedProfit": "0",
                    }
                ],
            )
        if request.method == "GET" and path == "/fapi/v3/openOrders":
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/fapi/v3/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        if request.method == "GET" and path == "/fapi/v3/leverageBracket":
            symbol = params.get("symbol", "BTCUSDT")
            return httpx.Response(
                200,
                json={"symbol": symbol, "brackets": [{"bracket": 1, "initialLeverage": 75}]},
            )
        return httpx.Response(404, json={"code": -1, "msg": f"fake has no route for {path}"})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        encryption_key="11" * 32,
        db_path=tmp_path / "miniapp-flow.sqlite3",
        log_path=tmp_path / "logs" / "miniapp-flow.log",
        telegram_bot_token=BOT_TOKEN,
        builder_address=BUILDER,
        builder_name="copytradeaster",
        max_fee_rate="0.0001",
        aster_egress_ips=[EGRESS_IP],
        require_agent_ip_whitelist=True,
        # Hermetic: the developer's local .env (real hedge mode, no ip
        # whitelist, extra symbols) must not leak into the fixture.
        position_mode="ONE_WAY",
        testnet_ip_whitelist_override=False,
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
    )


@pytest.fixture
async def env(tmp_path: Path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()
    app = App(settings, db=db)
    exchange = FakeExchange()

    # The leverage control-plane needs the master ("main") identity for its
    # catalog probe; production boot provides it, this flow test does not.
    # Leverage gating has its own signed coverage, so the flow test stubs a
    # clean prepare report and keeps the builder boundary in focus.
    class _StubLeverage:
        async def prepare_account(self, account_id, symbols):
            return PrepareReport(confirmed={s: 75 for s in symbols}, failed={})

    app.leverage = _StubLeverage()
    # Route every exchange call through the in-process emulator; the base URL
    # stays the official testnet host so the fail-closed guards stay armed.
    # The swap happens before composition: BuilderOnboardingService captures
    # the http client at construction.
    app.http = httpx.AsyncClient(transport=httpx.MockTransport(exchange.handle))
    composition = build_stage2_composition(app, admin_ids=[999])
    client = TestClient(TestServer(create_miniapp_app(settings, composition.application)))
    await client.start_server()
    yield client, exchange, app, composition
    await client.close()
    await app.stop()
    await app.http.aclose()
    await db.close()


def _auth(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": _init_data(user_id)}


async def test_full_builder_flow_commits_paused_over_real_http_stack(env):
    client, exchange, app, composition = env
    other_wallet = Account.create().address

    # 0. Access control plane: captcha-qualified request, then admin approval.
    request = await composition.application.submit_access_request(
        777, 777, username="follower", display_name="Follower"
    )
    decided = await composition.application.admin_decide_access(
        999, request.request_id, approve=True
    )
    assert decided.status.value == "APPROVED"

    # 1. Session gate: approved, nothing connected yet.
    resp = await client.post("/miniapp/api/session", headers=_auth(777))
    assert resp.status == 200
    session = await resp.json()
    assert session["access_state"] == "approved"
    assert session["account_connected"] is False

    # 2. Prepare: typed review payload with the combined signature plan.
    resp = await client.post(
        "/miniapp/api/builder/prepare",
        json={"user_address": USER_ADDRESS},
        headers=_auth(777),
    )
    assert resp.status == 200
    prepared = await resp.json()
    assert prepared["signature_plan"] == "combined_approve_agent"
    assert prepared["builder_name"] == "copytradeaster"
    assert prepared["can_perp_trade"] is True
    assert prepared["can_withdraw"] is False
    assert prepared["eip712"]["primaryType"] == "ApproveAgent"

    # 3. The wallet signs; the server never sees the main private key.
    typed = encode_typed_data(
        domain_data=prepared["eip712"]["domain"],
        message_types={"ApproveAgent": prepared["eip712"]["types"]["ApproveAgent"]},
        message_data=prepared["eip712"]["message"],
    )
    signature = Account.sign_message(typed, USER_WALLET.key).signature.hex()

    # 4. Submit: exchange approve, signed verify, preflight, atomic commit.
    resp = await client.post(
        "/miniapp/api/builder/submit",
        json={"pending_id": prepared["pending_id"], "signature": signature},
        headers=_auth(777),
    )
    assert resp.status == 200, await resp.text()
    result = await resp.json()
    assert result["paused"] is True
    assert result["account_id"] == "tg-777"
    assert result["position_mode"] == "ONE_WAY"
    assert result["available_usdt"] == "1000"

    # 5. The combined approve carried the builder monetization fields.
    assert len(exchange.approvals) == 1
    approval = exchange.approvals[0]
    assert approval["user"] == USER_ADDRESS
    assert approval["builder"] == BUILDER
    assert approval["maxFeeRate"] == "0.0001"
    assert approval["builderName"] == "copytradeaster"
    assert approval["canWithdraw"] == "false"
    assert approval["signatureChainId"] == "56"
    assert approval["signature"] == signature.removeprefix("0x")

    # 6. The signer key landed in the encrypted store, bound to the user.
    identity = app.credentials.identity("tg-777")
    assert identity.user.lower() == USER_ADDRESS.lower()
    assert identity.signer.lower() == approval["agentAddress"].lower()

    # 7. Dashboard over the same stack: connected and paused.
    resp = await client.post("/miniapp/api/dashboard", headers=_auth(777))
    assert resp.status == 200
    dashboard = await resp.json()
    assert dashboard["copy_state"] == "paused"
    assert dashboard["account_masked"] == result["account_masked"]

    # 8. Pause is available immediately after connect (no engine needed).
    resp = await client.post("/miniapp/api/pause", headers=_auth(777))
    assert resp.status == 200

    # 9. A second prepare for the connected user is a typed owner conflict.
    resp = await client.post(
        "/miniapp/api/builder/prepare",
        json={"user_address": other_wallet},
        headers=_auth(777),
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "owner_conflict"

    # 10. Resume without confirmation is rejected.
    resp = await client.post("/miniapp/api/resume", json={}, headers=_auth(777))
    assert resp.status == 409

    # 10b. Resume stays fail-closed without a started engine.
    prep_conf = await client.post(
        "/miniapp/api/confirmation/begin",
        json={"action": "resume"},
        headers=_auth(777),
    )
    assert prep_conf.status == 200
    conf_token = (await prep_conf.json())["token"]
    resp = await client.post(
        "/miniapp/api/resume",
        json={"confirmation": {"token": conf_token, "action": "resume"}},
        headers=_auth(777),
    )
    assert resp.status in {403, 409, 503}
    # 11. Another Telegram user is a separate identity with a clean slate.
    resp = await client.post("/miniapp/api/session", headers=_auth(778))
    assert (await resp.json())["account_connected"] is False


async def test_session_rejects_wrong_token_signature(env):
    client, _exchange, _app, _composition = env
    forged = _init_data(777).replace("hash=", "hash=deadbeef")
    resp = await client.post("/miniapp/api/session", headers={"X-Telegram-Init-Data": forged})
    assert resp.status == 401


async def test_remote_accepted_response_loss_recovers_without_second_signature(env):
    client, exchange, app, composition = env
    user_id = 779
    await app.db.execute_write(
        "INSERT INTO telegram_users(telegram_user_id, chat_id, role, notifications_enabled, "
        "access_enabled, created_at, updated_at) "
        "VALUES (?, ?, 'USER', 1, 1, datetime('now'), datetime('now'))",
        (user_id, user_id),
    )

    prep_resp = await client.post(
        "/miniapp/api/builder/prepare",
        json={"user_address": USER_ADDRESS},
        headers=_auth(user_id),
    )
    assert prep_resp.status == 200
    prepared = await prep_resp.json()

    # A PREPARED attempt may never bypass the wallet signature.
    premature = await client.post(
        "/miniapp/api/builder/resume-attempt",
        json={},
        headers=_auth(user_id),
    )
    assert premature.status == 400
    assert (await premature.json())["error"] == "approval_signature_required"

    typed = encode_typed_data(
        domain_data=prepared["eip712"]["domain"],
        message_types={"ApproveAgent": prepared["eip712"]["types"]["ApproveAgent"]},
        message_data=prepared["eip712"]["message"],
    )
    signature = Account.sign_message(typed, USER_WALLET.key).signature.hex()

    exchange.lose_next_approval_response = True
    submit = await client.post(
        "/miniapp/api/builder/submit",
        json={"pending_id": prepared["pending_id"], "signature": signature},
        headers=_auth(user_id),
    )
    assert submit.status == 400
    assert (await submit.json())["error"] == "approval_status_unknown"
    assert len(exchange.approvals) == 1

    session = await client.post("/miniapp/api/session", headers=_auth(user_id))
    assert session.status == 200
    session_body = await session.json()
    assert session_body["connection_state"] == "onboarding"
    assert session_body["onboarding_state"] == "APPROVAL_SENT"

    recovered = await client.post(
        "/miniapp/api/builder/resume-attempt",
        json={},
        headers=_auth(user_id),
    )
    assert recovered.status == 200, await recovered.text()
    result = await recovered.json()
    assert result["account_id"] == f"tg-{user_id}"
    assert result["paused"] is True
    assert len(exchange.approvals) == 1
    assert app.credentials.identity(f"tg-{user_id}").user.lower() == USER_ADDRESS.lower()


async def test_miniapp_session_recovery_required_and_recover_endpoint(env):
    client, _exchange, app, composition = env

    # Create owner and prepare
    await app.db.execute_write(
        "INSERT INTO telegram_users(telegram_user_id, chat_id, role, notifications_enabled, "
        "access_enabled, created_at, updated_at) "
        "VALUES (888, 888, 'USER', 1, 1, datetime('now'), datetime('now'))"
    )
    prep_resp = await client.post(
        "/miniapp/api/builder/prepare",
        json={"user_address": USER_ADDRESS},
        headers=_auth(888),
    )
    assert prep_resp.status == 200
    prepared = await prep_resp.json()

    typed = encode_typed_data(
        domain_data=prepared["eip712"]["domain"],
        message_types={"ApproveAgent": prepared["eip712"]["types"]["ApproveAgent"]},
        message_data=prepared["eip712"]["message"],
    )
    signature = Account.sign_message(typed, USER_WALLET.key).signature.hex()

    sub_resp = await client.post(
        "/miniapp/api/builder/submit",
        json={"pending_id": prepared["pending_id"], "signature": signature},
        headers=_auth(888),
    )
    assert sub_resp.status == 200
    # Simulate follower runtime registry unregistration (as in failed activation or process restart)
    app.follower_registry._by_id.pop("tg-888", None)
    app.follower_registry._snapshot = tuple(app.follower_registry._by_id.values())

    # Session reports recovery_required
    sess_resp = await client.post("/miniapp/api/session", headers=_auth(888))
    assert sess_resp.status == 200
    sess = await sess_resp.json()
    assert sess["account_connected"] is False

    # Call builder/recover
    rec_resp = await client.post("/miniapp/api/builder/recover", json={}, headers=_auth(888))
    assert rec_resp.status == 200
    rec = await rec_resp.json()
    assert rec["connection_state"] == "connected"
    assert rec["account_connected"] is True
    assert app.follower_registry.get("tg-888") is not None
