"""Signed §10.9 acceptance on a dedicated testnet follower (marker `testnet`).

Lifecycle on copyc2/BTCUSDT through the PRODUCTION order path
(Executor + execution_commands journal), then import through the
production sync/capture path, then proof on live data:

1. §10.9.1: all lifecycle fills arrive via Executor journal CONFIRMED rows;
   re-import duplicates nothing; a REAL restart (close + reopen the same
   history file, fresh objects) republishes the identical ref;
2. §10.9.2: exactly one closed episode holds every size change, late fills
   included; corrections are aliases, never second cards;
3. §10.9.3/4: episode gross equals the signed exchange SELL realizedPnl;
   exchange COMMISSION income single-counts the fill commissions; the net
   gap (if any) names its concrete proof issue;
4. §10.9.5/6: trades list, trade detail, trade events, activity feed and
   the recovery notification share one trade_ref with identical facts —
   verified over authenticated HTTP (HMAC initData, throwaway token);
5. SL lifecycle: a distant STOP is placed and cancelled through the
   journal; the protection observation folds into history;
6. Controlled UNKNOWN: a post-send transport timeout forces the
   query-before-retry path; exactly one exchange order exists per key and
   the command ends CONFIRMED;
7. flat at start (no open orders, else skip) and flat at the end, closed
   per leg with reduceOnly failsafes.

Skips (never a fake pass) unless copyc2 is flat with no open orders on
BTCUSDT and enough testnet USDT. Scope honesty: credentials are read from
the production file connect-only (never migrated here); the shared nonce
counter ADVANCES by design (reuse is rejected with -4226); canonical
history, journal and outbox live in tmp storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from copytradeaster.application.models import TradeUnavailableError
from copytradeaster.application.service import UserApplicationService
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.config import Settings
from copytradeaster.credentials.service import SqliteCredentials
from copytradeaster.domain.models import (
    ActionKind,
    ExecutionAction,
    ExecutionPlan,
    PosSide,
    RiskPolicy,
    Side,
)
from copytradeaster.history.accounting import AccountingProof, account_episode
from copytradeaster.history.capture import HistoryCapture
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import HistoryReader, Window
from copytradeaster.history.sync import HistorySynchronizer
from copytradeaster.miniapp.server import INIT_DATA_HEADER, create_miniapp_app
from copytradeaster.notifications.router import NotificationRouter
from copytradeaster.storage.database import Database
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.events import EventRepository
from copytradeaster.storage.history import HistoryRepository, HistoryScope
from copytradeaster.storage.repositories import (
    AccountRepo,
    CommandRepo,
    NonceStore,
    OrderLinkRepo,
    SettingsRepo,
    TelegramUserRepo,
)
from copytradeaster.trading.executor import Executor

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(os.environ.get("CT_TESTNET") != "1", reason="requires CT_TESTNET=1"),
]

ACCOUNT = "copyc2"
SYMBOL = "BTCUSDT"
_FILLS_SINCE = "SELECT * FROM follower_fills WHERE account_id=? AND symbol=? AND executed_at>=?"
QTY = D("0.001")
USER_ID = 7001
POLL_TIMEOUT_S = 90.0
HTTP_TOKEN = "123456:TEST-TOKEN"


class _LiveRuntime:
    """Owned snapshots for the service layer over live-imported history."""

    def __init__(self, db: Database):
        self.db = db
        self.testnet = True
        self.snapshot: dict | None = {"positions": {}, "available_usdt": "10000"}
        self.snapshot_at = datetime.now(UTC)

    async def cached_snapshot(self, account_id: str):
        del account_id
        return (self.snapshot, self.snapshot_at) if self.snapshot else None

    async def refresh_snapshot(self, account_id: str):
        return await self.cached_snapshot(account_id)

    async def account_version(self, account_id: str) -> str:
        del account_id
        return "live"

    async def symbol_assets(self, account_id: str, symbols: list[str]):
        del account_id
        return {symbol: "BTC" if symbol == "BTCUSDT" else None for symbol in symbols}


def _leg_qtys(positions) -> dict[str, D]:
    out: dict[str, D] = {}
    for row in positions:
        out[str(row.position_side)] = D(str(row.position_amt))
    return out


async def _wait_flat(client: AsterClient, timeout_s: float = POLL_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        qtys = _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL))
        if all(qty == 0 for qty in qtys.values()):
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"position did not settle flat: {qtys}")
        await asyncio.sleep(2.0)


async def _wait_qty(client: AsterClient, expected: D, timeout_s: float = POLL_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        qtys = _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL))
        total = sum((abs(qty) for qty in qtys.values()), D("0"))
        if total == expected:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"position did not settle at {expected}: {qtys}")
        await asyncio.sleep(2.0)


def _close_mode_extras(*, hedge: bool, leg: str | None = None) -> dict:
    """Mirror planner close-mode: hedge -> positionSide, no reduceOnly."""
    if hedge:
        return {"positionSide": leg} if leg else {}
    return {"reduceOnly": "true"}


def _signed_init_data(token: str, user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF" + "0" * 10,
        "user": f'{{"id":{user_id},"first_name":"T"}}',
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


class _RecordingClient:
    """Delegate everything; record real order ids; optionally fail post-send."""

    def __init__(self, inner: AsterClient):
        self._inner = inner
        self.order_ids: list[int] = []
        self.fail_post_send_once = False

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def place_order(self, account_id: str, req):
        order = await self._inner.place_order(account_id, req)
        self.order_ids.append(int(order.order_id))
        if self.fail_post_send_once:
            self.fail_post_send_once = False
            raise TimeoutError("injected post-send transport loss")
        return order


@pytest.fixture
async def live(tmp_path: Path):
    settings = Settings()
    if "testnet" not in settings.rest_base_url.lower():
        pytest.fail("signed tests are restricted to testnet")
    # Credentials are read connect-only (never migrated here). The shared
    # nonce counter advances by design: the exchange rejects reuse (-4226)
    # while a later bot restart safely continues the same counter.
    creds_db = Database(settings.db_path)
    await creds_db.connect()
    history_path = tmp_path / "history.sqlite3"
    history_db = Database(history_path)
    await history_db.connect()
    await history_db.migrate()
    try:
        creds = SqliteCredentials(creds_db, settings.encryption_key_bytes())
        await creds.load()
        if ACCOUNT not in creds.account_ids():
            pytest.skip(f"{ACCOUNT} credentials are not imported")
        http = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        limiter = RateLimits(
            settings.weight_limit_per_min,
            settings.orders_limit_per_min,
            settings.orders_limit_per_10s,
        )
        client = AsterClient(
            http, settings.rest_base_url, limiter, creds, creds, NonceStore(creds_db)
        )
        try:
            yield settings, client, history_db, history_path
        finally:
            await http.aclose()
    finally:
        await history_db.close()
        await creds_db.close()


def _market_action(
    kind: ActionKind, side: Side, qty: D, coid: str, pside: PosSide
) -> ExecutionAction:
    return ExecutionAction(
        kind=kind,
        symbol=SYMBOL,
        payload={
            "symbol": SYMBOL,
            "side": side.value,
            "type": "MARKET",
            "quantity": format(qty, "f"),
            "positionSide": pside.value,
            "newOrderRespType": "RESULT",
        },
        client_order_id=coid,
        replaces_client_order_id=None,
        priority=1,
    )


async def test_s16_lifecycle_open_increase_partial_close(live):
    settings, client, history_db, history_path = live
    del settings
    hedge = await client.get_position_mode(ACCOUNT)
    pside = PosSide.LONG if hedge else PosSide.BOTH
    if any(qty != 0 for qty in _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL)).values()):
        pytest.skip(f"{ACCOUNT} is not flat on {SYMBOL}")
    if await client.open_orders(ACCOUNT, SYMBOL):
        pytest.skip(f"{ACCOUNT} has open orders on {SYMBOL}: possibly managed elsewhere")
    probe_now = int(time.time() * 1000)
    recent = await client.user_trades(
        ACCOUNT, SYMBOL, start_time=probe_now - 300_000, end_time=probe_now
    )
    if recent:
        pytest.skip(f"{ACCOUNT} traded in the last 5 minutes: possibly managed elsewhere")
    balances = await client.balance(ACCOUNT)
    usdt = next((D(str(b.available_balance)) for b in balances if b.asset == "USDT"), D("0"))
    if usdt < D("100"):
        pytest.skip(f"{ACCOUNT} testnet faucet is too low ({usdt} USDT free)")

    tag = f"s16-{int(time.time())}"
    t0 = int(time.time() * 1000) - 120_000
    await AccountRepo(history_db).upsert(ACCOUNT, "follower", ACCOUNT, "0x" + "2" * 40)
    commands, links = CommandRepo(history_db), OrderLinkRepo(history_db)
    scope = HistoryScope(ACCOUNT, "testnet")
    history = HistoryRepository(history_db)

    async def observe(account_id: str, key: str, kind: str, order) -> None:
        observed = getattr(order, "update_time", None)
        await history.observe_order(
            HistoryScope(account_id, "testnet"),
            order,
            kind=kind,
            command_key=key,
            observed_at=int(observed) if observed is not None else int(time.time() * 1000),
        )

    recorder = _RecordingClient(client)
    executor = Executor(
        lambda account_id: client,
        commands,
        links,
        {},
        order_observer=observe,
    )

    async def submit(kind: ActionKind, side: Side, qty: D, coid: str):
        # NOTE: the journal key covers (account, operation, kind, revision,
        # payload) — NOT action.client_order_id. Two identical MARKET payloads
        # share one key by design (planner dedupe), so every lifecycle step
        # needs its own operation_id, or the second submit is a no-op that
        # still reports the first step's CONFIRMED.
        results = await executor.submit(
            ExecutionPlan(
                account_id=ACCOUNT,
                trade_id=None,
                actions=[_market_action(kind, side, qty, coid, pside)],
                operation_id=coid,
            ),
            client=recorder,
        )
        (result,) = results
        assert result.status == "CONFIRMED", f"{coid} did not confirm: {result}"
        # Prove OUR order landed on the exchange: resolve the journal coid
        # (executor-owned `ct<key>` scheme) and read it back signed.
        stored_row = await commands.get(result.command_key)
        assert stored_row is not None
        our_coid = str(stored_row["client_order_id"])
        seen = await client.query_order(ACCOUNT, SYMBOL, orig_client_order_id=our_coid)
        assert seen is not None and int(seen.order_id) in recorder.order_ids, (
            f"{coid} confirmed without an exchange order"
        )
        return result

    stop_coid = f"{tag}-stop"
    try:
        await submit(ActionKind.MARKET_OPEN, Side.BUY, QTY, f"{tag}-open-a")
        await _wait_qty(client, QTY)
        await submit(ActionKind.MARKET_OPEN, Side.BUY, QTY, f"{tag}-open-b")
        await _wait_qty(client, QTY * 2)
        # Distant STOP through the journal: placed, observed, cancelled —
        # the position never changes, the lifecycle math stays intact.
        stop_results = await executor.submit(
            ExecutionPlan(
                account_id=ACCOUNT,
                trade_id=None,
                operation_id=stop_coid,
                actions=[
                    ExecutionAction(
                        kind=ActionKind.PLACE_STOP,
                        symbol=SYMBOL,
                        payload={
                            "symbol": SYMBOL,
                            "side": Side.SELL.value,
                            "type": "STOP_MARKET",
                            "quantity": format(QTY * 2, "f"),
                            "stopPrice": "1000",
                            **_close_mode_extras(hedge=hedge, leg=pside.value),
                            "workingType": "MARK_PRICE",
                            "newOrderRespType": "ACK",
                        },
                        client_order_id=stop_coid,
                        replaces_client_order_id=None,
                        priority=1,
                    )
                ],
            ),
            client=recorder,
        )
        assert stop_results[0].status == "CONFIRMED"
        # The exchange coid is executor-owned (`ct<key>`), so the observation
        # is matched by journal command key, not by our tag.
        placed = await history.protection_observations(scope, SYMBOL, t0, int(time.time() * 1000))
        assert any(r["command_key"] == stop_results[0].command_key for r in placed)
        cancel_results = await executor.submit(
            ExecutionPlan(
                account_id=ACCOUNT,
                trade_id=None,
                operation_id=f"{tag}-stop-cancel",
                actions=[
                    ExecutionAction(
                        kind=ActionKind.CANCEL_ORDER,
                        symbol=SYMBOL,
                        payload={"symbol": SYMBOL, "origClientOrderId": stop_coid},
                        client_order_id=f"{tag}-stop-cancel",
                        replaces_client_order_id=None,
                        priority=1,
                    )
                ],
            ),
            client=recorder,
        )
        assert cancel_results[0].status == "CONFIRMED"
        await submit(ActionKind.MARKET_CLOSE, Side.SELL, QTY, f"{tag}-reduce")
        await _wait_qty(client, QTY)
        # Controlled UNKNOWN: the order really reaches the exchange, then
        # the transport "loses" the response. The executor must resolve by
        # querying before any retry — exactly one order per key exists.
        recorder.fail_post_send_once = True
        await submit(ActionKind.MARKET_CLOSE, Side.SELL, QTY, f"{tag}-close")
        await _wait_flat(client)
    finally:
        # Fail-safe flatten, one leg at a time (hedge close-mode).
        for leg, qty in _leg_qtys(await client.position_risk(ACCOUNT, SYMBOL)).items():
            if qty == 0:
                continue
            side = Side.SELL if qty > 0 else Side.BUY
            await executor.submit(
                ExecutionPlan(
                    account_id=ACCOUNT,
                    trade_id=None,
                    operation_id=f"{tag}-failsafe-{leg}",
                    actions=[
                        ExecutionAction(
                            kind=ActionKind.MARKET_CLOSE,
                            symbol=SYMBOL,
                            payload={
                                "symbol": SYMBOL,
                                "side": side.value,
                                "type": "MARKET",
                                "quantity": format(abs(qty), "f"),
                                **_close_mode_extras(hedge=hedge, leg=leg),
                                "newOrderRespType": "RESULT",
                            },
                            client_order_id=f"{tag}-failsafe-{leg}",
                            replaces_client_order_id=None,
                            priority=1,
                        )
                    ],
                ),
                client=recorder,
            )
        await _wait_flat(client)
    await asyncio.sleep(8.0)  # REST indexing latency for userTrades/income.

    journal_kinds = {
        row["kind"]
        for row in await history_db.fetchall(
            "SELECT DISTINCT kind FROM execution_commands WHERE account_id=?", (ACCOUNT,)
        )
    }
    assert {"MARKET_OPEN", "MARKET_CLOSE", "PLACE_STOP", "CANCEL_ORDER"} <= journal_kinds

    reader = HistoryReader(client)
    episodes = EpisodeRepository(history_db)
    events = EventRepository(history_db)
    synchronizer = HistorySynchronizer(reader, history)
    import_window = Window(t0, int(time.time() * 1000))
    await synchronizer.sync_fills(scope, SYMBOL, import_window)
    await synchronizer.sync_income(scope, import_window)
    capture = HistoryCapture(
        history,
        episodes,
        events=events,
        commands=commands,
        projection=HistoryProjection(history, episodes, events, commands=commands),
    )
    window = Window(t0, int(time.time() * 1000))
    stored, published = await capture.capture_and_publish(
        scope, SYMBOL, window, snapshots=client, synchronizer=synchronizer, notify_eligible=True
    )
    assert published.start == t0 and published.end >= window.end - 300_000

    stored_fills = await history_db.fetchall(
        _FILLS_SINCE,
        (ACCOUNT, SYMBOL, t0),
    )
    ours = [r for r in stored_fills if int(str(r["order_id"])) in recorder.order_ids]
    assert len(ours) >= 4, "all lifecycle fills must be imported"
    boundary_before = await episodes.load_boundary(scope, SYMBOL)

    closed, _ = await episodes.list_closed(scope, symbol=SYMBOL, limit=50)
    assert len(closed) == 1, "one closed episode holds every size change"
    ref = closed[0].episode.public_ref
    detail_episode = (await episodes.get(scope, ref)).episode

    # REAL restart: close and reopen the same history file, rebuild every
    # object, prove the import adds nothing and the ref is identical.
    fills_before = len(stored_fills)
    events_before = len(await events.list_owned(scope))
    await history_db.close()
    history_db2 = Database(history_path)
    await history_db2.connect()
    try:
        history2 = HistoryRepository(history_db2)
        episodes2 = EpisodeRepository(history_db2)
        events2 = EventRepository(history_db2)
        commands2 = CommandRepo(history_db2)
        synchronizer2 = HistorySynchronizer(HistoryReader(client), history2)
        await synchronizer2.sync_fills(scope, SYMBOL, window)
        await synchronizer2.sync_income(scope, window)
        capture2 = HistoryCapture(
            history2,
            episodes2,
            events=events2,
            commands=commands2,
            projection=HistoryProjection(history2, episodes2, events2, commands=commands2),
        )
        restated, _ = await capture2.capture_and_publish(
            scope,
            SYMBOL,
            window,
            snapshots=client,
            synchronizer=synchronizer2,
            notify_eligible=True,
        )
        assert [s.episode.public_ref for s in restated] == [ref]
        # The frontier may have grown over an empty range between the runs;
        # identity is the proven end state, not the exact cutoff number.
        boundary_after = await episodes2.load_boundary(scope, SYMBOL)
        assert boundary_after is not None
        assert boundary_after.seeds == boundary_before.seeds
        assert boundary_after.mode == boundary_before.mode
        assert boundary_after.cutoff_ms >= boundary_before.cutoff_ms
        assert (
            len(
                await history_db2.fetchall(
                    _FILLS_SINCE,
                    (ACCOUNT, SYMBOL, t0),
                )
            )
            == fills_before
        )
        assert len(await events2.list_owned(scope)) == events_before
    finally:
        history_db = history_db2

    # §10.9.3/4: signed exchange truth vs episode money. No sign assert: a
    # real short trade may lose; equality with the exchange is the proof.
    live_fills = await client.user_trades(ACCOUNT, SYMBOL, start_time=t0, end_time=window.end)
    mine = [f for f in live_fills if f.time >= t0 and int(f.order_id) in recorder.order_ids]
    assert len(mine) >= 4
    exchange_gross = sum((D(str(f.realized_pnl)) for f in mine if f.side == "SELL"), D("0"))
    assert D(str(detail_episode.realized_pnl_gross)) == exchange_gross
    live_income = await client.income_history(ACCOUNT, start_time=t0, end_time=window.end)
    fill_fees = sum(
        (abs(D(str(f.commission))) for f in mine if f.commission_asset == "USDT"), D("0")
    )
    income_fees = sum(
        (
            abs(D(str(i.income)))
            for i in live_income
            if i.income_type == "COMMISSION" and i.asset == "USDT" and i.time >= t0
        ),
        D("0"),
    )
    assert fill_fees > 0 and income_fees == fill_fees
    money = account_episode(
        detail_episode,
        [f for f in live_fills if f.time >= t0],
        [i for i in live_income if i.time >= t0],
        proof=AccountingProof(),
    )
    assert money.gross == exchange_gross
    if money.net is None:
        assert money.issues, "net gap must name its concrete cause"
    else:
        assert not money.issues

    # §10.9.5: one ref and identical facts on every surface, over real HTTP.
    user_address = "0x0000000000000000000000000000000000000001"
    await AccountRepo(history_db).upsert(ACCOUNT, "follower", ACCOUNT, user_address)
    await TelegramUserRepo(history_db).upsert(USER_ID, 9001, account_id=ACCOUNT)
    await SettingsRepo(history_db).upsert_policy(
        RiskPolicy(
            risk_percent=D("0.01"),
            max_leverage=3,
            max_entry_deviation_pct=D("0.02"),
            enabled=True,
            allow_new_entries=True,
        ),
        ACCOUNT,
    )
    service = UserApplicationService(
        _LiveRuntime(history_db),  # type: ignore[arg-type]
        admin_ids=[100],
        testnet_check=True,
    )
    app_settings = Settings(encryption_key="00" * 32, telegram_bot_token=HTTP_TOKEN)
    app = create_miniapp_app(app_settings, service)  # type: ignore[arg-type]
    server = TestServer(app)
    http_client = TestClient(server)
    try:
        await http_client.start_server()
        headers = {INIT_DATA_HEADER: _signed_init_data(HTTP_TOKEN, USER_ID)}
        resp = await http_client.post(
            "/miniapp/api/trades", json={"period": "all"}, headers=headers
        )
        assert resp.status == 200
        body = await resp.json()
        assert any(item["trade_ref"] == ref for item in body["items"])
        resp = await http_client.post(
            "/miniapp/api/trade", json={"trade_ref": ref}, headers=headers
        )
        assert resp.status == 200
        assert (await resp.json())["trade"]["trade_ref"] == ref
        resp = await http_client.post(
            "/miniapp/api/trade/events", json={"trade_ref": ref}, headers=headers
        )
        assert resp.status == 200
        assert (await resp.json())["events"], "closed trade shows its transitions"
        resp = await http_client.post("/miniapp/api/activity/feed", json={}, headers=headers)
        assert resp.status == 200
        assert any(item["trade_ref"] == ref for item in (await resp.json())["items"])
    finally:
        await http_client.close()

    detail = await service.trade(USER_ID, ref)
    router = NotificationRouter(history_db, admin_ids=[100])
    recovered = await router.recover_user_events(EventRepository(history_db), [scope], since_ms=t0)
    assert recovered >= 1
    row = await history_db.fetchone("SELECT * FROM notification_outbox WHERE trade_ref=?", (ref,))
    assert detail.trade.trade_ref == row["trade_ref"] == ref
    assert detail.trade.executed_qty == "0.002"
    assert row["action"] == "open_trade"

    await AccountRepo(history_db).upsert("foreign", "follower", "foreign", "0x" + "3" * 40)
    await TelegramUserRepo(history_db).upsert(7002, 9002, account_id="foreign")
    assert (await service.trades(7002)).items == ()
    with pytest.raises(TradeUnavailableError):
        await service.trade(7002, ref)
