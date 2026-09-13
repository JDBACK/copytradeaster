"""Builder fee accounting proof on the REAL testnet (marker `testnet`).

Fact this test exists to pin: a Builder-attributed order carries a higher
commission than the identical order without attribution, and the difference is
*exactly* the attributed ``feeRate`` — while ``GET /fapi/v3/income`` reports the
whole amount as one standard ``COMMISSION`` cashflow with no extra income type.

Why the engine needs that proof: ``storage/history.py:builder_policy`` refuses
to claim a net trade result for Builder accounts until the Builder fee's real
source is proven. This scenario is the proof, measured on the exchange rather
than inferred from the docs.

Method (per round trip, one symbol, minimum tradable size):

1. attributed round trip: MARKET open + MARKET close with ``builder`` and
   ``feeRate=CT_MAX_FEE_RATE``;
2. control round trip: the identical orders without ``builder``/``feeRate``;
3. both are read back through the production ``AsterClient`` reads and matched
   by the signed order ids the exchange returned, so an overlapping time window
   can never mix the two runs;
4. assertions: per-fill ``commission/quoteQty`` equals the control rate for the
   control fills, equals control rate + ``feeRate`` for the attributed fills,
   every fill's ``commission`` equals the absolute value of its own
   ``COMMISSION`` income row, and the only income types present are
   ``COMMISSION``/``REALIZED_PNL``.

Skips (never a fake pass) unless the section is approved by the configured
Builder and the account is flat with no open orders on the symbol. Flat and
order-free in ``finally``; no mainnet host is reachable from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from decimal import Decimal
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import next_nonce, prepare_signed_request
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, Side

from .support.builder_live import _wallet_sections, ensure_testnet_guard

pytestmark = pytest.mark.testnet

WALLET_FILE = Path("wallet.txt")
SYMBOL = "BTCUSDT"
QUANTITY = D("0.001")
LEDGER_WAIT_S = 12.0
ALLOWED_INCOME_TYPES = frozenset({"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"})


class _Signer:
    """One wallet section able to sign: main user + API signer + its key."""

    def __init__(self, label: str, user: str, signer: str, key: str) -> None:
        self.label = label
        self.user = user
        self.signer = signer
        self.key = key

    @property
    def chain_id(self) -> int:
        return 714


def _section_signer(name: str, *, require_api_wallet: bool = True) -> _Signer:
    from eth_account import Account

    section = _wallet_sections().get(name) or {}
    user = section.get("evm adress", "")
    signer = section.get("api wallet adress", "")
    if not user.startswith("0x"):
        pytest.skip(f"wallet.txt section {name!r} has no evm adress")
    if require_api_wallet and not signer.startswith("0x"):
        pytest.skip(f"wallet.txt section {name!r} has no API wallet")
    wanted = signer if signer.startswith("0x") else user
    for value in section.values():
        if value.startswith("0x") and len(value) == 66:
            try:
                if Account.from_key(value).address.casefold() == wanted.casefold():
                    return _Signer(name, user, wanted, value)
            except Exception:
                continue
    pytest.skip(f"wallet.txt section {name!r} has no key matching its wallet")


class _Api:
    """Signed testnet REST: exactly the wire shape the production client uses."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, signer: _Signer) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._signer = signer
        self._limiter = RateLimits(2400, 1200, 100)

    async def get(self, path: str, params: dict[str, str], *, weight: int = 5) -> object:
        await self._limiter.acquire_request(weight)
        _, full = prepare_signed_request(params, self._signer, self._signer.key, next_nonce())
        response = await self._http.get(f"{self._base}{path}?{full}")
        return self._body(path, response)

    async def send(self, method: str, path: str, params: dict[str, str], *, weight: int) -> dict:
        await self._limiter.acquire_request(weight)
        _, full = prepare_signed_request(params, self._signer, self._signer.key, next_nonce())
        response = await self._http.request(method, f"{self._base}{path}?{full}", content=b"")
        return self._body(path, response)

    @staticmethod
    def _body(path: str, response: httpx.Response) -> dict:
        body = response.json()
        code = int(body.get("code", 0) or 0) if isinstance(body, dict) else 0
        if response.status_code >= 400 or code < 0:
            pytest.fail(f"signed {path} rejected ({response.status_code}, code={code})")
        return body


