"""Aster V3 futures REST adapter (trading context).

Query-mode everywhere: parameters travel in the signed query string, request
body stays empty (matches the official example). Dry-run never sends HTTP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import ValidationError

from copytradeaster.aster.models import (
    AgentDTO,
    BalanceDTO,
    BuilderDTO,
    ExchangeInfo,
    IncomeDTO,
    LeverageBracketResponse,
    OrderDTO,
    PositionDTO,
    UserTradeDTO,
)
from copytradeaster.aster.ports import IdentityProvider, KeyProvider, NonceSource
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import prepare_signed_request
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.logging_utils import get_logger

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
    working_type: str | None = None  # STOP/TP market and limit flavors
    time_in_force: str | None = None  # plain LIMIT validity (GTC/IOC/FOK/GTX/GTD)
    builder: str | None = None
    fee_rate: Decimal | None = None

    def __post_init__(self) -> None:
        has_builder = self.builder is not None
        has_fee = self.fee_rate is not None
        if has_builder != has_fee:
            raise ValueError("builder and feeRate must be set together")
        if self.time_in_force is not None:
            tif = self.time_in_force.strip().upper()
            if tif not in {"GTC", "IOC", "FOK", "GTX", "GTD"}:
                raise ValueError("invalid timeInForce")
            object.__setattr__(self, "time_in_force", tif)
        if not has_builder and not has_fee:
            return
        if not isinstance(self.builder, str) or not self.builder.strip():
            raise ValueError("builder address is required")
        text = self.builder.strip()
        if len(text) != 42 or not text.startswith("0x"):
            raise ValueError("invalid builder address")
        try:
            int(text[2:], 16)
        except ValueError:
            raise ValueError("invalid builder address") from None
        object.__setattr__(self, "builder", text)
        raw = self.fee_rate
        if isinstance(raw, (bool, float)):
            raise TypeError("fee_rate float/bool is forbidden, use Decimal/str/int")
        try:
            fee = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, TypeError, AttributeError):
            raise ValueError("invalid feeRate") from None
        if not fee.is_finite():
            raise ValueError("invalid feeRate")
        if fee < Decimal(0):
            raise ValueError("invalid feeRate")
        if fee > Decimal("0.001"):
            raise ValueError("feeRate above Futures cap 0.001")
        object.__setattr__(self, "fee_rate", fee)

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
            "STOP",
            "TAKE_PROFIT_MARKET",
            "TAKE_PROFIT",
        ):
            params["workingType"] = self.working_type
        if self.time_in_force is not None:
            params["timeInForce"] = self.time_in_force
        if self.builder is not None and self.fee_rate is not None:
            params["builder"] = self.builder
            params["feeRate"] = format(self.fee_rate, "f")
        return params


def _wrap_transport_error(exc: Exception) -> AsterError:
    """httpx transport failures -> AsterError with UNKNOWN-classified codes.

    The executor treats -1006/-1007 as outcome-unknown (order may or may not
    exist on the exchange), so timeouts must surface here, not as raw httpx
    exceptions that no caller catches.
    """
    if isinstance(exc, httpx.TimeoutException):
        return AsterError(-1007, f"transport timeout: {exc}", None)
    return AsterError(-1006, f"transport error: {exc}", None)


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
        try:
            resp = await self._http.request(method, url, content=b"")
        except httpx.TransportError as exc:
            raise _wrap_transport_error(exc) from exc
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
        nonce = await self._nonces.next_nonce(identity.signer)
        _, full = prepare_signed_request(params, identity, key, nonce)
        url = self._base_url + path + "?" + full
        if self._dry_run and method != "GET":
            # The signed URL contains user/signer/nonce/signature query
            # material.  Keep the action trace, never the URL or parameters.
            log.info(
                "DRY-RUN signed request method=%s path=%s account_id=%s",
                method,
                path,
                account_id,
            )
            return {}
        try:
            resp = await self._http.request(method, url, content=b"")
        except httpx.TransportError as exc:
            raise _wrap_transport_error(exc) from exc
        if resp.status_code >= 400:
            raise self._parse_error(resp.status_code, _safe_json(resp))
        body = _safe_json(resp)
        if isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0:
            raise self._parse_error(resp.status_code, body)
        # Do not log ``qs``/``full``: even without the final signature the
        # signed query carries wallet identity and nonce fields.
        log.debug("signed request method=%s path=%s account_id=%s", method, path, account_id)
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
        try:
            body = await self._signed(
                account_id, "GET", "/fapi/v3/openOrders", params, weight=weight
            )
        except AsterError as exc:
            # Aster Futures Testnet intermittently returns -1000 from
            # openOrders while the same signed identity/nonce succeeds on
            # balance, positionRisk and allOrders.  allOrders is an
            # authoritative documented substitute only for a concrete symbol;
            # never turn an unavailable all-symbol snapshot into an empty list.
            if symbol is None or exc.code not in UNKNOWN_CODES:
                raise
            log.warning(
                "openOrders unavailable; using symbol-scoped allOrders account_id=%s symbol=%s",
                account_id,
                symbol,
            )
            history = await self.all_orders(account_id, symbol, limit=1000)
            return [order for order in history if order.status in {"NEW", "PARTIALLY_FILLED"}]
        return [OrderDTO.model_validate(x) for x in body]

    async def all_orders(
        self, account_id: str, symbol: str, *, limit: int = 1000
    ) -> list[OrderDTO]:
        """Return symbol-scoped active/canceled/filled order history.

        Aster requires ``symbol`` and caps ``limit`` at 1000.  Recovery uses
        this endpoint because the current-order view alone is not proof that a
        response-lost MARKET never executed.
        """
        if not isinstance(symbol, str) or not symbol.strip():
            raise AsterError(-1000, "symbol is required for allOrders")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("allOrders limit must be in [1, 1000]")
        body = await self._signed(
            account_id,
            "GET",
            "/fapi/v3/allOrders",
            {"symbol": symbol.strip(), "limit": str(limit)},
            weight=5,
        )
        if not isinstance(body, list):
            raise AsterError(-1000, "invalid allOrders response")
        try:
            return [OrderDTO.model_validate(row) for row in body]
        except Exception:
            raise AsterError(-1000, "invalid allOrders response") from None

    async def query_order_history(
        self,
        account_id: str,
        symbol: str,
        *,
        orig_client_order_id: str,
    ) -> OrderDTO | None:
        """Find a recent order by COID in authoritative symbol history."""
        orders = await self.all_orders(account_id, symbol, limit=1000)
        matches = [order for order in orders if order.client_order_id == orig_client_order_id]
        if not matches:
            if len(orders) >= 1000:
                # A full page cannot prove absence: the target order may have
                # fallen outside the returned window on a busy account.
                raise AsterError(-1000, "allOrders recovery window saturated")
            return None
        return max(matches, key=lambda order: order.update_time)

    async def agent(self, account_id: str) -> AgentDTO:
        """Return the Agent record belonging to the configured signer.

        ``GET /fapi/v3/agent`` is a signed USER_DATA request and returns a
        collection in the documented API.  Some deployments wrap that
        collection in ``data``/``agents`` while a single-record response is
        also seen in compatibility responses; accept those wire shapes but
        expose one typed record to callers.  A record for another signer is
        never returned.
        """
        body = await self._signed(account_id, "GET", "/fapi/v3/agent", {}, weight=5)
        if isinstance(body, list):
            records = body
        elif isinstance(body, dict):
            nested = body.get("data")
            if nested is None:
                nested = body.get("agents", body.get("agent"))
            if isinstance(nested, list):
                records = nested
            elif isinstance(nested, dict):
                records = [nested]
            else:
                records = [body]
        else:
            raise AsterError(-1000, "invalid agent response")

        expected_signer = self._identities.identity(account_id).signer.lower()
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                dto = AgentDTO.model_validate(record)
            except Exception:
                raise AsterError(-1000, "invalid agent response") from None
            if dto.agent_address and dto.agent_address.strip().lower() == expected_signer:
                return dto
        raise AsterError(-2015, "agent not found")

    async def builder(self, account_id: str) -> list[BuilderDTO]:
        """Return the typed approved-Builder records for the account identity.

        ``GET /fapi/v3/builder`` is a signed USER_DATA request returning a
        collection.  Accept the documented list shape plus ``data``/``rows``/
        ``builders``/``builder`` wrappers seen across deployments, but expose
        only typed records — never raw dicts.  Selection of the record that
        proves the current Builder authorization belongs to the attestation
        validator, not to transport: every well-formed record is returned.
        A malformed critical record fails the whole read closed.
        """
        body = await self._signed(account_id, "GET", "/fapi/v3/builder", {}, weight=5)
        if isinstance(body, list):
            records = body
        elif isinstance(body, dict):
            nested: object = body.get("data")
            if nested is None:
                nested = body.get("rows", body.get("builders", body.get("builder")))
            if isinstance(nested, list):
                records = nested
            elif isinstance(nested, dict):
                records = [nested]
            else:
                records = [body]
        else:
            raise AsterError(-1000, "invalid builder response")

        result: list[BuilderDTO] = []
        for record in records:
            if not isinstance(record, dict):
                raise AsterError(-1000, "invalid builder response")
            try:
                result.append(BuilderDTO.model_validate(record))
            except Exception:
                raise AsterError(-1000, "invalid builder response") from None
        return result

    async def leverage_bracket(self, account_id: str, symbol: str) -> LeverageBracketResponse:
        """Read and normalize the symbol-specific leverage brackets.

        The documented shape is an object, while the connected Aster testnet
        currently returns a one-element list for the same symbol query.  Both
        wire shapes are accepted, but the returned value is always typed and
        tied to the requested symbol.
        """
        if not isinstance(symbol, str) or not symbol.strip():
            raise AsterError(-1000, "symbol is required for leverage bracket")
        requested = symbol.strip()
        body = await self._signed(
            account_id,
            "GET",
            "/fapi/v3/leverageBracket",
            {"symbol": requested},
            weight=1,
        )
        records: list[dict]
        if isinstance(body, dict):
            records = [body]
        elif isinstance(body, list):
            if not all(isinstance(item, dict) for item in body):
                raise AsterError(-1000, "invalid leverage bracket response")
            records = body
        else:
            raise AsterError(-1000, "invalid leverage bracket response")
        if not records:
            raise AsterError(-1000, "empty leverage bracket response")
        try:
            parsed = [LeverageBracketResponse.model_validate(record) for record in records]
        except Exception:
            raise AsterError(-1000, "invalid leverage bracket response") from None
        for result in parsed:
            if result.symbol == requested:
                return result
        raise AsterError(-1000, "leverage bracket symbol mismatch")

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

    async def user_trades(
        self,
        account_id: str,
        symbol: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[UserTradeDTO]:
        """One page only. A full page never proves complete history."""
        params = _history_params(start_time, end_time, limit)
        if not symbol:
            raise ValueError("history symbol required")
        params["symbol"] = symbol
        if from_id is not None:
            if start_time is not None or end_time is not None:
                raise ValueError("fromId cannot be combined with time bounds")
            if type(from_id) is not int or from_id < 0:
                raise ValueError("invalid fromId")
            params["fromId"] = str(from_id)
        body = await self._signed(account_id, "GET", "/fapi/v3/userTrades", params, weight=5)
        try:
            if not isinstance(body, list):
                raise ValueError("expected list")
            rows = [UserTradeDTO.model_validate(row) for row in body]
            if any(row.symbol != symbol for row in rows):
                raise ValueError("symbol mismatch")
            return rows
        except (ValidationError, ValueError):
            raise AsterError(-1000, "invalid user trades response") from None

    async def income_history(
        self,
        account_id: str,
        *,
        start_time: int,
        end_time: int,
        symbol: str | None = None,
        income_type: str | None = None,
        limit: int = 1000,
    ) -> list[IncomeDTO]:
        """Bounded inclusive window; saturated windows require subdivision."""
        params = _history_params(start_time, end_time, limit)
        if symbol is not None:
            params["symbol"] = symbol
        if income_type is not None:
            params["incomeType"] = income_type
        body = await self._signed(account_id, "GET", "/fapi/v3/income", params, weight=30)
        try:
            if not isinstance(body, list):
                raise ValueError("expected list")
            rows = [IncomeDTO.model_validate(row) for row in body]
            if any(
                (symbol is not None and row.symbol != symbol)
                or (income_type is not None and row.income_type != income_type)
                for row in rows
            ):
                raise ValueError("history filter mismatch")
            return rows
        except (ValidationError, ValueError):
            raise AsterError(-1000, "invalid income history response") from None

    # --- trading writes ----------------------------------------------------------

    async def place_order(self, account_id: str, req: PlaceOrderRequest) -> OrderDTO:
        if self._dry_run:
            log.info(
                "DRY-RUN place_order account_id=%s symbol=%s side=%s type=%s",
                account_id,
                req.symbol,
                req.side.value,
                req.type,
            )
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


def _history_params(start: int | None, end: int | None, limit: int) -> dict[str, str]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("history limit must be between 1 and 1000")
    params = {"limit": str(limit)}
    for name, value in (("startTime", start), ("endTime", end)):
        if value is not None:
            if type(value) is not int or value < 0:
                raise ValueError("invalid history time")
            params[name] = str(value)
    if start is not None and end is not None and not 0 <= end - start <= 7 * 86400000:
        raise ValueError("history window must be ordered and at most seven days")
    return params


def _safe_json(resp: httpx.Response) -> dict | list:
    try:
        body = resp.json()
    except ValueError:
        raise AsterError(
            0, f"non-json response (http {resp.status_code}): {resp.text[:200]}", resp.status_code
        ) from None
    return body
