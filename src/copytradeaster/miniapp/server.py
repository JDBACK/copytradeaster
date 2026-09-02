"""Mini App aiohttp transport (Stage 3 part 1).

One process serves both the Telegram bot (polling) and the Mini App: this
module mounts a small JSON API plus the built Vite static bundle under
``/miniapp/``.  Every API request must carry the signed Telegram ``initData``
string; it is HMAC-verified server-side (``aiogram.utils.web_app``), its
``auth_date`` must be fresh, and the user id is derived from it — the client's
claimed identity is never trusted.

Local development without a Telegram context is possible through an explicit
fail-closed dev mode (``CT_MINIAPP_DEV_UNSIGNED``): it only accepts a declared
dev user id header, only on a testnet REST base URL, and only when the boot
settings configure both.  Every other request is rejected exactly as before.

Handlers only call the shared ``UserApplicationService`` use cases; no Aster
or storage imports live here.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from copytradeaster.application.models import (
    ActivityFilter,
    ApplicationError,
    ApplicationProtocol,
    AuthorizationError,
    Confirmation,
    ConfirmationAction,
    ConfirmationExpiredError,
    NotReadyError,
    RiskUpdateInput,
    ServiceUnavailableError,
)
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger
from copytradeaster.onboarding.errors import OnboardingErrorCode

log = get_logger(__name__)

MAX_BODY_BYTES = 64 * 1024
INIT_DATA_HEADER = "X-Telegram-Init-Data"
DEV_USER_HEADER = "X-Dev-User-Id"
TESTNET_REST_HOST = "fapi.asterdex-testnet.com"

ALLOWED_CLIENT_EVENTS = frozenset(
    {
        "wallet_connect_failed",
        "prepare_failed",
        "sign_failed",
        "submit_failed",
        "unknown",
    }
)

MSG_INVALID_SESSION = "Сессия Telegram недействительна — откройте мини-ап из чата с ботом."
MSG_BAD_BODY = "Некорректный формат запроса."
MSG_UNAVAILABLE = "Сервис временно недоступен, попробуйте позже."
MSG_ACTION_FAILED = "Действие не выполнено, попробуйте ещё раз."
MSG_ONBOARDING_FAILED = "Подключение не завершено — следуйте подсказке."


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unserializable DTO value: {type(value).__name__}")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_dto(value: Any) -> web.Response:
    """Serialize a transport-neutral dataclass DTO to a JSON response."""
    return web.json_response(asdict(value), dumps=_json_dumps)


def _json_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _error_code(exc: BaseException) -> str:
    """Typed public code: onboarding enum codes win over the generic class tag."""
    code = getattr(exc, "code", None)
    if isinstance(code, OnboardingErrorCode):
        return code.value
    return str(getattr(exc, "public_code", None) or type(exc).__name__)


def _error_status(exc: BaseException) -> int:
    if isinstance(exc, ConfirmationExpiredError):
        return 409
    if isinstance(exc, AuthorizationError):
        return 403
    if isinstance(exc, (ServiceUnavailableError, NotReadyError)):
        return 503
    if isinstance(exc, ApplicationError):
        # ValidationError, OnboardingApplicationError, StaleOperationError:
        # typed, safe-to-display classifications with remediation copy.
        return 400
    return 503


def _dev_mode_allowed(settings: Settings) -> bool:
    """The unsigned dev session may only ever exist against the testnet."""
    host = (urlsplit(settings.rest_base_url).hostname or "").lower()
    return settings.miniapp_dev_unsigned and host == TESTNET_REST_HOST


def create_miniapp_app(
    settings: Settings,
    application: ApplicationProtocol,
) -> web.Application:
    """Build the Mini App aiohttp application (API + static)."""

    async def _identity(request: web.Request) -> int | None:
        """Resolve the server-derived user id, or None for any untrusted input."""
        if _dev_mode_allowed(settings) and settings.miniapp_dev_user_id is not None:
            dev_user = request.headers.get(DEV_USER_HEADER, "").strip()
            if dev_user == str(settings.miniapp_dev_user_id):
                return settings.miniapp_dev_user_id
        init_data = request.headers.get(INIT_DATA_HEADER, "")
        if not init_data:
            return None
        try:
            parsed = safe_parse_webapp_init_data(settings.telegram_token_value(), init_data)
        except ValueError:
            return None
        if parsed is None or parsed.user is None:
            return None
        auth_date = getattr(parsed, "auth_date", None)
        if isinstance(auth_date, datetime):
            auth_ts = int(auth_date.timestamp())
        elif isinstance(auth_date, int):
            auth_ts = auth_date
        else:
            return None
        now_s = int(time.time())
        if now_s + 60 < auth_ts or now_s - auth_ts > settings.miniapp_init_data_ttl_s:
            return None
        return int(parsed.user.id)

    def _unauthorized() -> web.Response:
        return _json_error(401, "invalid_session", MSG_INVALID_SESSION)

    def _failure(exc: Exception, message: str) -> web.Response:
        return _json_error(_error_status(exc), _error_code(exc), message)

    async def _body(request: web.Request) -> dict | None:
        try:
            raw = await request.json(loads=json.loads)
        except Exception:  # noqa: BLE001 - any body decode failure is a 400
            return None
        return raw if isinstance(raw, dict) else None

    async def session(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        try:
            return _json_dto(await application.miniapp_session(user_id))
        except Exception as exc:  # noqa: BLE001 - typed application errors carry public_code
            return _failure(exc, MSG_UNAVAILABLE)

    async def dashboard(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        refresh = (bool(body) and bool(body.get("refresh"))) or request.query.get("refresh") in {
            "1",
            "true",
        }
        try:
            return _json_dto(await application.dashboard(user_id, refresh=refresh))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_UNAVAILABLE)

    async def positions(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        refresh = (bool(body) and bool(body.get("refresh"))) or request.query.get("refresh") in {
            "1",
            "true",
        }
        try:
            return _json_dto(await application.positions(user_id, refresh=refresh))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_UNAVAILABLE)

    async def activity(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request) or {}
        page_raw = body.get("page", 0)
        if isinstance(page_raw, bool) or not isinstance(page_raw, int) or page_raw < 0:
            return _json_error(400, "invalid_input", "Некорректный номер страницы журнала.")
        filter_raw = body.get("filter", "all")
        if not isinstance(filter_raw, str) or filter_raw not in {
            value.value for value in ActivityFilter
        }:
            return _json_error(400, "invalid_input", "Некорректный фильтр журнала.")
        try:
            return _json_dto(await application.activity(user_id, page=page_raw, filter=filter_raw))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_UNAVAILABLE)

    async def notifications(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        try:
            return _json_dto(await application.notification_settings(user_id))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_UNAVAILABLE)

    async def notifications_set(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        enabled = body.get("enabled") if body else None
        # Strict real-boolean boundary: missing/string/number are all invalid.
        if not isinstance(enabled, bool):
            return _json_error(400, "invalid_input", "Некорректное значение переключателя.")
        try:
            return _json_dto(await application.set_notifications(user_id, enabled))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ACTION_FAILED)

    async def risk(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        try:
            return _json_dto(await application.risk(user_id))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_UNAVAILABLE)

    async def risk_set(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        # Strict single-key boundary: legacy max_leverage /
        # max_entry_deviation_pct and any other mutation field are
        # rejected, never silently ignored (fail closed on forgery).
        if set(body.keys()) != {"risk_percent"}:
            return _json_error(400, "invalid_input", "Некорректный формат запроса.")
        raw = body.get("risk_percent")
        # Decimals travel as strings; bool/None/object/array/numbers are invalid.
        if isinstance(raw, bool) or not isinstance(raw, str):
            return _json_error(400, "invalid_input", "Некорректное значение риска.")
        text = raw.strip()
        if not text:
            return _json_error(400, "invalid_input", "Некорректное значение риска.")
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError, AttributeError):
            return _json_error(400, "invalid_input", "Некорректное значение риска.")
        if not value.is_finite():
            return _json_error(400, "invalid_input", "Некорректное значение риска.")
        try:
            return _json_dto(
                await application.update_risk(user_id, RiskUpdateInput(risk_percent=value))
            )
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ACTION_FAILED)

    async def pause(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        try:
            return _json_dto(await application.pause(user_id))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ACTION_FAILED)

    def _parse_confirmation(raw: Any) -> Confirmation | None:
        if not isinstance(raw, dict):
            return None
        token = raw.get("token")
        action = raw.get("action")
        if not isinstance(token, str) or not token or not isinstance(action, str):
            return None
        try:
            parsed_action = ConfirmationAction(action)
        except ValueError:
            return None
        expires_at: datetime | None = None
        raw_expires = raw.get("expires_at")
        if isinstance(raw_expires, str) and raw_expires:
            try:
                expires_at = datetime.fromisoformat(raw_expires)
            except ValueError:
                return None
        account_id = raw.get("account_id")
        return Confirmation(
            token=token,
            action=parsed_action,
            account_id=account_id if isinstance(account_id, str) else None,
            expires_at=expires_at,
        )

    async def resume(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        confirmation = _parse_confirmation(body.get("confirmation")) if body else None
        if body and body.get("confirmation") is not None and confirmation is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        try:
            return _json_dto(await application.resume(user_id, confirmation))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, "Возобновление не выполнено — подтвердите действие.")

    async def confirmation_begin(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        raw_action = body.get("action")
        try:
            action = ConfirmationAction(raw_action) if isinstance(raw_action, str) else None
        except ValueError:
            action = None
        if action is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        try:
            confirmation = await application.begin_confirmation(user_id, action)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ACTION_FAILED)
        return _json_dto(confirmation)

    async def confirmation_confirm(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        confirmation = _parse_confirmation(body.get("confirmation"))
        if confirmation is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        try:
            return _json_dto(await application.confirm_action(user_id, confirmation))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ACTION_FAILED)

    async def client_log(request: web.Request) -> web.Response:
        """Diagnostics channel: client-side failures land in the operator log."""
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        level = str(body.get("level") or "info").lower()
        if level not in {"info", "warning", "error"}:
            level = "info"
        raw_event = str(body.get("event") or "unknown").strip().lower()
        event = raw_event if raw_event in ALLOWED_CLIENT_EVENTS else "unknown"
        raw_code = str(body.get("code") or "-").strip()
        code = "".join(c for c in raw_code if c.isprintable() and c not in "\r\n\t")[:50]
        raw_detail = str(body.get("detail") or "").strip()
        detail = "".join(c for c in raw_detail if c.isprintable() and c not in "\r\n\t")[:100]
        log.warning(
            "client_event user=%s level=%s event=%s code=%s detail=%s",
            user_id,
            level,
            event,
            code,
            detail,
        )
        return web.json_response({"ok": True})

    async def builder_prepare(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        user_address = str(body.get("user_address") or "")
        try:
            return _json_dto(await application.builder_prepare(user_id, user_address))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ONBOARDING_FAILED)

    async def builder_submit(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        body = await _body(request)
        if body is None:
            return _json_error(400, "invalid_input", MSG_BAD_BODY)
        pending_id = str(body.get("pending_id") or "")
        signature = str(body.get("signature") or "")
        try:
            return _json_dto(await application.builder_submit(user_id, pending_id, signature))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ONBOARDING_FAILED)

    async def builder_resume_attempt(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        try:
            return _json_dto(await application.builder_resume_attempt(user_id))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, MSG_ONBOARDING_FAILED)

    async def builder_recover(request: web.Request) -> web.Response:
        user_id = await _identity(request)
        if user_id is None:
            return _unauthorized()
        try:
            return _json_dto(await application.miniapp_recover_runtime(user_id))
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, "Восстановление запуска не выполнено — попробуйте ещё раз.")

    @web.middleware
    async def security_headers(request: web.Request, handler) -> web.StreamResponse:
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.path.startswith("/miniapp/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.path.startswith("/miniapp"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[security_headers])
    app.router.add_post("/miniapp/api/session", session)
    app.router.add_post("/miniapp/api/client-log", client_log)
    app.router.add_post("/miniapp/api/dashboard", dashboard)
    app.router.add_post("/miniapp/api/positions", positions)
    app.router.add_post("/miniapp/api/activity", activity)
    app.router.add_post("/miniapp/api/notifications", notifications)
    app.router.add_post("/miniapp/api/notifications/enabled", notifications_set)
    app.router.add_post("/miniapp/api/risk", risk)
    app.router.add_post("/miniapp/api/risk/percent", risk_set)
    app.router.add_post("/miniapp/api/pause", pause)
    app.router.add_post("/miniapp/api/resume", resume)
    app.router.add_post("/miniapp/api/confirmation/begin", confirmation_begin)
    app.router.add_post("/miniapp/api/confirmation/confirm", confirmation_confirm)
    app.router.add_post("/miniapp/api/builder/prepare", builder_prepare)
    app.router.add_post("/miniapp/api/builder/submit", builder_submit)
    app.router.add_post("/miniapp/api/builder/resume-attempt", builder_resume_attempt)
    app.router.add_post("/miniapp/api/builder/recover", builder_recover)

    static_dir: Path | None = settings.miniapp_static_dir
    if static_dir is not None and Path(static_dir).is_dir():

        async def index(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(Path(static_dir) / "index.html")  # type: ignore[arg-type]

        async def root_redirect(_request: web.Request) -> web.Response:
            raise web.HTTPFound("/miniapp/")

        # Route order matters: aiohttp resolves resources in registration
        # order, so the index handlers must be added before the static mount
        # or the static router answers /miniapp/ with 403 (no directory
        # listing).
        app.router.add_get("/", root_redirect)
        app.router.add_get("/miniapp", index)
        app.router.add_get("/miniapp/", index)
        app.router.add_static("/miniapp/", Path(static_dir))
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
        import contextlib

        with contextlib.suppress(Exception):
            await self._runner.cleanup()
        self._runner = None


__all__ = ["MiniAppServer", "create_miniapp_app"]
