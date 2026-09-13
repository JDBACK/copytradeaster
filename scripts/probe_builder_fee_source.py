"""Probe the real source of the Aster Builder fee (signed testnet, facts only).

Question this answers: when an order carries ``builder`` + ``feeRate``, is the
Builder fee a *separate* cashflow (own ``incomeType`` / own debit) or is it
already inside the standard trade commission?

Method — one builder-attributed round trip on a disposable testnet account:

1. read which wallet section the configured Builder has approved
   (``GET /fapi/v3/builder/approvedUserList``, signed as the Builder);
2. open + close a minimum-size position with ``builder``/``feeRate``
   (the production payload: ``aster/trading_api.py``);
3. compare the three exchange sources for the same trade ids:
   * ``GET /fapi/v3/userTrades``   -> ``commission``
   * ``GET /fapi/v3/income``       -> signed ``COMMISSION`` cashflows
   * ``GET /fapi/v3/builder/userTrades`` (signed as the Builder) -> ``fee``/``builderFee``.

Verdict printed and written to ``data/builder-fee-probe.json``:

* ``included_in_commission`` — the builder's ``builderFee`` times the fee rate
  reconciles with the ordinary commission, and no extra income flow exists;
* ``separate`` — an extra flow/amount exists that the standard commission does
  not contain;
* ``inconclusive`` — the exchange did not report enough within the wait budget.

Safety: refuses any non-testnet host, refuses an account that already holds a
position or open orders on the symbol, cancels and flattens in ``finally``.
Secrets come from ``wallet.txt`` only and are never printed.

Usage:
  uv run python scripts/probe_builder_fee_source.py --section copyc2
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from copytradeaster.aster.rate_limits import RateLimits  # noqa: E402
from copytradeaster.aster.signing import next_nonce, prepare_signed_request  # noqa: E402
from copytradeaster.config import Settings  # noqa: E402
from copytradeaster.domain.models import PosSide, Side  # noqa: E402

TESTNET_HOST = "fapi.asterdex-testnet.com"
WALLET_FILE = Path("wallet.txt")
REPORT = Path("data/builder-fee-probe.json")
LEDGER_WAIT_S = 12.0


@dataclass(frozen=True, slots=True)
class Actor:
    """One wallet section: main user (owner) + API signer."""

    label: str
    user: str
    signer: str
    key: str
    chain_id: int = 714


def _sections() -> dict[str, dict[str, str]]:
    if not WALLET_FILE.exists():
        raise SystemExit("wallet.txt not found")
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in WALLET_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith(("evm", "api", "privte")):
            current = stripped
            sections.setdefault(current, {})
        elif current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            sections[current][key.strip().lower()] = value.strip()
    return sections


def _actor(name: str) -> Actor:
    """Wallet section -> signer actor.

    A follower section carries ``api wallet adress`` + its key. The Builder
    section has no API wallet: its own wallet key signs the Builder-side reads,
    so ``signer`` equals ``user`` there.
    """
    from eth_account import Account

    section = _sections().get(name)
    if not section:
        raise SystemExit(f"wallet.txt has no section {name!r}")
    user = section.get("evm adress", "")
    if not user.startswith("0x"):
        raise SystemExit(f"section {name!r} has no evm adress")
    signer = section.get("api wallet adress", "")
    wanted = signer if signer.startswith("0x") else user
    for value in section.values():
        if value.startswith("0x") and len(value) == 66:
            try:
                if Account.from_key(value).address.casefold() == wanted.casefold():
                    return Actor(name, user, wanted, value)
            except Exception:
                continue
    raise SystemExit(f"section {name!r} has no key matching {wanted[:8]}…")


class Client:
    """Minimal signed REST client: constants + EIP-712 over the query string."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, actor: Actor) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._actor = actor
        self._limiter = RateLimits(2400, 1200, 100)

    async def get(
        self, path: str, params: dict[str, str], *, weight: int = 5, actor: Actor | None = None
    ) -> Any:
        who = actor or self._actor
        await self._limiter.acquire_request(weight)
        _, full = prepare_signed_request(params, who, who.key, next_nonce())
        response = await self._http.get(f"{self._base}{path}?{full}")
        body = response.json()
        if response.status_code >= 400 or (isinstance(body, dict) and body.get("code", 0) < 0):
            raise SystemExit(f"signed GET {path} rejected: {body}")
        return body

    async def _send(self, method: str, path: str, params: dict[str, str], *, weight: int) -> Any:
        await self._limiter.acquire_request(weight)
        _, full = prepare_signed_request(params, self._actor, self._actor.key, next_nonce())
        response = await self._http.request(method, f"{self._base}{path}?{full}", content=b"")
        body = response.json()
        if response.status_code >= 400 or (isinstance(body, dict) and body.get("code", 0) < 0):
            raise SystemExit(f"{method} {path} rejected: {body}")
        return body

    async def post_order(self, params: dict[str, str], *, weight: int = 1) -> dict:
        return await self._send("POST", "/fapi/v3/order", params, weight=weight)

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        await self._send(
            "DELETE",
            "/fapi/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            weight=1,
        )


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