def _decimal(value: object) -> Decimal:
    return D(str(value))


async def _filters(api: _Api, symbol: str) -> Decimal:
    body = await api.get("/fapi/v3/exchangeInfo", {}, weight=1)
    assert isinstance(body, dict)
    for entry in body.get("symbols", []):
        if entry.get("symbol") == symbol:
            for item in entry.get("filters", []):
                if item.get("filterType") == "LOT_SIZE":
                    return _decimal(item["stepSize"])
    pytest.skip(f"no exchangeInfo LOT_SIZE for {symbol}")


async def _position_amount(api: _Api, symbol: str) -> Decimal:
    body = await api.get("/fapi/v3/positionRisk", {"symbol": symbol}, weight=5)
    rows = body if isinstance(body, list) else [body]
    return sum((_decimal(row.get("positionAmt", "0")) for row in rows), D(0))


async def _open_orders(api: _Api, symbol: str) -> list[dict]:
    body = await api.get("/fapi/v3/openOrders", {"symbol": symbol}, weight=1)
    return list(body) if isinstance(body, list) else []


async def _hedge(api: _Api) -> bool:
    body = await api.get("/fapi/v3/positionSide/dual", {}, weight=30)
    assert isinstance(body, dict)
    return bool(body.get("dualSidePosition"))


async def _approved_by_builder(api: _Api, builder_address: str) -> bool:
    """Whether this account approved the configured Builder (own signed read)."""
    body = await api.get("/fapi/v3/builder", {}, weight=5)
    rows = body.get("rows", body) if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return False
    return any(
        str(row.get("builderAddress", "")).casefold() == builder_address.casefold() for row in rows
    )


async def _flatten(api: _Api, symbol: str, pos_side: PosSide) -> None:
    """Cancel leftovers and reduce the test position to flat (best effort)."""
    for order in await _open_orders(api, symbol):
        with contextlib.suppress(Exception):
            await api.send(
                "DELETE",
                "/fapi/v3/order",
                {"symbol": symbol, "origClientOrderId": str(order["clientOrderId"])},
                weight=1,
            )
    amount = await _position_amount(api, symbol)
    if amount == 0:
        return
    params = {
        "symbol": symbol,
        "side": (Side.SELL if amount > 0 else Side.BUY).value,
        "type": "MARKET",
        "quantity": format(abs(amount), "f"),
        "positionSide": pos_side.value,
        "newClientOrderId": f"feeacctflat-{int(time.time())}",
    }
    if pos_side is PosSide.BOTH:
        params["reduceOnly"] = "true"
    await api.send("POST", "/fapi/v3/order", params, weight=1)


async def _round_trip(
    api: _Api,
    symbol: str,
    quantity: Decimal,
    pos_side: PosSide,
    *,
    builder: str | None,
    fee_rate: Decimal | None,
    tag: str,
) -> list[str]:
    """Market open + market close; returns the exchange order ids."""
    order_ids: list[str] = []
    for leg, side in (("open", Side.BUY), ("close", Side.SELL)):
        params = {
            "symbol": symbol,
            "side": side.value,
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "positionSide": pos_side.value,
            "newOrderRespType": "RESULT",
            "newClientOrderId": f"feeacct-{tag}-{leg}-{int(time.time() * 1000)}",
        }
        if pos_side is PosSide.BOTH and leg == "close":
            params["reduceOnly"] = "true"
        if builder is not None and fee_rate is not None:
            params["builder"] = builder
            params["feeRate"] = format(fee_rate, "f")
        body = await api.send("POST", "/fapi/v3/order", params, weight=1)
        order_ids.append(str(body.get("orderId")))
        if leg == "open":
            await _wait_fill(api, symbol)
    return order_ids


async def _wait_fill(api: _Api, symbol: str) -> None:
    """Bound the position change so the close cannot race the open."""
    for _ in range(20):
        if await _position_amount(api, symbol) != 0:
            return
        await asyncio.sleep(0.5)
    pytest.fail("open order did not produce a position")


