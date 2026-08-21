"""Aster V3 futures REST adapter (trading context).

Query-mode everywhere: parameters travel in the signed query string, request
body stays empty (matches the official example). Dry-run never sends HTTP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from copytradeaster.aster.models import BalanceDTO, ExchangeInfo, OrderDTO, PositionDTO
from copytradeaster.aster.ports import IdentityProvider, KeyProvider, NonceSource
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import prepare_signed_request
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.logging_utils import get_logger, redact

log = get_logger(__name__)

UNKNOWN_CODES = {-1000, -1006, -1007}


class AsterError(Exception):
    def __init__(self, code: int, msg: str, http_status: int | None = None) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


class RateLimitError(AsterError):
    """429/418 - IP/account rate limited."""


@dataclass(frozen=True)
class PlaceOrderRequest:
    symbol: str
    side: Side
    type: str
    quantity: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    close_position: bool = False
    position_side: PosSide = PosSide.BOTH
    new_client_order_id: str = ""
    new_order_resp_type: str = "ACK"
    working_type: str | None = None  # only for STOP_MARKET / TAKE_PROFIT_MARKET

    def to_params(self) -> dict[str, str]:
        params: dict[str, str] = {
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.type,
        }
        if self.quantity is not None:
            params["quantity"] = format(self.quantity, "f")
        if self.price is not None:
            params["price"] = format(self.price, "f")
        if self.stop_price is not None:
            params["stopPrice"] = format(self.stop_price, "f")
        if self.reduce_only:
            params["reduceOnly"] = "true"
        if self.close_position:
            params["closePosition"] = "true"
        if self.position_side != PosSide.BOTH:
            params["positionSide"] = self.position_side.value
        if self.new_client_order_id:
            params["newClientOrderId"] = self.new_client_order_id
        params["newOrderRespType"] = self.new_order_resp_type
        if self.working_type is not None and self.type in (
            "STOP_MARKET",
            "TAKE_PROFIT_MARKET",
        ):
            params["workingType"] = self.working_type
        return params


class AsterClient:
    EXCHANGE_INFO_TTL_S = 3600.0

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        limiter: RateLimits,
        identities: IdentityProvider,
        keys: KeyProvider,
        nonces: NonceSource,
        dry_run: bool = False,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter
        self._identities = identities
        self._keys = keys
        self._nonces = nonces
        self._dry_run = dry_run
        self._exchange_info: ExchangeInfo | None = None
        self._exchange_info_at = 0.0

    # --- low-level -----------------------------------------------------------

    def _parse_error(self, status: int | None, body: dict) -> AsterError:
        code = int(body.get("code", 0) or 0)
        msg = str(body.get("msg", ""))
        if status in (429, 418):
            return RateLimitError(code or status or 0, msg or "rate limited", status)
        return AsterError(code, msg, status)

    async def _request(
        self, method: str, path: str, params: dict[str, str], weight: int
    ) -> dict | list:
        url = self._base_url + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode(list(params.items()))
        await self._limiter.acquire_request(weight)
        if self._dry_run:
            log.info("DRY-RUN %s %s", method, url)
            return {}
        resp = await self._http.request(method, url, content=b"")
        if resp.status_code >= 400:
            raise self._parse_error(resp.status_code, _safe_json(resp))
        body = _safe_json(resp)
        if (
            isinstance(body, dict)
            and "code" in body
            and isinstance(body.get("code"), int)
            and body["code"] < 0
        ):
            raise self._parse_error(resp.status_code, body)
        return body

    async def _signed(
        self,
        account_id: str,
        method: str,
        path: str,
        params: dict[str, str],
        weight: int,
        is_order: bool = False,
    ) -> dict | list:
        await self._limiter.acquire_request(weight)
        if is_order:
            await self._limiter.acquire_order(account_id)
        identity = self._identities.identity(account_id)
        key = self._keys.private_key(account_id)
        nonce = self._nonces.next_nonce(identity.signer)
        qs, full = prepare_signed_request(params, identity, key, nonce)
        url = self._base_url + path + "?" + full
        if self._dry_run:
            masked = url.split("&signature=")[0] + "&signature=" + redact("0x" + "0" * 32)
            log.info("DRY-RUN %s %s", method, masked)
            return {}
        resp = await self._http.request(method, url, content=b"")
        if resp.status_code >= 400:
            raise self._parse_error(resp.status_code, _safe_json(resp))
        body = _safe_json(resp)
        if isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0:
            raise self._parse_error(resp.status_code, body)
        log.debug("%s %s?%s&signature=%s", method, path, qs, redact("s" * 64))
        return body

    # --- public (unsigned) ----------------------------------------------------

    async def server_time(self) -> int:
        body = await self._request("GET", "/fapi/v3/time", {}, weight=1)
        return int(body["serverTime"])

    async def exchange_info(self, force: bool = False) -> ExchangeInfo:
        now = time.monotonic()
        if (
            force
            or self._exchange_info is None
            or now - self._exchange_info_at > self.EXCHANGE_INFO_TTL_S
        ):
            body = await self._request("GET", "/fapi/v3/exchangeInfo", {}, weight=1)
            self._exchange_info = ExchangeInfo.model_validate(body)
            self._exchange_info_at = now
        return self._exchange_info

    # --- account reads ---------------------------------------------------------

    async def balance(self, account_id: str) -> list[BalanceDTO]:
        body = await self._signed(account_id, "GET", "/fapi/v3/balance", {}, weight=5)
        return [BalanceDTO.model_validate(x) for x in body]

    async def position_risk(self, account_id: str, symbol: str | None = None) -> list[PositionDTO]:
        params = {"symbol": symbol} if symbol else {}
        body = await self._signed(account_id, "GET", "/fapi/v3/positionRisk", params, weight=5)
        return [PositionDTO.model_validate(x) for x in body]

    async def open_orders(self, account_id: str, symbol: str | None = None) -> list[OrderDTO]:
        params = {"symbol": symbol} if symbol else {}
        weight = 1 if symbol else 40
        body = await self._signed(account_id, "GET", "/fapi/v3/openOrders", params, weight=weight)
        return [OrderDTO.model_validate(x) for x in body]

    async def query_order(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> OrderDTO | None:
        params: dict[str, str] = {"symbol": symbol}
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        elif order_id is not None:
            params["orderId"] = str(order_id)
        try:
            body = await self._signed(account_id, "GET", "/fapi/v3/order", params, weight=1)
        except AsterError as exc:
            if exc.code == -2013:
                return None
            raise
        return OrderDTO.model_validate(body)

    # --- trading writes ----------------------------------------------------------

    async def place_order(self, account_id: str, req: PlaceOrderRequest) -> OrderDTO:
        if self._dry_run:
            log.info("DRY-RUN place_order %s", req.to_params())
            return OrderDTO(
                orderId=0,
                clientOrderId=req.new_client_order_id,
                symbol=req.symbol,
                side=req.side,
                type=req.type,
                status="DRY_RUN",
                origQty=req.quantity if req.quantity is not None else Decimal(0),
                executedQty=Decimal(0),
                avgPrice=Decimal(0),
                price=req.price if req.price is not None else Decimal(0),
                stopPrice=req.stop_price if req.stop_price is not None else Decimal(0),
                reduceOnly=req.reduce_only,
                closePosition=req.close_position,
                positionSide=req.position_side,
                workingType=req.working_type or "MARK_PRICE",
                timeInForce="GTC",
                updateTime=0,
            )
        body = await self._signed(
            account_id, "POST", "/fapi/v3/order", req.to_params(), weight=1, is_order=True
        )
        return OrderDTO.model_validate(body)

    async def cancel_order(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> OrderDTO:
        if self._dry_run:
            return OrderDTO(
                orderId=0,
                clientOrderId=orig_client_order_id or "",
                symbol=symbol,
                side=Side.BUY,
                type="CANCELED_DRY_RUN",
                status="DRY_RUN",
                origQty=Decimal(0),
                executedQty=Decimal(0),
                avgPrice=Decimal(0),
                price=Decimal(0),
                stopPrice=Decimal(0),
                reduceOnly=False,
                closePosition=False,
                positionSide=PosSide.BOTH,
                workingType="MARK_PRICE",
                timeInForce="GTC",
                updateTime=0,
            )
        params: dict[str, str] = {"symbol": symbol}
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        elif order_id is not None:
            params["orderId"] = str(order_id)
        body = await self._signed(
            account_id, "DELETE", "/fapi/v3/order", params, weight=1, is_order=True
        )
        return OrderDTO.model_validate(body)

    async def set_leverage(self, account_id: str, symbol: str, leverage: int) -> None:
        await self._signed(
            account_id,
            "POST",
            "/fapi/v3/leverage",
            {"symbol": symbol, "leverage": str(leverage)},
            weight=1,
        )

    async def set_margin_type(self, account_id: str, symbol: str, margin_type: str) -> None:
        try:
            await self._signed(
                account_id,
                "POST",
                "/fapi/v3/marginType",
                {"symbol": symbol, "marginType": margin_type},
                weight=1,
            )
        except AsterError as exc:
            if exc.code in (-4046, -4045):  # "no need to change"
                return
            raise

    async def get_position_mode(self, account_id: str) -> bool:
        body = await self._signed(account_id, "GET", "/fapi/v3/positionSide/dual", {}, weight=30)
        return bool(body["dualSidePosition"])

    async def set_position_mode(self, account_id: str, dual: bool) -> None:
        await self._signed(
            account_id,
            "POST",
            "/fapi/v3/positionSide/dual",
            {"dualSidePosition": "true" if dual else "false"},
            weight=1,
        )

    # --- user-data stream listenKey ---------------------------------------------

    async def start_listen_key(self, account_id: str) -> str:
        body = await self._signed(account_id, "POST", "/fapi/v3/listenKey", {}, weight=1)
        return str(body["listenKey"])

    async def keepalive_listen_key(self, account_id: str) -> None:
        await self._signed(account_id, "PUT", "/fapi/v3/listenKey", {}, weight=1)

    async def close_listen_key(self, account_id: str) -> None:
        await self._signed(account_id, "DELETE", "/fapi/v3/listenKey", {}, weight=1)


def _safe_json(resp: httpx.Response) -> dict | list:
    try:
        body = resp.json()
    except ValueError:
        raise AsterError(
            0, f"non-json response (http {resp.status_code}): {resp.text[:200]}", resp.status_code
        ) from None
    return body
