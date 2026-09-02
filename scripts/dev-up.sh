#!/usr/bin/env bash
# One-command launch of the full local instance: Mini App bundle -> cloudflared
# quick tunnel -> CT_MINIAPP_PUBLIC_URL -> production process (Telegram bot +
# Mini App transport + trading core) on Aster Testnet.
#
# Usage:
#   scripts/dev-up.sh          # restart everything and wait until healthy
#   scripts/dev-up.sh stop     # stop the bot and the tunnel
#
# The script is idempotent: it always kills the previous instance first, so
# exactly one bot and one tunnel are left running.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT=8080
TUNNEL_LOG="data/logs/cloudflared-tunnel.log"
BOT_LOG="data/logs/run-bot-console.log"
PID_FILE="data/run-bot.pid"

mkdir -p data/logs

stop_instance() {
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
  pkill -f "copytradeaster run-bot" 2>/dev/null || true
  pkill -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" 2>/dev/null || true
  sleep 1
}

if [ "${1:-}" = "stop" ]; then
  stop_instance
  echo "Stopped: bot + tunnel."
  exit 0
fi

[ -x .venv/bin/python ] || { echo "ERROR: .venv not found — run: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2; exit 1; }
command -v cloudflared >/dev/null || { echo "ERROR: cloudflared is not installed (brew install cloudflared)" >&2; exit 1; }

echo "==> Stopping previous instance (if any)"
stop_instance

echo "==> Checking Mini App bundle freshness"
if [ ! -f miniapp/dist/index.html ] || [ -n "$(find miniapp/src miniapp/index.html miniapp/vite.config.ts \
    miniapp/tsconfig.app.json miniapp/package.json -newer miniapp/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "    sources changed -> building bundle"
  (cd miniapp && pnpm build)
else
  echo "    bundle is fresh (skip build)"
fi

echo "==> Starting cloudflared quick tunnel (port ${PORT})"
nohup cloudflared tunnel --url "http://127.0.0.1:${PORT}" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_URL=""
for _ in $(seq 1 30); do
  TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
  [ -n "$TUNNEL_URL" ] && break
  sleep 1
done
if [ -z "$TUNNEL_URL" ]; then
  echo "ERROR: tunnel URL not received — tail of ${TUNNEL_LOG}:" >&2
  tail -20 "$TUNNEL_LOG" >&2
  exit 1
fi
echo "    public URL: ${TUNNEL_URL}"

echo "==> Writing CT_MINIAPP_PUBLIC_URL to .env"
sed -i '' "s|^CT_MINIAPP_PUBLIC_URL=.*|CT_MINIAPP_PUBLIC_URL=${TUNNEL_URL}|" .env
grep -q "^CT_MINIAPP_PUBLIC_URL=${TUNNEL_URL}$" .env || { echo "ERROR: failed to update .env" >&2; exit 1; }

echo "==> Starting run-bot (Telegram bot + Mini App + trading core)"
nohup .venv/bin/python -m copytradeaster run-bot > "$BOT_LOG" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"

echo "==> Waiting for health (miniapp on :${PORT} + bot process alive)"
for _ in $(seq 1 90); do
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "ERROR: run-bot exited — tail of ${BOT_LOG}:" >&2
    tail -40 "$BOT_LOG" >&2
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:${PORT}/miniapp/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:${PORT}/miniapp/" >/dev/null 2>&1; then
  echo "ERROR: miniapp did not come up — tail of ${BOT_LOG}:" >&2
  tail -40 "$BOT_LOG" >&2
  exit 1
fi
if ! curl -fsS "${TUNNEL_URL}/miniapp/" >/dev/null 2>&1; then
  echo "ERROR: public tunnel URL is not serving the Mini App — tail of ${TUNNEL_LOG}:" >&2
  tail -20 "$TUNNEL_LOG" >&2
  exit 1
fi

echo
echo "READY — one instance is running and healthy."
echo "  Telegram:  open the bot chat -> menu button ASTER COPY (${TUNNEL_URL}/miniapp/)"
echo "  Local:     http://127.0.0.1:${PORT}/miniapp/"
echo "  Logs:      tail -f data/logs/copytradeaster.log | cat ${BOT_LOG}"
echo "  Stop:      scripts/dev-up.sh stop"
