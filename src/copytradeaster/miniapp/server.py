"""Mini App aiohttp transport (Stage 3 part 1).

One process serves both the Telegram bot (polling) and the Mini App: this
module mounts a small JSON API plus the built Vite static bundle under
``/miniapp/``.  Every API request must carry the signed Telegram ``initData``
string; it is HMAC-verified server-side (``aiogram.utils.web_app``) and the
user id is derived from it — the client's claimed identity is never trusted.

Handlers only call the shared ``UserApplicationService`` use cases; no Aster
or storage imports live here.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from copytradeaster.application.models import ApplicationProtocol
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)

MAX_BODY_BYTES = 64 * 1024
INIT_DATA_HEADER = "X-Telegram-Init-Data"


def _json_dto(value: Any) -> web.Response:
    """Serialize a transport-neutral dataclass DTO to a JSON response."""
    return web.json_response(asdict(value))


def _json_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "public_code", None) or type(exc).__name__)


def create_miniapp_app(
    settings: Settings,
    application: ApplicationProtocol,
) -> web.Application:
    """Build the Mini App aiohttp application (API + static)."""

    async def _identity(request: web.Request) -> int | None:
        """HMAC-verify the initData header and return the server-derived user id."""
        init_data = request.headers.get(INIT_DATA_HEADER, "")
        if not init_data:
            return None
        try:
            parsed = safe_parse_webapp_init_data(settings.telegram_token_value(), init_data)
        except ValueError:
            return None
        if parsed is None or parsed.user is None:
            return None
        return int(parsed.user.id)

    async def session(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _json_error(401, "invalid_session", "Telegram 会话无效，请从聊天中重新打开。")
        try:
            return _json_dto(await application.miniapp_session(user_id))
        except Exception as exc:  # noqa: BLE001 - typed application errors carry public_code
            return _json_error(503, _error_code(exc), "服务暂时不可用，请稍后重试。")

    async def builder_prepare(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _json_error(401, "invalid_session", "Telegram 会话无效，请从聊天中重新打开。")
        try:
            body = await request.json(loads=json.loads)
        except Exception:  # noqa: BLE001
            return _json_error(400, "invalid_input", "请求格式无效。")
        user_address = str(body.get("user_address") or "") if isinstance(body, dict) else ""
        try:
            return _json_dto(await application.builder_prepare(user_id, user_address))
        except Exception as exc:  # noqa: BLE001
            return _json_error(400, _error_code(exc), "无法开始 Builder 授权，请稍后重试。")

    async def builder_submit(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _json_error(401, "invalid_session", "Telegram 会话无效，请从聊天中重新打开。")
        try:
            body = await request.json(loads=json.loads)
        except Exception:  # noqa: BLE001
            return _json_error(400, "invalid_input", "请求格式无效。")
        if not isinstance(body, dict):
            return _json_error(400, "invalid_input", "请求格式无效。")
        pending_id = str(body.get("pending_id") or "")
        signature = str(body.get("signature") or "")
        try:
            return _json_dto(await application.builder_submit(user_id, pending_id, signature))
        except Exception as exc:  # noqa: BLE001
            return _json_error(400, _error_code(exc), "授权未完成，请按提示重试。")

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/miniapp/api/session", session)
    app.router.add_post("/miniapp/api/builder/prepare", builder_prepare)
    app.router.add_post("/miniapp/api/builder/submit", builder_submit)

    static_dir: Path | None = settings.miniapp_static_dir
    if static_dir is not None and Path(static_dir).is_dir():

        async def index(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(Path(static_dir) / "index.html")  # type: ignore[arg-type]

        app.router.add_static("/miniapp/", Path(static_dir))
        app.router.add_get("/miniapp", index)
        app.router.add_get("/miniapp/", index)
    else:
        log.warning("miniapp static dir not configured or missing; API-only mode")

    return app


class MiniAppServer:
    """Bounded aiohttp AppRunner lifecycle for the Mini App transport."""

    def __init__(self, settings: Settings, application: ApplicationProtocol) -> None:
        self._settings = settings
        self._application = application
        self._runner: web.AppRunner | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._settings.miniapp_enabled)

    async def start(self) -> None:
        if not self.enabled:
            return
        app = create_miniapp_app(self._settings, self._application)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            self._settings.miniapp_bind_host,
            self._settings.miniapp_port,
        )
        await site.start()
        log.info(
            "miniapp server listening on http://%s:%s/miniapp",
            self._settings.miniapp_bind_host,
            self._settings.miniapp_port,
        )

    async def stop(self) -> None:
        if self._runner is None:
            return
        with __import__("contextlib").suppress(Exception):
            await self._runner.cleanup()
        self._runner = None


__all__ = ["MiniAppServer", "create_miniapp_app"]
