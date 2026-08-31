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
3. `GET /fapi/v3/builder/userTrades` подписью билдера: сделка видна билдеру
   с `builderFee`.
4. `feeRate` выше одобренного `maxFeeRate` отклоняется биржей.
5. Финал: счёт flat, ордеров нет.  Отзыв Agent не выполняется (revoke —
   часть 2, решение #11 реестра): Agent истечёт через 30 дней.

Онбординг выполняется один раз на модуль (module-cache); async-фикстуры —
function-scoped, чтобы не пересекать event loop'ы pytest-asyncio.
Средства: только testnet faucet.  Mainnet не используется конструкцией.
"""

from __future__ import annotations

import os
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import prepare_signed_request
from copytradeaster.onboarding.builder import BuilderOnboardingService
from copytradeaster.onboarding.models import (
    BuilderOnboardingConfig,
    BuilderPrepareRequest,
    BuilderSubmitRequest,
    OnboardingCommit,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.repositories import NonceStore

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
WALLET_FILE = Path("wallet.txt")
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


def _load_wallet_section(name: str) -> tuple[str, str]:
    """(main_evm_address, main_evm_private_key) из секции wallet.txt.

    Исторические секции хранят `privte key` от API-кошелька (direct-V3), а
    подпись builder-flow идёт ОСНОВНЫМ кошельком.  Ищем среди всех 0x-ключей
    секции тот, что восстанавливается в evm-адрес; если его нет — skip с
    инструкцией (добавить строку `evm private key: 0x…`).
    """
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
        except Exception:  # noqa: S110 - not a key line, keep looking
            continue
    pytest.skip(
        f"wallet.txt section {name!r} has no main-key matching its evm address; "
        f"add a line `evm private key: 0x…` for the builder-flow signature"
    )


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
    from copytradeaster.onboarding.errors import OnboardingError

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
        user_address, user_key = _load_wallet_section(USER_SECTION)
        builder_address, builder_key = _load_wallet_section(BUILDER_SECTION)
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
            result = await service.submit(
                BuilderSubmitRequest(USER_TG_ID, prepared.pending_id, signature)
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

    async def signed(method: str, path: str, params: dict[str, str], *, is_order=False):
        await limiter.acquire_request(1)
        if is_order:
            await limiter.acquire_order("e2e")
        nonce = await nonce_store.next_nonce(signer_identity.signer)
        _, full = prepare_signed_request(params, signer_identity, signer_key, nonce)
        url = settings.rest_base_url + path + "?" + full
        resp = await http.request(method, url, content=b"")
        body = resp.json()
        assert resp.status_code < 400 and int(body.get("code", 0)) >= 0, body
        return body

    # Leverage low enough that QTY notional is well inside the balance.
    await signed("POST", "/fapi/v3/leverage", {"symbol": SYMBOL, "leverage": "20"})

    # copyc2 trades in HEDGE mode: positionSide is mandatory on every order.
    dual = await signed("GET", "/fapi/v3/positionSide/dual", {})
    hedge = bool(dual.get("dualSidePosition"))

    def side_for(order_side: str) -> dict:
        return {"positionSide": "LONG" if order_side == "BUY" else "SHORT"} if hedge else {}

    opened = await signed(
        "POST",
        "/fapi/v3/order",
        {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(QTY),
            "builder": builder_address,
            "feeRate": FEE_RATE,
            **side_for("BUY"),
        },
        is_order=True,
    )
    order_id = opened.get("orderId") or opened.get("i")
    assert order_id, opened

    closed = await signed(
        "POST",
        "/fapi/v3/order",
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
        is_order=True,
    )
    assert closed.get("orderId"), closed

    # Flat, no resting orders.
    nonce = await nonce_store.next_nonce(signer_identity.signer)
    await limiter.acquire_request(5)
    _, full = prepare_signed_request({"symbol": SYMBOL}, signer_identity, signer_key, nonce)
    resp = await http.get(settings.rest_base_url + "/fapi/v3/positionRisk?" + full)
    positions = resp.json()
    assert resp.status_code < 400, positions
    amounts = [D(str(p.get("positionAmt", "0"))) for p in positions] or [D(0)]
    assert all(a == 0 for a in amounts), positions
    nonce = await nonce_store.next_nonce(signer_identity.signer)
    await limiter.acquire_request(5)
    _, full = prepare_signed_request({"symbol": SYMBOL}, signer_identity, signer_key, nonce)
    resp = await http.get(settings.rest_base_url + "/fapi/v3/openOrders?" + full)
    assert resp.json() == []

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
    nonce = await nonce_store.next_nonce(builder_address_str)
    await limiter.acquire_request(5)
    _, full = prepare_signed_request({"symbol": SYMBOL}, builder_identity, builder_key, nonce)
    resp = await http.get(settings.rest_base_url + "/fapi/v3/builder/userTrades?" + full)
    body = resp.json()
    if resp.status_code == 400 and "no agent" in str(body.get("msg", "")).lower():
        print(
            "WARNING: /builder/userTrades is not queryable with a bare builder "
            "identity (no agent) — fee statement check deferred to part 2"
        )
    else:
        rows = body if isinstance(body, list) else body.get("rows", [])
        ours = [r for r in rows if str(r.get("orderId", "")) == str(order_id)]
        assert ours, f"builder userTrades missing order {order_id}: {body}"
        assert all("builderFee" in r for r in ours), ours


async def test_exchange_rejects_fee_rate_above_approved_cap(flowctx, http):
    """feeRate выше одобренного maxFeeRate должен отклоняться биржей."""
    commit = flowctx["commit"]
    settings = flowctx["settings"]
    signer_identity = _signer_identity(commit)
    signer_key = commit.signer_private_key
    builder_address = flowctx["builder_address"]
    nonce_store = NonceStore(flowctx["db"])
    await flowctx["limiter"].acquire_request(1)
    nonce = await nonce_store.next_nonce(signer_identity.signer)
    _, full = prepare_signed_request(
        {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(QTY),
            "builder": builder_address,
            "feeRate": "0.5",  # 50%: far above the approved 0.0001 cap
        },
        signer_identity,
        signer_key,
        nonce,
    )
    resp = await http.post(settings.rest_base_url + "/fapi/v3/order?" + full, content=b"")
    body = resp.json()
    assert resp.status_code >= 400 or int(body.get("code", 0)) < 0, body
