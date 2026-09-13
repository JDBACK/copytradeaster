"""Live acceptance: the close card carries the proven net over REAL Telegram.

The outbox row is created when the close event is published, and publication
happens as soon as the copy engine sees the flat — before the history worker has
imported that trade's commissions. So the card is rendered again at send time,
and this scenario proves the whole chain on live infrastructure:

1. a real master round trip copies to the owner's follower account;
2. the follower close publishes a `POSITION_CLOSED` user event and a durable
   outbox row;
3. with the money reader and the send-time renderer wired exactly as the
   production composition wires them, the row's text is re-rendered to
   `Итог сделки: <net> USDT` once the costs are proven;
4. the production `TelegramOutboxWorker` delivers that exact text through the
   real Bot API (terminal `SENT`).

Reuses the isolated-copy `live_app_stack` fixture (throwaway `sqlite3.backup` of
the real database) from the P2.3 acceptance module, so no production row is
written. Accounts are flattened in the fixture teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from aiogram import Bot

from copytradeaster.application.engine import build_user_event_renderer
from copytradeaster.aster.trading_api import PlaceOrderRequest
from copytradeaster.config import Settings
from copytradeaster.domain.models import PosSide, Side
from copytradeaster.notifications.money import SqliteEpisodeMoneyReader
from copytradeaster.notifications.worker import TelegramOutboxWorker
from copytradeaster.storage.episodes import EpisodeRepository
from copytradeaster.storage.history import HistoryRepository
from copytradeaster.storage.repositories import TelegramUserRepo

from .test_live_notifications_acceptance import (
    FOLLOWER_ACCOUNT,
    SYMBOL,
    TEST_USER_ID,
)
from .test_live_notifications_acceptance import live_app_stack as live_app_stack  # noqa: F401

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        os.environ.get("CT_TESTNET") != "1", reason="signed testnet runs require CT_TESTNET=1"
    ),
]

NET_LINE = "Итог сделки:"
# The default owner account pays fees in ASTER (528 fills), which the
# accountant refuses to convert without a historical rate. Set
# ``NET_CARD_ACCOUNT`` to a USDT-fee follower to exercise the full net path.
# The owner chat is the only one this bot has proven it can reach and a Telegram
# chat backs exactly one ``telegram_users`` row, so the variant hands that row to
# the follower under test inside the throwaway copy (see ``_delivery_recipient``).
FOLLOWER = os.environ.get("NET_CARD_ACCOUNT") or FOLLOWER_ACCOUNT
RETARGETED = FOLLOWER != FOLLOWER_ACCOUNT


async def _delivery_recipient(app) -> tuple[int, int]:
    """``(user_id, chat_id)`` that can really receive this follower's card.

    The product lets a Telegram user hold one account; the guard only refuses
    while the account it already owns is enabled. Handing the proven chat to a
    retargeted follower therefore disables that old account in the throwaway
    copy and clears the relation through the same repository method the product
    uses — the guard stays in force instead of being bypassed, and the real
    database is never touched.
    """
    if not RETARGETED:
        return TEST_USER_ID, TEST_USER_ID
    row = await app.db.fetchone(
        "SELECT account_id FROM telegram_users WHERE telegram_user_id=?", (TEST_USER_ID,)
    )
    current = None if row is None else row["account_id"]
    if current in (None, FOLLOWER):
        return TEST_USER_ID, TEST_USER_ID
    await app.db.execute_write("UPDATE accounts SET enabled=0 WHERE id=?", (str(current),))
    await TelegramUserRepo(app.db).upsert(
        TEST_USER_ID, TEST_USER_ID, account_id=None, notifications_enabled=True
    )
    return TEST_USER_ID, TEST_USER_ID


async def _wait_flat(app, client, label: str, timeout: float = 90.0) -> None:
    """Reconcile until the follower's copied position is gone again."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        positions = await client.position_risk(FOLLOWER)
        if all(position.position_amt == 0 for position in positions):
            return
        await app.projector.rebuild_full(client, "main")
        await app.reconcile_follower(FOLLOWER, reason=label)
        await asyncio.sleep(1.0)
    pytest.fail(f"follower {FOLLOWER} did not return to flat")


