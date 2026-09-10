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
LIVE_MARK_ACK_TIMEOUT_S = 10.0
MARK_FALLBACK_INTERVAL_S = 3.0
MARK_FALLBACK_FRESH_S = 12.0
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
        # A per-process epoch makes the revision counter interpretable across a
        # backend restart: revisions restart at 0, so a client holding a higher
        # watermark from the previous process would otherwise reject every new
        # event as stale/out-of-order and latch "stale" until the counter caught
        # up. Opaque and secret-free; only used for client re-baselining.
        self._epoch = secrets.token_hex(8)

    def subscribe(self, account_id: str) -> tuple[asyncio.Queue[dict[str, Any]], dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_max)
        self._queues.setdefault(account_id, set()).add(queue)
        initial = {
            "type": "hello",
            "epoch": self._epoch,
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

    def publish_health(self, account_id: str, health: str) -> bool:
        """Publish a changed health verdict; False when it was already current."""
        if health not in {"connecting", "live", "reconnecting"}:
            raise ValueError("invalid live health")
        if self._health.get(account_id) == health:
            return False
        self._health[account_id] = health
        self._publish(account_id, {"type": "health", "health": health})
        return True

    def _publish(self, account_id: str, payload: dict[str, Any]) -> None:
        revision = self._revisions.get(account_id, 0) + 1
        self._revisions[account_id] = revision
        event = {**payload, "revision": revision, "epoch": self._epoch}
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
        self._fallback_task: asyncio.Task[None] | None = None
        self._followers: dict[str, _FollowerHandle] = {}
        self._private_health: dict[str, bool] = {}
        self._market_live = False
        self._mark_fallback_at = 0.0
        self._quotes_live = False
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
        self._fallback_task = asyncio.create_task(
            self._run_mark_fallback(), name="miniapp-mark-price-fallback"
        )

    async def stop(self) -> None:
        self._stop.set()
        tasks = [
            task
            for task in (self._supervisor_task, self._market_task, self._fallback_task)
            if task is not None
        ]
        self._supervisor_task = None
        self._market_task = None
        self._fallback_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for account_id in tuple(self._followers):
            await self._stop_follower(account_id)
        self._unsubscribe()

    async def issue_ticket(self, user_id: int) -> dict[str, Any] | None:
        row = await self._users.get(user_id)
        if row is None or not int(row["access_enabled"]) or row["account_id"] is None:
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
        # The documented raw path (<ws>/<stream>) is not served by the Aster
        # testnet cluster: the socket opens and then stays silent forever, so
        # the stream is established through the SUBSCRIBE protocol, which the
        # same endpoint acknowledges. Liveness is only claimed once the
        # exchange actually produced a frame, so a silent half-open socket
        # cannot masquerade as a healthy presentation stream.
        url = self.app.cfg.ws_user_base.rstrip("/")
        params = [f"{symbol.lower()}@markPrice@1s" for symbol in self.app.cfg.symbol_whitelist]
        while not self._stop.is_set():
            try:
                async with self._market_ws_factory(url) as ws:
                    backoff = 1.0
                    await ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": 1}))
                    deadline = time.monotonic() + LIVE_MARK_ACK_TIMEOUT_S
                    while not self._stop.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("mark-price stream produced no frame")
                        receive = asyncio.create_task(ws.recv())
                        stopping = asyncio.create_task(self._stop.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {receive, stopping},
                                timeout=remaining,
                                return_when=asyncio.FIRST_COMPLETED,
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
                        if receive not in done:
                            raise TimeoutError("mark-price stream produced no frame")
                        prices = _mark_prices(receive.result(), set(self.app.cfg.symbol_whitelist))
                        if prices:
                            self.app.follower_snapshots.apply_mark_prices(prices)
                            # Only a frame that actually carries a mark price
                            # proves the stream delivers quotes. The SUBSCRIBE
                            # acknowledgement is a frame too, so claiming
                            # liveness on it idles the REST fallback while PnL
                            # freezes, then drops health on every 10s timeout.
                            if not self._market_live:
                                self._market_live = True
                                self._refresh_health()
                        deadline = time.monotonic() + LIVE_MARK_ACK_TIMEOUT_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # While quotes are still covered elsewhere the reconnect is a
                # background detail; only a real quote outage is worth a warning.
                if self._quotes_live:
                    log.debug("mark-price stream failed class=%s", type(exc).__name__)
                else:
                    log.warning("mark-price stream failed class=%s", type(exc).__name__)
            finally:
                self._market_live = False
                self._refresh_health()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except TimeoutError:
                pass
            backoff = min(backoff * 2, MARKET_BACKOFF_MAX_S)

    async def _run_mark_fallback(self) -> None:
        """Keep presentation quotes alive when the cluster pushes no mark frames.

        The shared mark-price stream stays primary: while it delivers frames
        this loop only sleeps. The Aster testnet cluster accepts the mark
        subscription and then stays silent, so a bounded unsigned REST read
        fills the same presentation-only quote path. Quotes never touch trading
        state and never refresh the REST ``captured_at`` age.
        """
        while not self._stop.is_set():
            if not self._market_live:
                try:
                    prices = await self._fetch_mark_prices()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._quotes_live:
                        log.debug("mark-price fallback failed class=%s", type(exc).__name__)
                    else:
                        log.warning("mark-price fallback failed class=%s", type(exc).__name__)
                else:
                    if prices:
                        self._mark_fallback_at = time.monotonic()
                        self.app.follower_snapshots.apply_mark_prices(prices)
                # Re-evaluate on every silent iteration so a fallback outage
                # demotes health once the last accepted quote ages out.
                self._refresh_health()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=MARK_FALLBACK_INTERVAL_S)
                return
            except TimeoutError:
                pass

    async def _fetch_mark_prices(self) -> dict[str, Decimal]:
        client = getattr(self.app, "master_client", None)
        fetch = getattr(client, "mark_prices", None)
        if fetch is None:
            return {}
        return await fetch(list(self.app.cfg.symbol_whitelist))

    def _refresh_health(self) -> None:
        """Publish ``live`` only while exchange quotes are actually flowing."""
        fresh = time.monotonic() - self._mark_fallback_at <= MARK_FALLBACK_FRESH_S
        live = self._market_live or fresh
        if live == self._quotes_live:
            return
        self._quotes_live = live
        self._publish_all_health()

    def _publish_all_health(self) -> None:
        for account_id in self._private_health:
            self._publish_health(account_id)

    def _publish_health(self, account_id: str) -> None:
        health = (
            "live" if self._private_health.get(account_id) and self._quotes_live else "reconnecting"
        )
        if self.hub.publish_health(account_id, health) and health != "live":
            # One line per real transition: without it, a user-visible flap is
            # impossible to attribute (private stream blip vs quote fallback gap).
            log.info(
                "live health dropped account_id=%s private=%s quotes=%s",
                account_id,
                self._private_health.get(account_id),
                self._quotes_live,
            )


__all__ = ["LiveGrant", "LiveTicketStore", "MiniAppLiveHub", "MiniAppLiveRuntime"]
