#!/usr/bin/env bash
# Stage 3 Mini App local dev: start a cloudflared quick tunnel in front of the
# local aiohttp transport and print the public HTTPS URL.  No account needed.
# The URL changes on every tunnel restart; update the BotFather menu button
# (or the inline WebApp keyboard URL) with the new value.
#
# Usage: scripts/dev-miniapp-tunnel.sh [local-port]
set -euo pipefail

PORT="${1:-8080}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install with: brew install cloudflared" >&2
  exit 1
fi

echo "Starting quick tunnel -> http://127.0.0.1:${PORT}"
echo "Copy the https://<random>.trycloudflare.com URL below into the Telegram"
echo "menu button / WebApp keyboard, then open the Mini App from the bot chat."
exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
