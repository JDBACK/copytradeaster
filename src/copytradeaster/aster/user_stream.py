"""Master user-data WebSocket stream.

Lifecycle: start_listen_key -> connect <ws_base>/<listenKey> -> events to
on_event. Keepalive PUT every `keepalive_min` (default 50 < 60 min TTL);
proactive reconnect after 23h (24h limit); on 'listenKeyExpired' or any
disconnect: fresh listenKey -> reconnect with backoff 1s..30s + jitter.
The `websockets` library handles ping/pong frames itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from websockets.asyncio.client import connect as default_ws_factory

from copytradeaster.aster.trading_api import AsterClient
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)

MAX_SESSION_S = 23 * 3600.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
#: A session must stay up this long before it counts as proof that the
#: endpoint is healthy and the reconnect backoff may restart at the base.
STABLE_SESSION_S = 30.0


class StreamEvent(NamedTuple):
    raw: dict
    received_at: float


class UserStream:
    def __init__(
        self,
        client: AsterClient,
        account_id: str,
        ws_base: str,
        keepalive_min: int,
        on_event: Callable[[StreamEvent], Awaitable[None]],
        on_reconnect: Callable[[], Awaitable[None]],
        *,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        on_disconnected: Callable[[], Awaitable[None]] | None = None,
        ws_factory: Callable[[str], Any] | None = None,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        max_session_s: float = MAX_SESSION_S,
        stable_session_s: float = STABLE_SESSION_S,
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._ws_base = ws_base.rstrip("/")
        self._keepalive_s = keepalive_min * 60
        self._on_event = on_event
        self._on_reconnect = on_reconnect
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._ws_factory = ws_factory or default_ws_factory
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._max_session_s = max_session_s
        self._stable_session_s = stable_session_s

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self._backoff_base
        first = True
        while not stop.is_set():
            connected = False
            disconnected_notified = False
            opened_at: float | None = None
            try:
                listen_key = await self._client.start_listen_key(self._account_id)
                url = f"{self._ws_base}/{listen_key}"
                async with self._ws_factory(url) as ws:
                    opened_at = time.monotonic()
                    if not first:
                        await self._safe_on_reconnect()
                    first = False
                    connected = True
                    # Start recv before the authoritative connected callback.
                    # Frames already buffered by Aster are therefore queued and
                    # tagged with the pre-rebuild generation while the callback
                    # obtains the post-connect REST snapshot.
                    session_task = asyncio.create_task(self._session(ws, stop))
                    boundary_task: asyncio.Task[None] | None = None
                    try:
                        if self._on_connected is not None:
                            boundary_task = asyncio.create_task(self._on_connected())
                            done, _ = await asyncio.wait(
                                {session_task, boundary_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if session_task in done and not boundary_task.done():
                                if stop.is_set():
                                    return
                                # Do not let a slow post-connect REST boundary
                                # open entry admission after the socket already
                                # died. App observes disconnect before the
                                # boundary callback reaches its admission check.
                                await self._safe_on_disconnected()
                                disconnected_notified = True
                            await boundary_task
                        await session_task
                    finally:
                        if boundary_task is not None:
                            boundary_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await boundary_task
                        session_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await session_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop boundary
                log.warning("user stream error: %s: %s", type(exc).__name__, exc, exc_info=True)
            finally:
                if connected and not disconnected_notified and not stop.is_set():
                    await self._safe_on_disconnected()
                # Resetting the backoff on the handshake alone let a socket that
                # dies or expires right after connect buy a fresh one every base
                # interval, forever: 1s -> 1s -> 1s instead of 1 -> 2 -> 4 -> 30.
                # Each such accepted reconnect also re-runs the Mini App's
                # authoritative follower snapshot, so the ladder is the bound.
                if opened_at is not None and time.monotonic() - opened_at >= self._stable_session_s:
                    backoff = self._backoff_base
            if stop.is_set():
                break
            jitter = random.uniform(0, backoff * 0.3)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff + jitter)
                break
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self._backoff_max)
        log.info("user stream stopped")

    async def _session(self, ws: Any, stop: asyncio.Event) -> None:
        keepalive = asyncio.create_task(self._keepalive_loop(stop))
        deadline = asyncio.get_running_loop().time() + self._max_session_s
        try:
            while not stop.is_set():
                timeout = max(deadline - asyncio.get_running_loop().time(), 0.1)
                recv_task = asyncio.ensure_future(ws.recv())
                stop_task = asyncio.ensure_future(stop.wait())
                try:
                    done, _ = await asyncio.wait(
                        {recv_task, stop_task},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # A close can race the stop waiter and complete recv() with
                    # ConnectionClosedOK after asyncio.wait() wakes. Await every
                    # child, including already-finished ones, so shutdown never
                    # leaks an unobserved recv exception.
                    for task in (recv_task, stop_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(recv_task, stop_task, return_exceptions=True)
                if stop_task in done:
                    return
                if recv_task not in done:
                    log.info("proactive reconnect after max session time")
                    return
                try:
                    message = recv_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - transport-level failure
                    log.warning("recv failed: %s", exc)
                    return
                if isinstance(message, bytes):
                    message = message.decode()
                try:
                    raw = json.loads(message)
                except ValueError:
                    log.warning("non-json stream frame dropped")
                    continue
                if not isinstance(raw, dict):
                    continue
                if raw.get("e") == "listenKeyExpired":
                    log.warning("listenKeyExpired - rotating listen key")
                    return
                await self._on_event(StreamEvent(raw=raw, received_at=time.monotonic()))
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive

    async def _keepalive_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._keepalive_s)
                return
            except TimeoutError:
                pass
            try:
                await self._client.keepalive_listen_key(self._account_id)
                log.debug("listenKey keepalive sent")
            except Exception as exc:  # noqa: BLE001 - keepalive must not kill session
                log.warning("listenKey keepalive failed: %s", exc)

    async def _safe_on_reconnect(self) -> None:
        try:
            await self._on_reconnect()
        except Exception as exc:  # noqa: BLE001
            log.error("on_reconnect handler failed: %s", exc)

    async def _safe_on_disconnected(self) -> None:
        if self._on_disconnected is None:
            return
        try:
            await self._on_disconnected()
        except Exception as exc:  # noqa: BLE001
            log.error("on_disconnected handler failed: %s", exc)