async def _flatten_account(app, client, account_id: str, timeout: float = 60.0) -> None:
    """Cancel protection and market-close any position left on the follower.

    Testnet accounts are disposable, but the scenario needs a known starting
    quantity: the copy turns a leftover 0.001 plus the master's 0.002 into one
    0.003 close, and the assertions below bind to this trade's own fill.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        positions = await client.position_risk(account_id)
        open_orders = await client.open_orders(account_id)
        if all(position.position_amt == 0 for position in positions) and not open_orders:
            return
        for order in open_orders:
            with contextlib.suppress(Exception):
                await client.cancel_order(
                    account_id, SYMBOL, orig_client_order_id=order.client_order_id
                )
        for position in positions:
            if position.position_amt == 0:
                continue
            with contextlib.suppress(Exception):
                await client.place_order(
                    account_id,
                    PlaceOrderRequest(
                        symbol=SYMBOL,
                        side=Side.SELL if position.position_amt > 0 else Side.BUY,
                        type="MARKET",
                        quantity=abs(position.position_amt),
                        reduce_only=True,
                        position_side=position.position_side,
                        new_order_resp_type="RESULT",
                        new_client_order_id=f"live-net-flat-{int(time.time() * 1000)}"[:36],
                    ),
                )
        await asyncio.sleep(1.0)
    pytest.fail(f"follower {account_id} could not be flattened before the scenario")


async def test_live_close_card_carries_the_proven_net(live_app_stack) -> None:
    app = live_app_stack["app"]
    client = live_app_stack["client"]
    users = live_app_stack["users"]
    hedge_master = live_app_stack["hedge_master"]
    baseline = live_app_stack["outbox_baseline_max"]
    recipient, recipient_chat = await _delivery_recipient(app)
    # The isolated copy inherits earlier runs' backlog. A stale
    # `execution.confirmed` row the Bot API rejects makes the worker disable the
    # whole recipient (its production safety reaction), which would hide the
    # card this scenario is about. Start the copy from a clean delivery state:
    # this touches the throwaway copy only, never the real database.
    await app.db.execute_write("DELETE FROM notification_outbox")
    await app.db.execute_write(
        "UPDATE telegram_users SET notifications_enabled=1, blocked_at=NULL, "
        "access_enabled=1 WHERE telegram_user_id=?",
        (recipient,),
    )
    if RETARGETED:
        # A follower other than the fixture's owner is not in the registry yet
        # (the copy engine only reconciles registered runtimes), and the durable
        # router needs a recipient row for it. Both changes live in the throwaway
        # copy; the real chat id keeps the Bot API leg genuinely live.
        if app.follower_registry.get(FOLLOWER) is None:
            await app.register_follower(FOLLOWER)
        await users.upsert(
            recipient,
            recipient_chat,
            role="USER",
            account_id=FOLLOWER,
            notifications_enabled=True,
        )
        # The fixture flattened its own follower, not this one. A leftover test
        # position would be added to the copied entry (`copy entry must land`
        # only checks "non-zero"), and the close fill would then not match the
        # master's quantity. Start from a known flat account.
        await _flatten_account(app, client, FOLLOWER)

    # The same seam the production composition uses, plus one extra: the card is
    # rendered again at send time so a commission import that lands after the
    # enqueue still reaches the message.
    renderer = build_user_event_renderer(app.db)
    settings = Settings()
    bot = Bot(settings.telegram_token_value())
    worker = TelegramOutboxWorker(
        bot=bot,
        db=app.db,
        concurrency=1,
        poll_interval=0.2,
        per_chat_interval=0.0,
        max_attempts=3,
        user_events_only=True,
        user_event_renderer=renderer,
    )

    async def _rows_after_baseline() -> list[dict]:
        # ``SELECT *``: the send-time renderer needs ``account_id`` and
        # ``trade_ref``, and the production lease hands it the full row. A
        # narrower projection silently makes the renderer a no-op (it keeps the
        # stored text), which would look like a passing gross-only assertion.
        rows = await app.db.fetchall(
            "SELECT * FROM notification_outbox WHERE id>? AND telegram_user_id=? ORDER BY id",
            (baseline, recipient),
        )
        return [dict(row) for row in rows]

    side = PosSide.LONG if hedge_master else PosSide.BOTH
    ts = int(time.time() * 1000)
    # The account carries hundreds of episodes from earlier runs, and its newest
    # POSITION_CLOSED event can belong to any of them (two closes can land in the
    # same import pass). Both durable floors below are taken *before* the trade,
    # so the assertions bind to the event and the row this trade produces.
    event_floor_row = await app.db.fetchone(
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM user_activity_events WHERE account_id=?",
        (FOLLOWER,),
    )
    event_floor = int(event_floor_row["max_seq"])
    # Deleting the delivery cursor makes the sweep re-plan every already
    # published event for this account - leftover rows from earlier runs would
    # then be indistinguishable from this trade's row. Pin the cursor to the
    # pre-trade floor instead, so only the events this trade publishes can be
    # enqueued. (The copy is throwaway; the real database keeps its cursor.)
    await app.db.execute_write(
        "INSERT INTO user_event_delivery_state(account_id, environment, scanned_seq, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(account_id, environment) DO UPDATE SET "
        "scanned_seq=excluded.scanned_seq, updated_at=excluded.updated_at",
        (FOLLOWER, "testnet", event_floor, ts),
    )
    try:
        # 1. Real master entry → copied to the owner's account.
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.BUY,
                type="MARKET",
                quantity=D("0.002"),
                position_side=side,
                new_client_order_id=f"live-net-open-{ts}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        # A bare entry is refused by design (`NO_STOP`): the follower only opens
        # what the master protects with a stop.
        entry_price = (await client.position_risk("main", SYMBOL))[0].mark_price
        stop_price = (entry_price * D("0.95")).quantize(D("0.1"))
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="STOP_MARKET",
                stop_price=stop_price,
                close_position=True,
                position_side=side,
                new_client_order_id=f"live-net-stop-{ts}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        await app.projector.rebuild_full(client, "main")
        await app.reconcile_follower(FOLLOWER, reason="live_net_open")
        copied = await client.position_risk(FOLLOWER)
        assert any(position.position_amt != 0 for position in copied), "copy entry must land"

        # 2. Real master close → follower close → POSITION_CLOSED + outbox row.
        await client.place_order(
            "main",
            PlaceOrderRequest(
                symbol=SYMBOL,
                side=Side.SELL,
                type="MARKET",
                quantity=D("0.002"),
                position_side=side,
                reduce_only=not hedge_master,
                new_client_order_id=f"live-net-close-{ts}"[:36],
                new_order_resp_type="RESULT",
            ),
        )
        await _wait_flat(app, client, "live_net_close")

        # The production history worker owns fill import, episode/event
        # publication and the commissions/funding import. This harness runs it
        # explicitly so the same durable rows appear as in a live process.
        from copytradeaster.history.composition import build_history_worker

        history_worker = build_history_worker(app)
        deadline = time.monotonic() + 60
        money = None
        ref = None
        while time.monotonic() < deadline:
            # Bind to *this* trade through its own durable facts: the sweep can
            # also enqueue older pending events for the account (a catch-up pass
            # publishes several episodes at once), so "newest POSITION_CLOSED"
            # is not the trade under test. The close fill imported after the
            # pre-trade floor is, and its allocation names the episode.
            fill_rows = await app.db.fetchall(
                "SELECT exchange_trade_id, qty FROM follower_fills "
                "WHERE account_id=? AND symbol=? AND executed_at>=? ORDER BY executed_at DESC",
                (FOLLOWER, SYMBOL, ts),
            )
            for fill_row in fill_rows:
                alloc_rows = await app.db.fetchall(
                    "SELECT trade_ref FROM follower_episode_allocations "
                    "WHERE account_id=? AND symbol=? AND exchange_trade_id=? AND role='exit'",
                    (FOLLOWER, SYMBOL, str(fill_row["exchange_trade_id"])),
                )
                if alloc_rows:
                    ref = str(alloc_rows[0]["trade_ref"])
                    break
            if ref is not None:
                from copytradeaster.storage.history import HistoryScope

                scope = HistoryScope(FOLLOWER, "testnet")
                reader = SqliteEpisodeMoneyReader(
                    EpisodeRepository(app.db), HistoryRepository(app.db)
                )
                money = (await reader.money_for(scope, ref)).get(ref)
                if money is not None and (
                    money.result_state == "net_complete" or "non_usdt_cost" in money.issues
                ):
                    break
            await history_worker.run_once()
            await asyncio.sleep(0.5)
        assert money is not None, "the close event must be published"
        assert money.result_state == "net_complete" or "non_usdt_cost" in money.issues, (
            f"the live account must have a provable or explicitly non-USDT cost set, "
            f"issues={money.issues}"
        )

        # 3. The durable row exists and the send-time render carries the net.
        # The production delivery sweep is the only path that turns a published
        # event into an outbox row, and it runs behind a rollout floor; this
        # harness drives the exact same function the live process calls.
        from copytradeaster.notifications.delivery import (
            ensure_user_event_rollout_floor,
            recover_user_events_durable,
        )
        from copytradeaster.storage.events import EventRepository

        floor = await ensure_user_event_rollout_floor(app.db, now=datetime.now(UTC))
        await users.set_notifications_enabled(recipient, True)
        await app.db.execute_write(
            "UPDATE telegram_users SET blocked_at=NULL WHERE telegram_user_id=?",
            (recipient,),
        )
        await recover_user_events_durable(
            app.db,
            app.notifier,
            EventRepository(app.db),
            [scope],
            rollout_floor_ms=floor,
        )
        row = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            # The sweep replays every event above its cursor, so pick the row for
            # this trade's episode instead of the first closed card it enqueued.
            rows = [
                r
                for r in await _rows_after_baseline()
                if r["kind"] == "user.POSITION_CLOSED" and str(r["trade_ref"]) == ref
            ]
            if rows:
                row = rows[0]
                break
            await asyncio.sleep(0.5)
        assert row is not None, f"this trade's episode {ref} must enqueue a durable outbox row"
        rendered = await renderer(row, str(row["text"]))
        if money.result_state == "net_complete":
            # The card must carry the net of the episode as it stands now: the
            # live history worker keeps importing inside this window, so compare
            # against a read taken at render time, not against an earlier poll.
            now_money = (await reader.money_for(scope, ref)).get(ref)
            assert rendered is not None, (
                f"a proven net must re-render the close card; at render time "
                f"result_state={None if now_money is None else now_money.result_state} "
                f"gross={None if now_money is None else now_money.gross} "
                f"issues={None if now_money is None else now_money.issues}"
            )
            assert NET_LINE in rendered, rendered
            assert now_money is not None and now_money.net is not None, now_money
            assert now_money.net in rendered, (now_money, rendered)
            assert "до комиссий" not in rendered, rendered
        else:
            # Fees paid in a non-USDT asset cannot be converted without a
            # historical rate. The renderer deliberately keeps the stored card,
            # which is the honestly labelled gross result — never a net guess.
            assert rendered is None, rendered
            assert "PnL до комиссий" in str(row["text"]), row["text"]

        # 4. Real Bot API delivery of that exact text (terminal SENT). The
        # worker re-renders and persists what it actually handed to Bot API.
        for _ in range(30):
            await worker.process_once()
            fresh = await _rows_after_baseline()
            current = next(r for r in fresh if r["id"] == row["id"])
            if current["status"] == "SENT":
                delivered = str(current["text"])
                if money.result_state == "net_complete":
                    # The delivered message must carry a proven net for this
                    # episode; the worker re-renders at its own send moment, so
                    # the value is read again rather than reused from the poll.
                    # The stored enqueue text carried the gross line, so its
                    # absence is the proof that the send-time render really ran.
                    delivered_money = (await reader.money_for(scope, ref)).get(ref)
                    assert NET_LINE in delivered, (
                        f"delivered={delivered!r} rendered={rendered!r} money={money}"
                    )
                    assert "до комиссий" not in delivered, delivered
                    assert delivered_money is not None and delivered_money.net is not None, (
                        delivered_money
                    )
                    assert delivered_money.net in delivered, (delivered_money, delivered)
                else:
                    # Non-USDT cost: the delivered card keeps the honest gross
                    # line, and the live Bot API leg is proven by the SENT row.
                    assert "PnL до комиссий" in delivered, delivered
                    assert money.net is None
                break
            assert current["status"] in {"PENDING", "SENDING"}, current
            await asyncio.sleep(0.5)
        else:
            pytest.fail("the close card was not delivered by the real Bot API")
    finally:
        with contextlib.suppress(Exception):
            await bot.session.close()


def _deliver_overriding_text(worker: TelegramOutboxWorker, text: str):
    """Send the freshly rendered card without mutating the durable row.

    The row keeps the text it was enqueued with (the outbox is a delivery log,
    not a rendering cache), so the acceptance asserts on the message that is
    actually handed to the Bot API.
    """
    original_send = worker._send  # noqa: SLF001 - explicit acceptance seam

    async def send(chat_id: int, _text: str, markup=None):
        return await original_send(chat_id, text, markup)

    worker._send = send  # type: ignore[method-assign]  # noqa: SLF001
    return worker._deliver  # noqa: SLF001
