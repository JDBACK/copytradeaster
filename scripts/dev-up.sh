#!/usr/bin/env bash
# One-command launch of the full local instance: Mini App bundle -> cloudflared
# quick tunnel -> CT_MINIAPP_PUBLIC_URL -> production process (Telegram bot +
# Mini App transport + trading core) on Aster Testnet.
#
# Usage:
#   scripts/dev-up.sh          # reuse the live stack when healthy, else restart
#   scripts/dev-up.sh --rotate # force a fresh tunnel URL (invalidates the old
#                              # one: the Telegram menu button needs minutes to
#                              # propagate, so prefer plain runs)
#   scripts/dev-up.sh stop     # stop the bot and the tunnel
#
# Rotation is expensive: a quick-tunnel hostname dies with its process, and a
# setChatMenuButton URL takes minutes to propagate on Telegram's side. A plain
# run therefore keeps a healthy stack untouched and only restarts what is down.
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

MODE="reuse"
if [ "${1:-}" = "stop" ]; then
  stop_instance
  echo "Stopped: bot + tunnel."
  exit 0
fi
if [ "${1:-}" = "--rotate" ]; then
  MODE="rotate"
elif [ -n "${1:-}" ]; then
  echo "Usage: scripts/dev-up.sh [--rotate] | stop" >&2
  exit 2
fi
[ -x .venv/bin/python ] || { echo "ERROR: .venv not found — run: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2; exit 1; }
command -v cloudflared >/dev/null || { echo "ERROR: cloudflared is not installed (brew install cloudflared)" >&2; exit 1; }

edge_check() {
  # True when the public URL serves the Mini App bundle through the edge.
  # Plain curl first; sandbox DNS cannot resolve external names, so fall back
  # to DoH resolution plus --resolve (SNI intact, edge IP pinned).
  local url="$1" body host edge_ip
  if body="$(curl -sk --max-time 10 "$url" 2>/dev/null)" && printf '%s' "$body" | grep -q 'id="root"'; then
    return 0
  fi
  host="$(printf '%s' "$url" | sed -E 's|^https?://([^/:]+).*|\1|')"
  edge_ip="$(curl -sk --max-time 10 "https://1.1.1.1/dns-query?name=${host}&type=A" -H 'accept: application/dns-json' 2>/dev/null | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' | head -1 || true)"
  [ -n "$edge_ip" ] || return 1
  body="$(curl -sk --max-time 10 --resolve "${host}:443:${edge_ip}" "$url" 2>/dev/null || true)"
  printf '%s' "$body" | grep -q 'id="root"'
}
current_public_url() {
  grep -E '^CT_MINIAPP_PUBLIC_URL=' .env 2>/dev/null | cut -d= -f2- || true
}

wait_button() {
  # Converge the Telegram menu button onto $1 (base URL, no trailing path).
  # setChatMenuButton applies with minutes of propagation lag: a MISMATCH
  # right after our own SET proves nothing (read-after-write lag), so repair
  # sparingly — a repeated SET every 30 s only burns Bot API calls without
  # speeding up Telegram-side propagation. Worst case is now ~68 calls
  # (reads + two repairs) instead of ~288.
  local url="$1" i
  for i in $(seq 1 24); do
    if .venv/bin/python scripts/verify_menu_button.py --expected-url "$url"; then
      return 0
    fi
    if [ "$i" -eq 1 ] || [ "$i" -eq 12 ]; then
      echo "    not converged — repairing button via Bot API and waiting 30s"
      .venv/bin/python scripts/verify_menu_button.py --expected-url "$url" --fix || true
    else
      echo "    not converged yet — waiting 30s (no API calls, propagation lag)"
    fi
    sleep 30
  done
  return 1
}

stack_alive() {
  # Live serving path, regardless of button state: bot + tunnel processes,
  # local bundle answering, public edge serving the current .env URL.
  local url
  url="$(current_public_url)"
  [ -n "$url" ] || return 1
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null || return 1
  pgrep -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" >/dev/null 2>&1 || return 1
  curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/miniapp/" >/dev/null 2>&1 || return 1
  edge_check "${url}/miniapp/" >/dev/null 2>&1 || return 1
}

if [ "$MODE" = "reuse" ] && stack_alive; then
  echo "==> Stack alive on $(current_public_url)/miniapp/ — reusing (no restart, no URL rotation)"
  if wait_button "$(current_public_url)"; then
    echo "READY — live stack already converged (nothing restarted)."
    echo "  Telegram:  open the bot chat -> menu button ASTER COPY"
    echo "  Stop:      scripts/dev-up.sh stop | force rotation: scripts/dev-up.sh --rotate"
    exit 0
  fi
  echo "ERROR: stack serves, but the menu button did not converge in ~12 minutes." >&2
  echo "  Tunnel left running — do NOT rotate; wait and re-run scripts/dev-up.sh." >&2
  exit 1
fi

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
  if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/miniapp/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/miniapp/" >/dev/null 2>&1; then
  echo "ERROR: miniapp did not come up — tail of ${BOT_LOG}:" >&2
  tail -40 "$BOT_LOG" >&2
  exit 1
fi
echo "==> Waiting for public tunnel URL (edge warm-up can take a minute)"
PUBLIC_OK=""
for _ in $(seq 1 18); do
  if edge_check "${TUNNEL_URL}/miniapp/"; then
    PUBLIC_OK=1
    break
  fi
  if ! pgrep -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" >/dev/null 2>&1; then
    echo "ERROR: cloudflared died while waiting - tail of ${TUNNEL_LOG}:" >&2
    tail -20 "$TUNNEL_LOG" >&2
    exit 1
  fi
  sleep 5
done
if [ -z "$PUBLIC_OK" ]; then
  if grep -q "Registered tunnel connection" "$TUNNEL_LOG" 2>/dev/null; then
    echo "WARNING: tunnel is registered but the edge is not serving it yet — edge"
    echo "  warm-up, not a stale button. Continuing to the button check; retry the"
    echo "  phone in a minute. Do NOT rerun (a rerun rotates the tunnel URL)."
  else
    echo "ERROR: public tunnel URL is not serving the Mini App after ~90s." >&2
    echo "Instance LEFT RUNNING (local Mini App may already work):" >&2
    echo "  Local:  http://127.0.0.1:${PORT}/miniapp/" >&2
    echo "  Public: ${TUNNEL_URL}/miniapp/ (retry manually, rerun script if needed)" >&2
    tail -20 "$TUNNEL_LOG" >&2
    exit 1
  fi
fi
echo "==> Waiting for the menu button to converge (Telegram propagates URL changes for minutes)"
if ! wait_button "${TUNNEL_URL}"; then
  echo "ERROR: menu button does not point at ${TUNNEL_URL}/miniapp/ after ~12 minutes." >&2
  echo "  The Mini App would open Error 1016 (stale tunnel URL)." >&2
  echo "  Tunnel left running — do NOT rerun for 10 minutes (a rerun rotates the URL);" >&2
  echo "  then run scripts/dev-up.sh --rotate for a fresh URL." >&2
  exit 1
fi

echo
echo "READY — one instance is running and healthy."
echo "  Telegram:  open the bot chat -> menu button ASTER COPY (${TUNNEL_URL}/miniapp/)"
echo "  If the phone shows Error 1016: force-kill the Telegram app (client-side button"
echo "    cache), reopen the chat and tap ASTER COPY again."
echo "  Local:     http://127.0.0.1:${PORT}/miniapp/"
echo "  Logs:      tail -f data/logs/copytradeaster.log | cat ${BOT_LOG}"
echo "  Stop:      scripts/dev-up.sh stop"
