"""Live presentation transport over follower snapshots and Aster stream hints."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from websockets.asyncio.client import connect as default_ws_factory

from copytradeaster.aster.user_stream import StreamEvent, UserStream
from copytradeaster.logging_utils import get_logger
from copytradeaster.storage.repositories import TelegramUserRepo
from copytradeaster.trading.coordinator import FollowerRuntime

if TYPE_CHECKING:
    from copytradeaster.app import App
    from copytradeaster.trading.snapshots import SnapshotPublication

log = get_logger(__name__)

LIVE_TICKET_TTL_S = 20.0
LIVE_QUEUE_MAX = 8
FOLLOWER_SYNC_INTERVAL_S = 1.0
FOLLOWER_EVENT_DEBOUNCE_S = 0.2
MARKET_BACKOFF_MAX_S = 30.0
FOLLOWER_EVENT_TYPES = frozenset(
    {"ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE", "ACCOUNT_CONFIG_UPDATE", "MARGIN_CALL"}
)


@dataclass(frozen=True, slots=True)
class LiveGrant:
    user_id: int
    account_id: str


@dataclass(slots=True)
class _Ticket:
    grant: LiveGrant
    expires_at: float


class LiveTicketStore:
    """Short-lived single-use tickets; only a SHA-256 digest is retained."""

    def __init__(
        self,
        *,
        ttl_s: float = LIVE_TICKET_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        max_pending: int = 1024,
    ) -> None:
        if ttl_s <= 0 or max_pending <= 0:
            raise ValueError("live ticket bounds must be positive")
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._max_pending = int(max_pending)
        self._tickets: dict[bytes, _Ticket] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    async def issue(self, grant: LiveGrant) -> str:
        token = secrets.token_urlsafe(32)
        digest = _ticket_digest(token)
        async with self._lock:
            self._prune()
            while len(self._tickets) >= self._max_pending:
                oldest = min(self._tickets, key=lambda item: self._tickets[item].expires_at)
                self._tickets.pop(oldest, None)
            self._tickets[digest] = _Ticket(grant, self._clock() + self._ttl_s)
        return token

    async def consume(self, token: str) -> LiveGrant | None:
        if not token or len(token) > 256:
            return None
        digest = _ticket_digest(token)
        async with self._lock:
            self._prune()
            ticket = self._tickets.pop(digest, None)
            if ticket is None or ticket.expires_at <= self._clock():
                return None
            return ticket.grant

    def _prune(self) -> None:
        now = self._clock()
        for digest, ticket in tuple(self._tickets.items()):
            if ticket.expires_at <= now:
                self._tickets.pop(digest, None)


class MiniAppLiveHub:
    """Bounded account-scoped fan-out of invalidations, never raw Aster data."""

    def __init__(self, *, queue_max: int = LIVE_QUEUE_MAX) -> None:
        if queue_max <= 0:
            raise ValueError("live queue size must be positive")
        self._queue_max = int(queue_max)
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._revisions: dict[str, int] = {}
        self._health: dict[str, str] = {}
        self._last_update_at: dict[str, str] = {}

    def subscribe(self, account_id: str) -> tuple[asyncio.Queue[dict[str, Any]], dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_max)
        self._queues.setdefault(account_id, set()).add(queue)
        initial = {
            "type": "hello",
            "revision": self._revisions.get(account_id, 0),
            "health": self._health.get(account_id, "connecting"),
            "updated_at": self._last_update_at.get(account_id),
        }
        return queue, initial

    def unsubscribe(self, account_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        queues = self._queues.get(account_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._queues.pop(account_id, None)

    def publish_snapshot(self, publication: SnapshotPublication) -> None:
        self._last_update_at[publication.account_id] = publication.published_at.isoformat()
        self._publish(
            publication.account_id,
            {
                "type": "update",
                "resources": sorted(publication.resources),
                "source": publication.source,
                "captured_at": publication.captured_at.isoformat(),
                "updated_at": publication.published_at.isoformat(),
            },
        )

    def publish_history(self, account_id: str) -> None:
        self._publish(account_id, {"type": "update", "resources": ["history"]})

    def publish_health(self, account_id: str, health: str) -> None:
        if health not in {"connecting", "live", "reconnecting"}:
            raise ValueError("invalid live health")
        if self._health.get(account_id) == health:
            return
        self._health[account_id] = health
        self._publish(account_id, {"type": "health", "health": health})

    def _publish(self, account_id: str, payload: dict[str, Any]) -> None:
        revision = self._revisions.get(account_id, 0) + 1
        self._revisions[account_id] = revision
        event = {**payload, "revision": revision}
        for queue in tuple(self._queues.get(account_id, ())):
            if queue.full():
                while not queue.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


@dataclass(slots=True)
class _FollowerHandle:
    runtime: FollowerRuntime
    stop: asyncio.Event
    task: asyncio.Task[None]


def _ticket_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _mark_prices(payload: str | bytes, whitelist: set[str]) -> dict[str, Decimal]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    if isinstance(raw, dict) and "data" in raw:
        raw = raw["data"]
    items = raw if isinstance(raw, list) else [raw]
    prices: dict[str, Decimal] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("s") or "").strip().upper()
        if symbol not in whitelist:
            continue
        try:
            price = Decimal(str(item.get("p") or ""))
        except (InvalidOperation, ValueError):
            continue
        if price.is_finite() and price > 0:
            prices[symbol] = price
    return prices


class MiniAppLiveRuntime:
    """Own follower private streams, shared mark stream, tickets and fan-out."""

    def __init__(
        self,
        app: App,
        *,
        user_stream_factory: Callable[..., UserStream] = UserStream,
        market_ws_factory: Callable[[str], Any] = default_ws_factory,
    ) -> None:
        self.app = app
        self.hub = MiniAppLiveHub()
        self.tickets = LiveTicketStore()
        self._users = TelegramUserRepo(app.db)
        self._user_stream_factory = user_stream_factory
        self._market_ws_factory = market_ws_factory
        self._stop = asyncio.Event()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._market_task: asyncio.Task[None] | None = None
        self._followers: dict[str, _FollowerHandle] = {}
        self._private_health: dict[str, bool] = {}
        self._market_live = False
        self._unsubscribe = app.follower_snapshots.subscribe(self.hub.publish_snapshot)

    async def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._stop.clear()
        self._supervisor_task = asyncio.create_task(
            self._supervise_followers(), name="miniapp-follower-streams"
        )
        self._market_task = asyncio.create_task(
            self._run_market_stream(), name="miniapp-mark-price-stream"
        )

    async def stop(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._supervisor_task, self._market_task) if task is not None]
        self._supervisor_task = None
        self._market_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for account_id in tuple(self._followers):
            await self._stop_follower(account_id)
        self._unsubscribe()

    async def issue_ticket(self, user_id: int) -> dict[str, Any] | None:
        row = await self._users.get(user_id)
        if (
            row is None
            or not int(row["access_enabled"])
            or row["account_id"] is None
        ):
            return None
        account_id = str(row["account_id"])
        if self.app.follower_registry.get(account_id) is None:
            return None
        token = await self.tickets.issue(LiveGrant(int(user_id), account_id))
        return {"ticket": token, "expires_in": int(self.tickets.ttl_s)}

    async def consume_ticket(self, token: str) -> LiveGrant | None:
        grant = await self.tickets.consume(token)
        if grant is None:
            return None
        row = await self._users.get(grant.user_id)
        if (
            row is None
            or not int(row["access_enabled"])
            or str(row["account_id"] or "") != grant.account_id
            or self.app.follower_registry.get(grant.account_id) is None
        ):
            return None
        return grant

    def publish_history(self, account_id: str) -> None:
        self.hub.publish_history(account_id)

    async def _supervise_followers(self) -> None:
        try:
            while not self._stop.is_set():
                current = {
                    runtime.account_id: runtime
                    for runtime in self.app.follower_registry.snapshot()
                    if runtime.execution_mode == "REAL"
                }
                for account_id, handle in tuple(self._followers.items()):
                    if current.get(account_id) is not handle.runtime:
                        await self._stop_follower(account_id)
                for account_id, runtime in current.items():
                    if account_id not in self._followers:
                        self._start_follower(runtime)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=FOLLOWER_SYNC_INTERVAL_S)
        finally:
            for account_id in tuple(self._followers):
                await self._stop_follower(account_id)

    def _start_follower(self, runtime: FollowerRuntime) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._run_follower(runtime, stop),
            name=f"miniapp-follower-stream:{runtime.account_id}",
        )
        self._followers[runtime.account_id] = _FollowerHandle(runtime, stop, task)
        self._private_health[runtime.account_id] = False
        self._publish_health(runtime.account_id)

    async def _stop_follower(self, account_id: str) -> None:
        handle = self._followers.pop(account_id, None)
        self._private_health.pop(account_id, None)
        if handle is None:
            return
        handle.stop.set()
        handle.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await handle.task
        close_key = getattr(handle.runtime.client, "close_listen_key", None)
        if callable(close_key):
            with contextlib.suppress(Exception):
                await close_key(account_id)

    async def _run_follower(self, runtime: FollowerRuntime, stop: asyncio.Event) -> None:
        dirty = asyncio.Event()
        refresh_lock = asyncio.Lock()

        async def refresh(*, connected_boundary: bool = False) -> None:
            async with refresh_lock:
                if self.app.follower_registry.get(runtime.account_id) is not runtime:
                    return
                try:
                    await self.app.coordinator.snapshot_follower(runtime)
                except Exception as exc:
                    self._private_health[runtime.account_id] = False
                    self._publish_health(runtime.account_id)
                    log.warning(
                        "follower live snapshot failed account_id=%s class=%s",
                        runtime.account_id,
                        type(exc).__name__,
                    )
                    if connected_boundary:
                        raise
                    return
                self._private_health[runtime.account_id] = True
                self._publish_health(runtime.account_id)

        async def on_event(event: StreamEvent) -> None:
            if str(event.raw.get("e") or "") in FOLLOWER_EVENT_TYPES:
                dirty.set()

        async def on_reconnect() -> None:
            self._private_health[runtime.account_id] = False
            self._publish_health(runtime.account_id)

        async def on_connected() -> None:
            await refresh(connected_boundary=True)

        async def on_disconnected() -> None:
            self._private_health[runtime.account_id] = False
            self._publish_health(runtime.account_id)

        async def refresh_worker() -> None:
            while not stop.is_set():
                dirty_wait = asyncio.create_task(dirty.wait())
                stop_wait = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait(
                        {dirty_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in (dirty_wait, stop_wait):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(dirty_wait, stop_wait, return_exceptions=True)
                if stop_wait in done:
                    return
                dirty.clear()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=FOLLOWER_EVENT_DEBOUNCE_S)
                    return
                except TimeoutError:
                    pass
                dirty.clear()
                await refresh()

        stream = self._user_stream_factory(
            client=runtime.client,
            account_id=runtime.account_id,
            ws_base=self.app.cfg.ws_user_base,
            keepalive_min=self.app.cfg.listenkey_keepalive_min,
            on_event=on_event,
            on_reconnect=on_reconnect,
            on_connected=on_connected,
            on_disconnected=on_disconnected,
        )
        stream_task = asyncio.create_task(stream.run(stop))
        refresh_task = asyncio.create_task(refresh_worker())
        try:
            await asyncio.gather(stream_task, refresh_task)
        finally:
            for task in (stream_task, refresh_task):
                task.cancel()
            await asyncio.gather(stream_task, refresh_task, return_exceptions=True)

    async def _run_market_stream(self) -> None:
        backoff = 1.0
        url = f"{self.app.cfg.ws_user_base.rstrip('/')}/!markPrice@arr@1s"
        while not self._stop.is_set():
            try:
                async with self._market_ws_factory(url) as ws:
                    backoff = 1.0
                    self._market_live = True
                    self._publish_all_health()
                    while not self._stop.is_set():
                        receive = asyncio.create_task(ws.recv())
                        stopping = asyncio.create_task(self._stop.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {receive, stopping}, return_when=asyncio.FIRST_COMPLETED
                            )
                        finally:
                            # Always consume both child tasks. During shutdown the
                            # websocket can finish recv() with ConnectionClosedOK
                            # in the same turn that the stop waiter wins; merely
                            # calling cancel() then leaves the finished exception
                            # unobserved ("Task exception was never retrieved").
                            for task in (receive, stopping):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(receive, stopping, return_exceptions=True)
                        if stopping in done:
                            return
                        prices = _mark_prices(receive.result(), set(self.app.cfg.symbol_whitelist))
                        self.app.follower_snapshots.apply_mark_prices(prices)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("mark-price stream failed class=%s", type(exc).__name__)
            finally:
                self._market_live = False
                self._publish_all_health()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except TimeoutError:
                pass
            backoff = min(backoff * 2, MARKET_BACKOFF_MAX_S)

    def _publish_all_health(self) -> None:
        for account_id in self._private_health:
            self._publish_health(account_id)

    def _publish_health(self, account_id: str) -> None:
        health = (
            "live" if self._private_health.get(account_id) and self._market_live else "reconnecting"
        )
        self.hub.publish_health(account_id, health)


__all__ = ["LiveGrant", "LiveTicketStore", "MiniAppLiveHub", "MiniAppLiveRuntime"]