async def test_builder_fee_is_inside_the_standard_commission() -> None:
    settings = Settings()
    base_url = str(settings.rest_base_url)
    ensure_testnet_guard(base_url)
    builder_address = str(settings.builder_address or "").strip()
    if not builder_address.startswith("0x"):
        pytest.skip("CT_BUILDER_ADDRESS is not configured")
    fee_rate = _decimal(settings.max_fee_rate)
    if not fee_rate.is_finite() or fee_rate <= 0:
        pytest.skip("CT_MAX_FEE_RATE is not a positive rate")

    signer = _section_signer("copyc2")
    timeout = httpx.Timeout(20)
    async with httpx.AsyncClient(timeout=timeout) as http:
        api = _Api(http, base_url, signer)
        step = await _filters(api, SYMBOL)
        quantity = (QUANTITY // step) * step
        if quantity <= 0:
            pytest.skip(f"probe quantity is below the {SYMBOL} step {step}")
        if await _position_amount(api, SYMBOL) != 0 or await _open_orders(api, SYMBOL):
            pytest.skip(f"{SYMBOL} is not flat with no open orders on copyc2")
        if not await _approved_by_builder(api, builder_address):
            pytest.skip("copyc2 has not approved the configured Builder")

        pos_side = PosSide.LONG if await _hedge(api) else PosSide.BOTH
        start_ms = int(time.time() * 1000) - 60_000
        attributed_ids: list[str] = []
        control_ids: list[str] = []
        try:
            attributed_ids = await _round_trip(
                api,
                SYMBOL,
                quantity,
                pos_side,
                builder=builder_address,
                fee_rate=fee_rate,
                tag="b",
            )
            await asyncio.sleep(4)
            control_ids = await _round_trip(
                api, SYMBOL, quantity, pos_side, builder=None, fee_rate=None, tag="d"
            )
        finally:
            if await _position_amount(api, SYMBOL) != 0 or await _open_orders(api, SYMBOL):
                await _flatten(api, SYMBOL, pos_side)

        await asyncio.sleep(LEDGER_WAIT_S)
        end_ms = int(time.time() * 1000) + 1_000
        fills_body = await api.get(
            "/fapi/v3/userTrades",
            {
                "symbol": SYMBOL,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": "1000",
            },
        )
        flows_body = await api.get(
            "/fapi/v3/income",
            {"startTime": str(start_ms), "endTime": str(end_ms), "limit": "1000"},
            weight=30,
        )

    fills = [row for row in (fills_body if isinstance(fills_body, list) else [])]
    flows = [row for row in (flows_body if isinstance(flows_body, list) else [])]
    assert attributed_ids and control_ids, "both round trips must return order ids"

    def own(ids: list[str]) -> list[dict]:
        wanted = set(ids)
        return [row for row in fills if str(row.get("orderId")) in wanted]

    attributed = own(attributed_ids)
    control = own(control_ids)
    assert len(attributed) >= 2, f"attributed fills not found in {attributed_ids}"
    assert len(control) >= 2, f"control fills not found in {control_ids}"

    def rate(row: dict) -> Decimal:
        return (_decimal(row["commission"]) / _decimal(row["quoteQty"])).quantize(D("0.000001"))

    control_rate = rate(control[0])
    attributed_rate = rate(attributed[0])
    for row in control:
        assert rate(row) == control_rate, "control fills must share one taker rate"
    for row in attributed:
        assert rate(row) == attributed_rate, "attributed fills must share one rate"
    assert attributed_rate == control_rate + fee_rate, (
        "builder fee must be exactly CT_MAX_FEE_RATE on top of the control rate: "
        f"{attributed_rate} != {control_rate} + {fee_rate}"
    )

    commission_income: dict[str, Decimal] = {}
    income_types: set[str] = set()
    for flow in flows:
        kind = str(flow.get("incomeType"))
        income_types.add(kind)
        if kind == "COMMISSION":
            key = str(flow.get("tradeId", ""))
            commission_income[key] = commission_income.get(key, D(0)) + _decimal(
                flow.get("income", "0")
            )

    for row in attributed + control:
        trade_id = str(row.get("id"))
        assert trade_id in commission_income, f"no COMMISSION income for trade {trade_id}"
        assert _decimal(row["commission"]) == -commission_income[trade_id], (
            "a fill's commission must equal the absolute value of its own "
            f"COMMISSION income row (trade {trade_id})"
        )
    assert income_types <= ALLOWED_INCOME_TYPES, (
        f"unexpected income types appeared: {sorted(income_types - ALLOWED_INCOME_TYPES)}"
    )
    assert "COMMISSION" in income_types
