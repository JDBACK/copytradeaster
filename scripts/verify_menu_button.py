"""Verify (and optionally repair) the Telegram menu button Mini App URL.

The dev stack exposes the Mini App through a cloudflared *quick* tunnel whose
public hostname changes on every restart. The bot registers its menu button at
boot, but that registration is best-effort: if it silently fails (or an older
per-chat override survives), the button keeps pointing at a dead hostname and
Telegram shows ``Error 1016 / Origin DNS error``. This helper closes the loop:
it reads back the server-side button state via ``getChatMenuButton`` and, with
``--fix``, rewrites any mismatched button to the live tunnel URL.

Usage:
    python scripts/verify_menu_button.py --expected-url <url> [--fix]

Configuration (never printed):
    CT_TELEGRAM_BOT_TOKEN, or a local ``.botkey`` file with a ``BOT_KEY=...``
    line; admin chat ids from CT_TELEGRAM_ADMIN_IDS (``[123]`` / ``123, 456``).

Exit 0: default button and every admin per-chat override match the expected
``.../miniapp/`` URL. Exit 1: any mismatch remains, or the Bot API failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_TIMEOUT_S = 15
BUTTON_TEXT = "ASTER COPY"


def _read_token() -> str | None:
    token = (os.environ.get("CT_TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        return token
    try:
        text = (ROOT / ".botkey").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "BOT_KEY" and value.strip():
            return value.strip()
    return None


def _env_file_value(key: str) -> str | None:
    try:
        text = (ROOT / ".env").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            return value.strip().strip("\"'")
    return None


def _admin_ids() -> list[int]:
    # dev-up.sh never sources .env into its own environment, so without the
    # file fallback the per-chat admin overrides would silently go unchecked.
    # Real env wins; values are never logged.
    raw = (os.environ.get("CT_TELEGRAM_ADMIN_IDS") or "").strip()
    if not raw:
        raw = _env_file_value("CT_TELEGRAM_ADMIN_IDS") or ""
    return [int(match) for match in re.findall(r"-?\d+", raw)]


def _normalize(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if not cleaned.endswith("/miniapp"):
        cleaned = f"{cleaned}/miniapp"
    return f"{cleaned}/"


def _api(token: str, method: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Bot API {method} not ok: {body}")
    return body["result"]


def _button_url(state: dict) -> str | None:
    if state.get("type") != "web_app":
        return None
    web_app = state.get("web_app") or {}
    return web_app.get("url")


def main(expected_url: str, fix: bool) -> int:
    token = _read_token()
    if not token:
        print("ERROR: no bot token (CT_TELEGRAM_BOT_TOKEN or .botkey)", file=sys.stderr)
        return 1
    expected = _normalize(expected_url)
    scopes: list[tuple[str, dict]] = [("default", {})]
    scopes.extend((f"chat {chat_id}", {"chat_id": chat_id}) for chat_id in _admin_ids())

    failed = False
    for label, params in scopes:
        try:
            state = _api(token, "getChatMenuButton", params)
        except Exception as exc:  # noqa: BLE001 — reported, never raised
            print(f"{label}: CHECK FAILED ({type(exc).__name__}: {exc})")
            failed = True
            continue
        current = _button_url(state)
        if current == expected:
            print(f"{label}: MATCH {current}")
            continue
        print(f"{label}: MISMATCH got {current!r} want {expected!r}")
        if not fix:
            failed = True
            continue
        try:
            _api(
                token,
                "setChatMenuButton",
                {
                    **params,
                    "menu_button": {
                        "type": "web_app",
                        "text": BUTTON_TEXT,
                        "web_app": {"url": expected},
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 — reported, never raised
            print(f"{label}: REPAIR FAILED ({type(exc).__name__}: {exc})")
            failed = True
            continue
        # The Bot API answers SET ok:true but a get immediately afterwards can
        # still return the previous value (read-after-write lag) — confirm
        # with retries instead of trusting the first read.
        confirmed = False
        for _ in range(4):
            time.sleep(5)
            try:
                state = _api(token, "getChatMenuButton", params)
            except Exception as exc:  # noqa: BLE001 — reported, never raised
                print(f"{label}: RECHECK FAILED ({type(exc).__name__}: {exc})")
                break
            if _button_url(state) == expected:
                confirmed = True
                break
        if confirmed:
            print(f"{label}: REPAIRED {expected}")
        else:
            print(f"{label}: REPAIR UNCONFIRMED got {_button_url(state)!r}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify the Mini App menu button URL.")
    parser.add_argument("--expected-url", required=True)
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.expected_url, args.fix))
