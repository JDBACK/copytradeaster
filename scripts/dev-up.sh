#!/usr/bin/env bash
# One-command launch of the full local instance: Mini App bundle -> cloudflared
# quick tunnel -> CT_MINIAPP_PUBLIC_URL -> production process (Telegram bot +
# Mini App transport + trading core) on Aster Testnet.
#
# The plain path is deliberately reuse-first: when the bot needs a restart but
# the current tunnel is still alive, keep that tunnel and its hostname. A
# Telegram menu button may cache the previous hostname for minutes; rotating a
# healthy tunnel here would create a deterministic Error 1033 window.
#
# Usage:
#   scripts/dev-up.sh          # reuse stack/tunnel when possible, else restart
#   scripts/dev-up.sh --rotate # force a fresh tunnel URL (invalidates the old
#                              # one: the Telegram menu button needs minutes to
#                              # propagate; use only when the current tunnel died)
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
TUNNEL_PID_FILE="data/cloudflared.pid"

mkdir -p data/logs

stop_instance() {
  local keep_tunnel="${1:-0}"
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
  pkill -f "copytradeaster run-bot" 2>/dev/null || true
  if [ "$keep_tunnel" != "1" ]; then
    if [ -f "$TUNNEL_PID_FILE" ]; then
      kill "$(cat "$TUNNEL_PID_FILE")" 2>/dev/null || true
      rm -f "$TUNNEL_PID_FILE"
    fi
    pkill -f "[c]loudflared tunnel --url http://127.0.0.1:${PORT}" 2>/dev/null || true
  fi
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

tunnel_process_alive() {
  local pid command
  if [ -f "$TUNNEL_PID_FILE" ]; then
    pid="$(cat "$TUNNEL_PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      printf '%s' "$command" | grep -Fq "cloudflared tunnel --url http://127.0.0.1:${PORT}" && return 0
    fi
  fi
  # The bracket in [c]loudflared prevents pgrep from matching its own command.
  pgrep -f "[c]loudflared tunnel --url http://127.0.0.1:${PORT}" >/dev/null 2>&1
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

wait_public() {
  # A transient edge 502 must never trigger tunnel rotation: Telegram may
  # still have the current hostname cached. Keep the process and hostname,
  # and give the existing connector up to 90 seconds to recover.
  local url="$1"
  for _ in $(seq 1 24); do
    if edge_check "${url}/miniapp/"; then
      return 0
    fi
    if ! tunnel_process_alive; then
      return 1
    fi
    sleep 5
  done
  return 1
}

stack_alive() {
  # Process serving path, regardless of transient edge state: bot + tunnel
  # processes and the local bundle answering. The caller checks the public
  # edge separately so a short 502 cannot rotate away a still-live hostname.
  local url
  url="$(current_public_url)"
  [ -n "$url" ] || return 1
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null || return 1
  tunnel_process_alive || return 1
  curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/miniapp/" >/dev/null 2>&1 || return 1
}

if [ "$MODE" = "reuse" ] && stack_alive; then
  echo "==> Stack processes alive on $(current_public_url)/miniapp/ — reusing (no URL rotation)"
  echo "==> Checking the existing public edge (no tunnel rotation on transient 502)"
  if ! wait_public "$(current_public_url)"; then
    echo "ERROR: existing tunnel is not serving the Mini App after ~90s." >&2
    echo "  Tunnel and bot were left running; retry without --rotate." >&2
    exit 1
  fi
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

CURRENT_URL="$(current_public_url)"
KEEP_TUNNEL=0
if [ "$MODE" = "reuse" ] && [ -n "$CURRENT_URL" ] && tunnel_process_alive; then
  KEEP_TUNNEL=1
fi

if [ "$KEEP_TUNNEL" = "1" ]; then
  echo "==> Restarting bot while keeping live tunnel ${CURRENT_URL}"
  stop_instance 1
else
  echo "==> Stopping previous instance (if any)"
  stop_instance
fi

echo "==> Checking Mini App bundle freshness"
if [ ! -f miniapp/dist/index.html ] || [ -n "$(find miniapp/src miniapp/index.html miniapp/vite.config.ts \
    miniapp/tsconfig.app.json miniapp/package.json -newer miniapp/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "    sources changed -> building bundle"
  (cd miniapp && pnpm build)
else
  echo "    bundle is fresh (skip build)"
fi

if [ "$KEEP_TUNNEL" = "1" ]; then
  TUNNEL_URL="$CURRENT_URL"
  echo "==> Reusing existing cloudflared tunnel ${TUNNEL_URL}"
else
  echo "==> Starting cloudflared quick tunnel (port ${PORT})"
  # Never parse a hostname from an earlier quick-tunnel generation. The
  # hostname in the first log line may already be dead while a new connector
  # is starting, which would publish a guaranteed Error 1033 URL to Telegram.
  : > "$TUNNEL_LOG"
  .venv/bin/python scripts/detach_process.py \
    --pid-file "$TUNNEL_PID_FILE" \
    --log-file "$TUNNEL_LOG" \
    -- cloudflared tunnel --url "http://127.0.0.1:${PORT}"
  TUNNEL_URL=""
  for _ in $(seq 1 30); do
    TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | tail -1 || true)"
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
fi

echo "==> Starting run-bot (Telegram bot + Mini App + trading core)"
BOT_PID="$(.venv/bin/python scripts/detach_process.py \
  --pid-file "$PID_FILE" \
  --log-file "$BOT_LOG" \
  -- .venv/bin/python -m copytradeaster run-bot)"

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
echo "==> Waiting for public tunnel URL (edge warm-up can take up to two minutes)"
PUBLIC_OK=""
for _ in $(seq 1 24); do
  if edge_check "${TUNNEL_URL}/miniapp/"; then
    PUBLIC_OK=1
    break
  fi
  if ! tunnel_process_alive; then
    echo "ERROR: cloudflared died while waiting - tail of ${TUNNEL_LOG}:" >&2
    tail -20 "$TUNNEL_LOG" >&2
    exit 1
  fi
  sleep 5
done
if [ -z "$PUBLIC_OK" ]; then
  echo "ERROR: public tunnel URL is not serving the Mini App after ~120s." >&2
  echo "Instance LEFT RUNNING; READY is withheld until the public edge answers." >&2
  echo "  Local:  http://127.0.0.1:${PORT}/miniapp/" >&2
  echo "  Public: ${TUNNEL_URL}/miniapp/ (retry plain scripts/dev-up.sh; do not rotate)" >&2
  tail -20 "$TUNNEL_LOG" >&2
  exit 1
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