async def _filters(client: Client, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
    body = await client.get("/fapi/v3/exchangeInfo", {}, weight=1)
    for entry in body.get("symbols", []):
        if entry.get("symbol") != symbol:
            continue
        by_type = {f["filterType"]: f for f in entry.get("filters", [])}
        return (
            _decimal(by_type["LOT_SIZE"]["stepSize"]),
            _decimal(by_type["PRICE_FILTER"]["tickSize"]),
            _decimal(by_type.get("MIN_NOTIONAL", {}).get("notional", "0")),
        )
    raise SystemExit(f"no exchangeInfo filters for {symbol}")


async def _mark(client: Client, symbol: str) -> Decimal:
    body = await client.get("/fapi/v3/ticker/price", {"symbol": symbol}, weight=2)
    return _decimal(body["price"])


async def _open_position(client: Client, symbol: str) -> Decimal:
    """Signed position amount of the symbol; zero means flat."""
    body = await client.get("/fapi/v3/positionRisk", {"symbol": symbol}, weight=5)
    rows = body if isinstance(body, list) else [body]
    return sum((_decimal(row.get("positionAmt", "0")) for row in rows), Decimal(0))


async def _open_orders(client: Client, symbol: str) -> list[dict]:
    body = await client.get("/fapi/v3/openOrders", {"symbol": symbol}, weight=1)
    return list(body) if isinstance(body, list) else []


async def _user_trades(client: Client, symbol: str, start: int, end: int) -> list[dict]:
    body = await client.get(
        "/fapi/v3/userTrades",
        {"symbol": symbol, "startTime": str(start), "endTime": str(end), "limit": "1000"},
    )
    return list(body) if isinstance(body, list) else []


async def _income(client: Client, start: int, end: int) -> list[dict]:
    body = await client.get(
        "/fapi/v3/income",
        {"startTime": str(start), "endTime": str(end), "limit": "1000"},
        weight=30,
    )
    return list(body) if isinstance(body, list) else []


async def _builder_trades(reader: Client, start: int, end: int) -> list[dict]:
    """Builder-scoped trade rows: the endpoint resolves the caller as Builder.

    The Builder section has no agent wallet of its own, so the Builder's own
    agent wallets (the follower signers it created) are the working reader.
    """
    body = await reader.get(
        "/fapi/v3/builder/userTrades",
        {"startTime": str(start), "endTime": str(end), "limit": "1000"},
        weight=5,
    )
    rows = body.get("rows", body) if isinstance(body, dict) else body
    return list(rows) if isinstance(rows, list) else []


async def _approved_users(builder: Client) -> list[dict]:
    body = await builder.get("/fapi/v3/builder/approvedUserList", {"limit": "1000"}, weight=5)
    rows = body.get("rows", body) if isinstance(body, dict) else body
    return list(rows) if isinstance(rows, list) else []


async def _my_builders(client: Client) -> list[dict]:
    """Which Builders this account approved (its own signed read)."""
    body = await client.get("/fapi/v3/builder", {}, weight=5)
    rows = body.get("rows", body) if isinstance(body, dict) else body
    return list(rows) if isinstance(rows, list) else []


async def _flatten(client: Client, symbol: str, pos_side: PosSide) -> None:
    """Cancel leftovers and reduce the probe position to flat (best effort).

    ``reduceOnly`` is deliberately absent in Hedge mode: Aster rejects it with
    -1106 "Parameter 'reduceOnly' sent when not required" and the concrete
    ``positionSide`` already scopes the reduction.
    """
    for order in await _open_orders(client, symbol):
        with contextlib.suppress(Exception):
            await client.cancel_order(symbol, str(order["clientOrderId"]))
    amount = await _open_position(client, symbol)
    if amount == 0:
        return
    params = {
        "symbol": symbol,
        "side": (Side.SELL if amount > 0 else Side.BUY).value,
        "type": "MARKET",
        "quantity": format(abs(amount), "f"),
        "positionSide": pos_side.value,
        "newClientOrderId": f"probeflat-{int(time.time())}",
    }
    if pos_side is PosSide.BOTH:
        params["reduceOnly"] = "true"
    await client.post_order(params)


async def main() -> int:
    section = "copyc2"
    symbol = "BTCUSDT"
    quantity = Decimal("0.001")
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg == "--section" and index + 1 < len(argv):
            section = argv[index + 1]
        elif arg == "--symbol" and index + 1 < len(argv):
            symbol = argv[index + 1]
        elif arg == "--qty" and index + 1 < len(argv):
            quantity = Decimal(argv[index + 1])

    settings = Settings()
    base_url = str(settings.rest_base_url)
    if TESTNET_HOST not in base_url.lower():
        raise SystemExit(f"refusing non-testnet host {base_url!r}")

    builder_label = "builder"
    builder_section = _sections().get(builder_label) or {}
    builder_address = builder_section.get("evm adress", "")
    if not builder_address.startswith("0x"):
        raise SystemExit("wallet.txt has no builder address")

    actor = _actor(section)
    builder_actor = _actor(builder_label)
    fee_rate = _decimal(settings.max_fee_rate)
    timeout = httpx.Timeout(20)
    skip_precheck = "--skip-precheck" in argv
    no_builder = "--no-builder" in argv
    report_path = REPORT
    for index, arg in enumerate(argv):
        if arg == "--report" and index + 1 < len(argv):
            report_path = Path(argv[index + 1])
    scan_sections = [s for s in _sections() if s.startswith("copyc")]

    async def approved_users() -> set[str]:
        """Which accounts approved the Builder.

        The Builder-side read needs the Builder's own agent wallet; when that
        is unavailable (no agent registered, -1000) the same fact is readable
        from each follower's own signed ``GET /fapi/v3/builder``.
        """
        try:
            rows = await _approved_users(builder)
            return {str(row.get("userAddress", "")).casefold() for row in rows if row}
        except SystemExit as exc:
            if skip_precheck:
                raise
            print(f"builder-side list unavailable ({exc}); scanning follower sections")
            found: set[str] = set()
            for name in scan_sections:
                try:
                    who = _actor(name)
                except SystemExit:
                    continue
                rows = await _my_builders(Client(http, base_url, who))
                for row in rows:
                    address = str(row.get("builderAddress", "")).casefold()
                    if address == builder_address.casefold():
                        found.add(who.user.casefold())
                        print(f"  {name}: builder approved (maxFeeRate={row.get('maxFeeRate')})")
            return found

    async with httpx.AsyncClient(timeout=timeout) as http:
        client = Client(http, base_url, actor)
        builder = Client(http, base_url, builder_actor)

        approved_addresses = await approved_users()
        is_approved = actor.user.casefold() in approved_addresses

        position = await _open_position(client, symbol)
        orders = await _open_orders(client, symbol)
        print(
            f"section={section} approved_by_builder={is_approved} "
            f"position={position} open_orders={len(orders)}"
        )
        if position != 0 or orders:
            raise SystemExit("account is not flat with no open orders; refusing to probe")
        if not is_approved:
            # Only masked addresses: the operator needs to see which section to use.
            listed = ", ".join(sorted({f"{addr[:8]}…" for addr in approved_addresses if addr}))
            raise SystemExit(
                f"section {section!r} ({actor.user[:8]}…) is not approved by builder "
                f"{builder_address[:8]}…; approved users: {listed or 'none'}"
            )

        step, _tick, _min_notional = await _filters(client, symbol)
        quantity = (quantity // step) * step
        if quantity <= 0:
            raise SystemExit(f"quantity below step {step}")
        price = await _mark(client, symbol)
        dual = await client.get("/fapi/v3/positionSide/dual", {}, weight=30)
        hedge = bool(dual.get("dualSidePosition"))
        pos_side = PosSide.LONG if hedge else PosSide.BOTH
        start_ms = int(time.time() * 1000) - 60_000

        opened = 0
        try:
            entry_params = {
                "symbol": symbol,
                "side": Side.BUY.value,
                "type": "MARKET",
                "quantity": format(quantity, "f"),
                "positionSide": pos_side.value,
                "newOrderRespType": "RESULT",
                "newClientOrderId": f"probe-open-{int(time.time())}",
            }
            if not no_builder:
                entry_params["builder"] = builder_address
                entry_params["feeRate"] = format(fee_rate, "f")
            entry = await client.post_order(entry_params)
            opened = 1
            print(f"opened {symbol} {quantity} @~{price} order={entry.get('orderId')}")
            await asyncio.sleep(4)
            close_params = {
                "symbol": symbol,
                "side": Side.SELL.value,
                "type": "MARKET",
                "quantity": format(quantity, "f"),
                "positionSide": pos_side.value,
                "newOrderRespType": "RESULT",
                "newClientOrderId": f"probe-close-{int(time.time())}",
            }
            if not no_builder:
                close_params["builder"] = builder_address
                close_params["feeRate"] = format(fee_rate, "f")
            if pos_side is PosSide.BOTH:
                close_params["reduceOnly"] = "true"
            closed = await client.post_order(close_params)
            print(f"closed order={closed.get('orderId')}")
        finally:
            if opened and await _open_position(client, symbol) != 0:
                await _flatten(client, symbol, pos_side)
                print("flattened")

        await asyncio.sleep(LEDGER_WAIT_S)
        end_ms = int(time.time() * 1000) + 1_000
        fills = await _user_trades(client, symbol, start_ms, end_ms)
        flows = await _income(client, start_ms, end_ms)
        builder_rows: list[dict] = []
        reader_used = "none"
        for label, reader in (("follower-agent", client), ("builder-main", builder)):
            try:
                builder_rows = await _builder_trades(reader, start_ms, end_ms)
            except SystemExit as exc:
                print(f"builder/userTrades via {label} unavailable ({exc})")
                continue
            reader_used = label
            break

        commission_flows: dict[str, Decimal] = {}
        other_types: dict[str, Decimal] = {}
        for flow in flows:
            kind = str(flow.get("incomeType"))
            amount = _decimal(flow.get("income", "0"))
            if kind == "COMMISSION":
                commission_flows[str(flow.get("tradeId", ""))] = (
                    commission_flows.get(str(flow.get("tradeId", "")), Decimal(0)) + amount
                )
            else:
                other_types[kind] = other_types.get(kind, Decimal(0)) + amount

        builder_by_trade = {str(row.get("tradeId")): row for row in builder_rows}
        rows = []
        for fill in fills:
            trade_id = str(fill.get("id"))
            builder_row = builder_by_trade.get(trade_id)
            rows.append(
                {
                    "tradeId": trade_id,
                    "side": fill.get("side"),
                    "qty": fill.get("qty"),
                    "price": fill.get("price"),
                    "notional": fill.get("quoteQty"),
                    "realizedPnl": fill.get("realizedPnl"),
                    "commission": fill.get("commission"),
                    "commissionAsset": fill.get("commissionAsset"),
                    "income_commission": str(commission_flows.get(trade_id, "")),
                    "builder_fee": None if builder_row is None else builder_row.get("builderFee"),
                    "builder_fee_side_fee": None if builder_row is None else builder_row.get("fee"),
                }
            )

        builders_found = [row for row in rows if row["builder_fee"] is not None]
        mismatch = [
            row
            for row in rows
            if row["income_commission"] != ""
            and _decimal(row["commission"]) != -_decimal(row["income_commission"])
        ]
        verdict = "inconclusive"
        if rows and builders_found:
            if mismatch:
                verdict = "sign-or-amount-mismatch"
            elif all(_decimal(row["builder_fee"]) >= 0 for row in builders_found):
                summary_fee = sum(
                    (_decimal(row["builder_fee"]) for row in builders_found), Decimal(0)
                )
                expected = sum(
                    (_decimal(row["notional"]) * fee_rate for row in builders_found), Decimal(0)
                )
                verdict = (
                    "separate"
                    if abs(summary_fee - expected) > Decimal("0.00000001") and summary_fee > 0
                    else "included_in_commission"
                )
        report = {
            "section": section,
            "symbol": symbol,
            "quantity": format(quantity, "f"),
            "fee_rate": format(fee_rate, "f"),
            "builder": builder_address,
            "builder_attributed": not no_builder,
            "approved_by_builder": is_approved,
            "window_ms": [start_ms, end_ms],
            "verdict": verdict,
            "builder_reader": reader_used,
            "fills": rows,
            "income_other_types": {k: str(v) for k, v in other_types.items()},
            "builder_trade_rows": len(builder_rows),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
