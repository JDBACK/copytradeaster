"""Bounded Telegram outbox worker with leases and safe retry policy."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from copytradeaster.logging_utils import get_logger
from copytradeaster.notifications.models import NOTIFICATION_ACTIONS, clip_plain_text
from copytradeaster.storage.repositories import NotificationOutboxRepo, TelegramUserRepo

log = get_logger(__name__)
BotSender = Callable[[int, str], Awaitable[Any]]

OPEN_TRADE_BUTTON_TEXT = "Открыть сделку"


def trade_keyboard(
    trade_ref: Any | None, action: Any | None, base_url: str | None
) -> InlineKeyboardMarkup | None:
    """Episode deep-link button for a structured outbox row.

    Returns None unless the action is known, the ref is proven and the
    WebApp base URL is configured. The button stays dormant (None) until
    S3 activates the trade route; until then delivery is text-only.
    """
    if action != "open_trade" or action not in NOTIFICATION_ACTIONS:
        return None
    ref = str(trade_ref or "")
    base = (base_url or "").rstrip("/")
    if not ref or not base:
        return None
    url = f"{base}/miniapp/?trade_ref={quote(ref)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=OPEN_TRADE_BUTTON_TEXT, web_app=WebAppInfo(url=url))]
        ]
    )


class TelegramOutboxWorker:
    """Deliver pending rows without coupling trading completion to Telegram."""

    def __init__(
        self,
        bot: Any | None = None,
        db: Any | None = None,
        *,
        sender: BotSender | None = None,
        outbox: NotificationOutboxRepo | None = None,
        users: TelegramUserRepo | None = None,
        worker_id: str | None = None,
        concurrency: int = 2,
        poll_interval: float = 0.25,
        lease_seconds: int = 30,
        max_attempts: int = 5,
        per_chat_interval: float = 1.0,
        retry_base: float = 1.0,
        retry_max: float = 60.0,
        retry_jitter: float = 0.25,
        drain_timeout: float = 5.0,
        webapp_base_url: str | None = None,
        trade_links_enabled: bool = False,
    ) -> None:
        if bot is None and sender is None:
            raise ValueError("bot or sender is required")
        if not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be between 1 and 4")
        if poll_interval <= 0 or lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("poll_interval, lease_seconds and max_attempts must be positive")
        if per_chat_interval < 0 or retry_base < 0 or retry_max < 0 or retry_jitter < 0:
            raise ValueError("pacing and retry delays cannot be negative")
        if drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive")
        if outbox is None or users is None:
            if db is None:
                raise ValueError("db is required when repositories are not supplied")
            outbox = outbox or NotificationOutboxRepo(db)
            users = users or TelegramUserRepo(db)
        self._bot = bot
        self._sender = sender
        self._webapp_base_url = (webapp_base_url or "").rstrip("/") or None
        # S3 capability, not URL presence: the episode deep-link button needs
        # an accepted owned route. Payload keeps its structured action/ref
        # while delivery stays text-only until then.
        self._trade_links_enabled = bool(trade_links_enabled)
        self._outbox = outbox
        self._users = users
        self._worker_id = worker_id or f"telegram-{uuid.uuid4().hex}"
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._per_chat_interval = per_chat_interval
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._retry_jitter = retry_jitter
        self._drain_timeout = drain_timeout
        self._stop_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_chat_send: dict[int, float] = {}
        self._started = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            await self._outbox.recover_expired()
            await self._prune_retention()
            self._stop_event.clear()
            self._tasks = [
                asyncio.create_task(
                    self._worker_loop(index),
                    name=f"{self._worker_id}-{index}",
                )
                for index in range(self._concurrency)
            ]
            self._started = True

    async def _prune_retention(self) -> None:
        """Bounded once-per-boot terminal cleanup; never fails startup."""
        try:
            now = datetime.now(UTC)
            await self._outbox.prune_terminal(before=now - timedelta(days=7), statuses=("SENT",))
            await self._outbox.prune_terminal(before=now - timedelta(days=30), statuses=("FAILED",))
        except Exception:  # noqa: BLE001 - retention must not block delivery
            log.exception("terminal outbox prune failed")

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

    async def stop(self, *, drain: bool = False, timeout: float | None = None) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                if drain:
                    await self.drain(timeout=timeout)
                return
            started_at = time.monotonic()
            self._stop_event.set()
            tasks = tuple(self._tasks)
            try:
                if tasks:
                    wait_timeout = self._drain_timeout if timeout is None else timeout
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True),
                            timeout=wait_timeout,
                        )
                    except TimeoutError:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                if drain:
                    remaining = self._drain_timeout if timeout is None else timeout
                    remaining -= time.monotonic() - started_at
                    if remaining > 0:
                        await self.drain(timeout=remaining)
            finally:
                self._tasks = []
                self._started = False

    async def drain(self, *, timeout: float | None = None) -> int:
        """Process pending rows until empty or the bounded deadline expires."""
        deadline = time.monotonic() + (self._drain_timeout if timeout is None else timeout)
        processed = 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                did_work = await asyncio.wait_for(self.process_once(), timeout=remaining)
            except TimeoutError:
                break
            if not did_work:
                break
            processed += 1
        return processed

    async def process_once(self) -> bool:
        """Recover stale leases, claim one row and attempt one delivery."""
        await self._outbox.recover_expired()
        row = await self._outbox.claim_one(
            self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if row is None:
            return False
        await self._deliver(row)
        return True

    async def status(self) -> dict[str, int]:
        counts = await self._outbox.counts()
        counts["WORKER_TASKS"] = len(self._tasks)
        counts["STARTED"] = int(self._started)
        return counts

    async def _worker_loop(self, index: int) -> None:
        worker_id = f"{self._worker_id}:{index}"
        while not self._stop_event.is_set():
            try:
                await self._outbox.recover_expired()
                row = await self._outbox.claim_one(
                    worker_id,
                    lease_seconds=self._lease_seconds,
                )
                if row is None:
                    await self._wait_or_stop(self._poll_interval)
                    continue
                await self._deliver(row, worker_id=worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker must stay alive after one row
                log.exception("notification worker iteration failed")
                await self._wait_or_stop(self._poll_interval)

    async def _wait_or_stop(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _deliver(self, row: Any, *, worker_id: str | None = None) -> None:
        worker_id = worker_id or self._worker_id
        user_id = int(row["telegram_user_id"])
        active = await self._users.notification_recipients(telegram_user_ids=[user_id])
        if not active:
            await self._outbox.fail(row["id"], "recipient_disabled", worker_id)
            return
        chat_id = int(row["chat_id"])
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        trade_ref = row["trade_ref"] if "trade_ref" in keys else None
        action = row["action"] if "action" in keys else None
        markup = (
            trade_keyboard(trade_ref, action, self._webapp_base_url)
            if self._trade_links_enabled
            else None
        )
        async with self._chat_lock(chat_id):
            await self._pace(chat_id)
            try:
                await self._send(chat_id, clip_plain_text(str(row["text"])), markup)
            except TelegramRetryAfter as exc:
                delay = max(1, int(exc.retry_after))
                await self._outbox.retry(
                    row["id"],
                    "telegram_retry_after",
                    available_at=datetime.now(UTC) + timedelta(seconds=delay),
                    max_attempts=self._max_attempts,
                    worker_id=worker_id,
                )
            except TelegramForbiddenError:
                await self._outbox.fail(row["id"], "telegram_forbidden", worker_id)
                try:
                    await self._users.disable_notifications(user_id)
                except Exception:  # noqa: BLE001 - one blocked recipient is isolated
                    log.exception("failed to disable forbidden notification recipient")
            except TelegramBadRequest:
                await self._outbox.fail(row["id"], "telegram_bad_request", worker_id)
            except (
                TelegramNetworkError,
                TelegramServerError,
                httpx.HTTPError,
                TimeoutError,
                OSError,
            ):
                await self._retry_transport(row, worker_id)
            except Exception:  # noqa: BLE001 - bounded retry for sender adapters
                await self._retry_transport(row, worker_id, error_code="send_error")
            else:
                await self._outbox.mark_sent(row["id"], worker_id)
            finally:
                self._last_chat_send[chat_id] = time.monotonic()

    async def _send(
        self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> Any:
        if self._sender is not None:
            # Custom senders stay text-only; the keyboard is a Bot API
            # transport-boundary concern assembled for the real bot below.
            return await self._sender(chat_id, text)
        if reply_markup is None:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=None,
            )
        return await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=None,
            reply_markup=reply_markup,
        )

    async def _pace(self, chat_id: int) -> None:
        if self._per_chat_interval <= 0:
            return
        previous = self._last_chat_send.get(chat_id)
        if previous is None:
            return
        delay = self._per_chat_interval - (time.monotonic() - previous)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _retry_transport(
        self,
        row: Any,
        worker_id: str,
        *,
        error_code: str = "transport_error",
    ) -> None:
        attempts = max(1, int(row["attempts"]))
        exponent = max(0, attempts - 1)
        delay = min(self._retry_max, self._retry_base * (2**exponent))
        if self._retry_jitter:
            delay += random.uniform(0, self._retry_jitter)
        await self._outbox.retry(
            row["id"],
            error_code,
            available_at=datetime.now(UTC) + timedelta(seconds=delay),
            max_attempts=self._max_attempts,
            worker_id=worker_id,
        )
