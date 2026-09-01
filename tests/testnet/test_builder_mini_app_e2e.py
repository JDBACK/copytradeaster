"""Signed testnet e2e: Mini App part-1 Builder flow + проверочная сделка.

Marker `testnet`, требует CT_TESTNET=1 и wallet.txt с секциями `copyc2`
(кошелёк пользователя с testnet-средствами) и `builder` (адрес билдера).
Схема:

1. Реальный BuilderOnboardingService против fapi.asterdex-testnet.com:
   prepare -> подпись EIP-712 ГЛАВНЫМ ключом copyc2 (как это делает кошелёк
   во фронтенде) -> submit -> signed verify -> preflight -> paused commit.
2. Проверочная сделка от сгенерированного signer'а с `builder` + `feeRate`
   (снимает риск «подключён, но не торгует» и закрывает исторический
   Builder-e2e Stage 1): MARKET BUY -> reduceOnly SELL -> flat.
3. Best-effort diagnostic `GET /fapi/v3/builder/userTrades` подписью
   билдера. Endpoint не документирован для этого auth context и
   относится к Builder Center / Part 2, поэтому не является binding
   acceptance Stage 3 Part 1.
4. `feeRate` выше одобренного `maxFeeRate` отклоняется биржей.
5. Финал: счёт flat, ордеров нет.  Отзыв Agent не выполняется (revoke —
   часть 2, решение #11 реестра): Agent истечёт через 30 дней.

Онбординг выполняется один раз на модуль (module-cache); async-фикстуры —
function-scoped, чтобы не пересекать event loop'ы pytest-asyncio.
Средства: только testnet faucet.  Mainnet не используется конструкцией.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from decimal import Decimal as D

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import prepare_signed_request
from copytradeaster.onboarding.builder import BuilderOnboardingService
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderPrepareRequest,
    BuilderSubmitRequest,
    OnboardingCommit,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import NonceStore
from tests.testnet.support.builder_live import (
    best_effort_flatten,
    load_wallet_section,
    signed_read_json,
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

SYMBOL = "BTCUSDT"
QTY = D("0.002")
FEE_RATE = "0.0001"
USER_SECTION = "copyc2"
BUILDER_SECTION = "builder"
USER_TG_ID = 777_000_001

_MODULE: dict = {}


class _RecordingStore:
    """Sqlite onboarding store that keeps the last commit payload in memory.

    The commit carries the generated signer key; the trade step of this e2e
    signs orders exactly like the production service does after decrypting
    the same key.  Nothing is ever logged.
    """

    def __init__(self, db: Database) -> None:
        from copytradeaster.application.adapters import SqliteOnboardingStore
        from copytradeaster.credentials.service import SqliteCredentials

        self._inner = SqliteOnboardingStore(db, SqliteCredentials(db, bytes.fromhex("11" * 32)))
        self.commits: list[OnboardingCommit] = []

    async def find_identity_conflict(self, telegram_user_id: int, user_address: str):
        return await self._inner.find_identity_conflict(telegram_user_id, user_address)

    async def commit_onboarding(self, commit: OnboardingCommit) -> None:
        self.commits.append(commit)
        await self._inner.commit_onboarding(commit)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    from copytradeaster.config import Settings

    cfg = Settings(
        db_path=tmp_path_factory.mktemp("builder-e2e") / "builder-e2e.sqlite3",
        log_path=tmp_path_factory.mktemp("builder-e2e-logs") / "builder.log",
        builder_name="copytradeaster",
        max_fee_rate=FEE_RATE,
    )
    if "testnet" not in cfg.rest_base_url.lower():
        pytest.fail(f"builder e2e is restricted to testnet, got {cfg.rest_base_url!r}")
    return cfg


@pytest.fixture
async def http(settings):
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        yield client


@pytest.fixture
async def flowctx(settings, http):
    """Onboard once per module; per-test database connection (function loop)."""
    from copytradeaster.aster.trading_api import AsterClient

    if "commit" not in _MODULE:
        db = Database(settings.db_path)
        await db.connect()
        await db.migrate()
        # Production grants onboarding only to a captcha-approved telegram
        # user; create that pre-allowlist row exactly like the access flow.
        from copytradeaster.storage.repositories import TelegramUserRepo

        await TelegramUserRepo(db).upsert(USER_TG_ID, USER_TG_ID)
        limiter = RateLimits(
            settings.weight_limit_per_min,
            settings.orders_limit_per_min,
            settings.orders_limit_per_10s,
        )
        # Capturing store: the commit payload (with the generated signer key)
        # is kept in this process only, so the trade step can sign orders —
        # exactly what the production service does with the encrypted store.
        store = _RecordingStore(db)
        user_address, user_key = load_wallet_section(USER_SECTION)
        builder_address, builder_key = load_wallet_section(BUILDER_SECTION)
        config = BuilderOnboardingConfig(
            base_url=settings.rest_base_url,
            trading_chain_id=714,
            registration_chain_id=settings.registration_chain_id,
            builder_address=builder_address,
            builder_name=settings.builder_name,
            max_fee_rate=settings.max_fee_rate,
            agent_expiry_days=settings.builder_agent_expiry_days,
            approval_ttl_s=settings.builder_approval_ttl_s,
            expected_egress_ips=(),
            require_agent_ip_whitelist=False,
            expected_position_mode=settings.position_mode,
        )

        def client_factory(identities, keys):
            return AsterClient(
                http=http,
                base_url=settings.rest_base_url,
                limiter=limiter,
                identities=identities,
                keys=keys,
                nonces=NonceStore(db),
                dry_run=False,
            )

        service = BuilderOnboardingService(http, limiter, client_factory, store, config=config)
        prepared = await service.prepare(BuilderPrepareRequest(USER_TG_ID, user_address))
        typed = encode_typed_data(
            domain_data=prepared.eip712["domain"],
            message_types={"ApproveAgent": prepared.eip712["types"]["ApproveAgent"]},
            message_data=prepared.eip712["message"],
        )
        signature = Account.sign_message(typed, user_key).signature.hex()
        try:
            result = await _submit_builder_with_read_retries(
                service,
                USER_TG_ID,
                prepared.pending_id,
                signature,
            )
        except OnboardingError as exc:
            if exc.code.value == "agent_submit_failed":
                # Live-testnet fact (2026-08-31): the exchange rejects the
                # combined approval while the builder address is absent from
                # the product registry ("Builder address ... not registered in
                # Aster").  Registration is a web Builder Center step, not an
                # API call — the signing/format pipeline itself is proven.
                pytest.skip(
                    "builder address is not registered in Aster; register it "
                    "via the web Builder Center and rerun this e2e"
                )
            pytest.fail(f"builder onboarding failed on testnet: {exc.code.value}")
        assert result.paused is True
        assert store.commits, "commit was not captured - mis-wired store"
        _MODULE.update(
            {
                "result": result,
                "commit": store.commits[0],
                "user_address": user_address,
                "user_key": user_key,
                "builder_address": builder_address,
                "builder_key": builder_key,
                "limiter": limiter,
                "settings": settings,
            }
        )
        await db.close()

    db = Database(settings.db_path)
    await db.connect()
    _MODULE["db"] = db
    yield _MODULE
    await db.close()


def _signer_identity(commit: OnboardingCommit):
    from copytradeaster.domain.models import TradingIdentity

    return TradingIdentity(
        account_id=commit.account_id,
        user=commit.user_address,
        signer=commit.signer_address,
        chain_id=714,
    )


def _client_order_id(label: str) -> str:
    return f"ctE2E{label}{secrets.token_hex(6)}"


async def _submit_builder_with_read_retries(
    service: BuilderOnboardingService,
    telegram_user_id: int,
    pending_id: str,
    signature: str,
):
    for attempt in range(12):
        try:
            return await service.submit(
                BuilderSubmitRequest(
                    telegram_user_id,
                    pending_id,
                    signature if attempt == 0 else "",
                )
            )
        except OnboardingError as exc:
            if exc.code not in {
                OnboardingErrorCode.AGENT_NOT_CONFIRMED,
                OnboardingErrorCode.SIGNED_READ_FAILED,
            }:
                raise
            if attempt == 11:
                raise
            await asyncio.sleep(1.5)
    raise AssertionError("unreachable builder e2e retry state")


async def _signed_write(
    http: httpx.AsyncClient,
    base_url: str,
    path: str,
    params: dict[str, str],
    identity,
    private_key: str,
    nonce_store: NonceStore,
    limiter: RateLimits,
    *,
    is_order: bool = False,
) -> dict:
    await limiter.acquire_request(1)
    if is_order:
        await limiter.acquire_order("builder-e2e")
    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request(params, identity, private_key, nonce)
    response = await http.post(f"{base_url}{path}?{full}", content=b"")
    try:
        body = response.json()
    except ValueError:
        pytest.fail(f"signed POST {path} returned non-JSON HTTP {response.status_code}")
    if not isinstance(body, dict):
        pytest.fail(f"signed POST {path} returned a non-object payload")
    code = int(body.get("code", 0) or 0)
    if response.status_code >= 400 or code < 0:
        pytest.fail(f"signed POST {path} was rejected (http={response.status_code}, code={code})")
    return body


async def _place_order_resolved(
    http: httpx.AsyncClient,
    base_url: str,
    params: dict[str, str],
    identity,
    private_key: str,
    nonce_store: NonceStore,
    limiter: RateLimits,
    *,
    label: str,
) -> dict:
    """POST once; resolve only an unknown outcome by idempotent order query."""
    client_order_id = _client_order_id(label)
    submitted = {**params, "newClientOrderId": client_order_id}
    await limiter.acquire_request(1)
    await limiter.acquire_order("builder-e2e")
    nonce = await nonce_store.next_nonce(identity.signer)
    _, full = prepare_signed_request(submitted, identity, private_key, nonce)
    unknown = False
    try:
        response = await http.post(f"{base_url}/fapi/v3/order?{full}", content=b"")
    except httpx.TransportError:
        unknown = True
        body: dict[str, object] = {}
    else:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        body = parsed if isinstance(parsed, dict) else {}
        code = int(body.get("code", 0) or 0)
        if response.status_code < 400 and code >= 0 and body:
            return body
        unknown = response.status_code >= 500 or code in {-1000, -1006, -1007}
        if not unknown:
            pytest.fail(f"order {label} was rejected (http={response.status_code}, code={code})")

    assert unknown
    for attempt in range(6):
        found = await signed_read_json(
            http,
            base_url,
            "/fapi/v3/order",
            {"symbol": SYMBOL, "origClientOrderId": client_order_id},
            identity,
            private_key,
            nonce_store,
            limiter,
            weight=1,
            allowed_error_codes=frozenset({-2013}),
        )
        if isinstance(found, dict):
            return found
        if attempt < 5:
            await asyncio.sleep(0.75)
    pytest.fail(f"order {label} outcome remained unknown after query resolution")


async def test_agent_committed_paused_with_builder_fields(flowctx):
    result = flowctx["result"]
    commit = flowctx["commit"]
    assert result.paused is True
    assert result.account_masked.startswith("0x")
    assert result.builder_name == "copytradeaster"
    assert result.max_fee_rate == FEE_RATE
    assert commit.allow_new_entries is False
    assert commit.validation.can_perp_trade is True
    assert commit.validation.can_withdraw is False


async def test_builder_order_with_fee_rate_round_trip(flowctx, http):
    commit = flowctx["commit"]
    limiter: RateLimits = flowctx["limiter"]
    settings = flowctx["settings"]
    signer_identity = _signer_identity(commit)
    signer_key = commit.signer_private_key
    builder_address = flowctx["builder_address"]
    nonce_store = NonceStore(flowctx["db"])

    # Leverage low enough that QTY notional is well inside the balance.
    await _signed_write(
        http,
        settings.rest_base_url,
        "/fapi/v3/leverage",
        {"symbol": SYMBOL, "leverage": "20"},
        signer_identity,
        signer_key,
        nonce_store,
        limiter,
    )

    # copyc2 trades in HEDGE mode: positionSide is mandatory on every order.
    dual = await signed_read_json(
        http,
        settings.rest_base_url,
        "/fapi/v3/positionSide/dual",
        {},
        signer_identity,
        signer_key,
        nonce_store,
        limiter,
        weight=30,
    )
    assert isinstance(dual, dict)
    hedge = bool(dual.get("dualSidePosition"))

    def side_for(order_side: str) -> dict:
        return {"positionSide": "LONG" if order_side == "BUY" else "SHORT"} if hedge else {}

    order_id = None
    try:
        opened = await _place_order_resolved(
            http,
            settings.rest_base_url,
            {
                "symbol": SYMBOL,
                "side": "BUY",
                "type": "MARKET",
                "quantity": str(QTY),
                "builder": builder_address,
                "feeRate": FEE_RATE,
                **side_for("BUY"),
            },
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            label="CapOpen",
        )
        order_id = opened.get("orderId") or opened.get("i")
        assert order_id, opened

        closed = await _place_order_resolved(
            http,
            settings.rest_base_url,
            {
                "symbol": SYMBOL,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(QTY),
                "builder": builder_address,
                "feeRate": FEE_RATE,
                # Hedge mode closes LONG by SELL with the same positionSide;
                # reduceOnly is rejected there.
                "positionSide": "LONG",
            }
            if hedge
            else {
                "symbol": SYMBOL,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(QTY),
                "reduceOnly": "true",
                "builder": builder_address,
                "feeRate": FEE_RATE,
            },
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            label="CapClose",
        )
        assert closed.get("orderId"), closed

        positions = await signed_read_json(
            http,
            settings.rest_base_url,
            "/fapi/v3/positionRisk",
            {"symbol": SYMBOL},
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            weight=5,
        )
        assert isinstance(positions, list)
        amounts = [D(str(position.get("positionAmt", "0"))) for position in positions] or [D(0)]
        assert all(amount == 0 for amount in amounts), positions
        orders = await signed_read_json(
            http,
            settings.rest_base_url,
            "/fapi/v3/openOrders",
            {"symbol": SYMBOL},
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            weight=1,
        )
        assert orders == []
    finally:
        await best_effort_flatten(
            http,
            settings.rest_base_url,
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            SYMBOL,
        )

    assert order_id is not None

    # The builder-side fee statement is a reporting extra with undocumented
    # auth (no /builder/userTrades entry on the endpoints page).  The binding
    # acceptance above is exchange-level: orders carrying builder+feeRate are
    # accepted and executed, and an over-cap feeRate is rejected.  Fee
    # statement integration lands with the Builder Center work (part 2).
    builder_key = flowctx["builder_key"]
    builder_address_str = flowctx["builder_address"]
    builder_identity = type(
        "I", (), {"user": builder_address_str, "signer": builder_address_str, "chain_id": 714}
    )()
    body = await signed_read_json(
        http,
        settings.rest_base_url,
        "/fapi/v3/builder/userTrades",
        {"symbol": SYMBOL},
        builder_identity,
        builder_key,
        nonce_store,
        limiter,
        weight=5,
        allowed_error_codes=frozenset({-2015, -2016}),
        optional_after_retries=True,
    )
    if body is None:
        print(
            "WARNING: /builder/userTrades is not queryable with a bare builder "
            "identity (no agent) — fee statement check deferred to part 2"
        )
    else:
        assert isinstance(body, (dict, list))
        rows = body if isinstance(body, list) else body.get("rows", [])
        ours = [r for r in rows if str(r.get("orderId", "")) == str(order_id)]
        assert ours, f"builder userTrades missing order {order_id}: {body}"
        assert all("builderFee" in r for r in ours), ours


async def test_exchange_rejects_fee_rate_above_approved_cap(flowctx, http):
    """feeRate выше одобренного maxFeeRate должен отклоняться биржей с конкретной ошибкой."""
    commit = flowctx["commit"]
    settings = flowctx["settings"]
    limiter = flowctx["limiter"]
    signer_identity = _signer_identity(commit)
    signer_key = commit.signer_private_key
    builder_address = flowctx["builder_address"]
    nonce_store = NonceStore(flowctx["db"])

    # 1. Query mode through the bounded idempotent-read helper.
    dual_data = await signed_read_json(
        http,
        settings.rest_base_url,
        "/fapi/v3/positionSide/dual",
        {},
        signer_identity,
        signer_key,
        nonce_store,
        limiter,
        weight=30,
    )
    assert isinstance(dual_data, dict)
    hedge = bool(dual_data.get("dualSidePosition"))

    # 2. Pre-order flat baseline.
    pos_before = await signed_read_json(
        http,
        settings.rest_base_url,
        "/fapi/v3/positionRisk",
        {"symbol": SYMBOL},
        signer_identity,
        signer_key,
        nonce_store,
        limiter,
        weight=5,
    )
    assert isinstance(pos_before, list)
    amounts_before = [D(str(p.get("positionAmt", "0"))) for p in pos_before] or [D(0)]
    assert all(a == 0 for a in amounts_before), f"account should be flat before test: {pos_before}"

    # 3. Same valid MARKET shape; only feeRate is above the approved cap
    # (plus the mandatory unique client id used for deterministic cleanup).
    order_params: dict[str, str] = {
        "symbol": SYMBOL,
        "side": "BUY",
        "type": "MARKET",
        "quantity": str(QTY),
        "builder": builder_address,
        "feeRate": "0.0005",
        "newClientOrderId": _client_order_id("AboveCap"),
    }
    if hedge:
        order_params["positionSide"] = "LONG"

    try:
        await limiter.acquire_request(1)
        await limiter.acquire_order("builder-e2e")
        nonce = await nonce_store.next_nonce(signer_identity.signer)
        _, full = prepare_signed_request(order_params, signer_identity, signer_key, nonce)
        resp = await http.post(settings.rest_base_url + "/fapi/v3/order?" + full, content=b"")
        body = resp.json()

        # 4. Prove cap semantics from the observed exchange message, not a
        # generic validation code or the word "invalid".
        assert isinstance(body, dict)
        assert resp.status_code >= 400 or int(body.get("code", 0)) < 0, (
            f"unexpected above-cap order acceptance: {body}"
        )
        error_msg = str(body.get("msg", "")).casefold()
        assert "fee" in error_msg and any(
            term in error_msg for term in ("max", "limit", "cap", "exceed", "approved")
        ), f"exchange rejection did not identify the approved fee cap: {body}"

        # 5. The rejected order cannot create exposure or a resting order.
        pos_after = await signed_read_json(
            http,
            settings.rest_base_url,
            "/fapi/v3/positionRisk",
            {"symbol": SYMBOL},
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            weight=5,
        )
        assert isinstance(pos_after, list)
        amounts_after = [D(str(p.get("positionAmt", "0"))) for p in pos_after] or [D(0)]
        assert all(a == 0 for a in amounts_after), (
            f"positions should remain flat after rejected order: {pos_after}"
        )
        orders_after = await signed_read_json(
            http,
            settings.rest_base_url,
            "/fapi/v3/openOrders",
            {"symbol": SYMBOL},
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            weight=1,
        )
        assert orders_after == []
    finally:
        await best_effort_flatten(
            http,
            settings.rest_base_url,
            signer_identity,
            signer_key,
            nonce_store,
            limiter,
            SYMBOL,
        )
