# Scripts Guidelines

Map of `scripts/`: what each script does, how to run it, which gates apply.
Read each script's usage and gates before running. A local process or backup
DB does not isolate exchange accounts: `dev-up.sh` starts the configured bot,
and `preview_miniapp_engine.py` boots a real testnet engine using copied
credentials. `verify_menu_button.py` uses Telegram's API; `--fix` writes remote
configuration. `check_doc_links.py` is an offline repository check.
`benchmark_history.py` is an offline synthetic probe using temporary SQLite.
Use existing task authorization for live operations; a docs/UI edit alone
does not require starting the trading stack. `__pycache__/` is noise, ignore it.

## Launch and preview (local processes, live services)

| Script | Purpose | Run |
|---|---|---|
| `dev-up.sh` | Full local stack in one command: reuse-first (plain run keeps a live bot/tunnel hostname even when the public edge has a transient 502, so Telegram never opens a dead rotated URL) → else bundle freshness check (`pnpm build` when `miniapp/src|index.html|vite.config.ts|tsconfig.app.json|package.json` outdate `miniapp/dist`) → detached `cloudflared tunnel --url http://127.0.0.1:8080` → writes `CT_MINIAPP_PUBLIC_URL` into `.env` (BSD sed, key must pre-exist) → detached `run-bot` → local `/miniapp/` (90×1s) → public edge wait via `edge_check` (plain curl, DoH+`--resolve` fallback for DNS-less sandboxes, 24×5s; no `READY` until the public edge serves the bundle, `id="root"`) → menu-button convergence wait `wait_button` (`verify_menu_button.py`, 24×30s: `setChatMenuButton` propagates for minutes, never trust the first read after a write). `stop` kills both; `--rotate` is the explicit fresh-URL escape hatch. Logs in `data/logs/`, PIDs in `data/run-bot.pid` and `data/cloudflared.pid` | `./scripts/dev-up.sh` (reuse) / `./scripts/dev-up.sh --rotate` / `./scripts/dev-up.sh stop` |
| `detach_process.py` | Small stdlib launcher used by `dev-up.sh`: starts one command in a new session, redirects its output to a log, and records its PID so the bot/tunnel survive the terminal or Codex command that started them | internal helper; do not run directly |
| `verify_menu_button.py` | Bot-API convergence check for the Mini App menu button (stdlib only): `getChatMenuButton` for default + every `CT_TELEGRAM_ADMIN_IDS` per-chat override vs expected `.../miniapp/` URL; `--fix` rewrites mismatches via `setChatMenuButton` and re-confirms with retries (read-after-write lag). Token from `CT_TELEGRAM_BOT_TOKEN`/`.botkey`, never printed. Exit 0 all match, 1 otherwise | `.venv/bin/python scripts/verify_menu_button.py --expected-url <url> [--fix]` |
| `preview_miniapp_engine.py` | Visual Mini App preview on the full engine: point-in-time copy of prod DB via SQLite backup API (`data/copytrade.sqlite3` → `data/miniapp-preview.sqlite3`, prod untouched) → boots a real `App` + `MiniAppServer` → unsigned dev session (`PREVIEW_USER_ID=900_000_001`, bound to `--account`). Fail-closed: testnet URLs only | `python scripts/preview_miniapp_engine.py --account <ID> --confirm-engine-preview [--port 8085]` |
| `audit_miniapp_browser.mjs` | Real-Chromium acceptance over CDP (Node, no npm deps) against the *running* bot: `smoke` (what the surface shows), `swipe` (dispatched touch pull: indicator phases, the measured gap it opens, Telegram swipe handoff, the `refresh=true` reads it fires, and the scrolled-away guard on a genuinely scrollable page), `series` (four tabs), `nested` (every nested screen: status, risk, account, notifications, closed trades, security, help), `jitter` (records every DOM mutation for N seconds so "the screen stopped moving" is measured; `--tab N` and `--closed` watch a specific root tab, including the "Открытые → Закрытые" tap), `status` (samples the System Status screen for 30 s after opening: does the app re-prove permissions by itself; `--after N` idles on the root tab first so the cached evidence is genuinely older than its TTL), `churn` (50 tab switches plus nested navigation while a page-installed counter watches WebSocket opens and leaks), `shot` (PNG of the current surface plus its text, for a human reviewer). `--out <prefix>` writes phase screenshots during `swipe`. The `noise` field matches only the app's own staleness wording, so real feed copy like "сигнал мастера устарел" is not reported as UI noise. Read-only: uses the Mini App API as the session user. Needs a locally minted signed `window.Telegram` stub (`--init`, a session credential — never commit); `telegram.org/js/telegram-web-app.js` is blocked because outside Telegram it overwrites the stub with an empty initData | `node scripts/audit_miniapp_browser.mjs smoke\|swipe\|series\|nested\|jitter\|status\|churn\|shot --init <stub.js> [--seconds 190] [--tab 1] [--closed] [--after 95] [--out data/pull] [--url ...] [--chrome ...]` |
| `audit_miniapp_browser.mjs` | Real-Chromium acceptance over CDP (Node, no npm deps) against the *running* bot: `smoke` (what the surface shows), `swipe` (dispatched touch pull: indicator phases, Telegram swipe handoff, the `refresh=true` reads it fires, and the scrolled-away guard on a genuinely scrollable page), `series` (four tabs), `nested` (every nested screen: status, risk, account, notifications, closed trades, security, help), `jitter` (records every DOM mutation for N seconds so "the screen stopped moving" is measured; `--tab N` and `--closed` watch a specific root tab, including the "Открытые → Закрытые" tap), `status` (samples the System Status screen for 24 s after opening: does the app re-prove permissions by itself). `churn` (50 tab switches plus nested navigation while a page-installed counter watches WebSocket opens and leaks: the plan's "a tab walk must not multiply live sessions" gate). Read-only: uses the Mini App API as the session user. Needs a locally minted signed `window.Telegram` stub (`--init`, a session credential — never commit); `telegram.org/js/telegram-web-app.js` is blocked because outside Telegram it overwrites the stub with an empty initData | `node scripts/audit_miniapp_browser.mjs smoke\|swipe\|series\|nested\|jitter\|status\|churn --init <stub.js> [--seconds 190] [--tab 1] [--closed] [--url ...] [--chrome ...]` |
| `audit_miniapp_browser.mjs` | Real-Chromium acceptance over CDP (Node, no npm deps) against the *running* bot: `smoke` (what the surface shows), `swipe` (dispatched touch pull: indicator phases, Telegram swipe handoff, the `refresh=true` reads it fires, and the scrolled-away guard on a genuinely scrollable page), `series` (four tabs), `nested` (every nested screen: status, risk, account, notifications, closed trades, security, help), `jitter` (records every DOM mutation for N seconds so "the screen stopped moving" is measured). Read-only: uses the Mini App API as the session user. Needs a locally minted signed `window.Telegram` stub (`--init`, a session credential — never commit); `telegram.org/js/telegram-web-app.js` is blocked because outside Telegram it overwrites the stub with an empty initData. Every `Runtime.evaluate` is bounded at 15 s, and a rejected session fails with its own message, so a stale stub can no longer look like a hung UI | `node scripts/audit_miniapp_browser.mjs smoke\|swipe\|series\|nested\|jitter --init <stub.js> [--seconds 190] [--url ...] [--chrome ...]` |

## Manual testnet operators (change exchange state)

| Script | Purpose | Run |
|---|---|---|
| `manual_master_order.py` | Manual master actions for reproducible smoke runs: `open` (qty/stop-pct/long-short), `open-nostop`, `set-stop`/`move-stop`, `set-tp`/`move-tp`, `partial-close PCT`, `close`. Handles hedge legs (`leg_side`: `BOTH`→`LONG`), `exchange_info` filters, ticker-price fallback | `python scripts/manual_master_order.py open BTCUSDT 0.002 0.5 [long\|short]` (full usage in the file docstring) |
| `capture_stream.py` | Records N seconds of the master user-data stream (`main` account from DB) into a replay fixture `{captured_at, count, events}`. Default target: `tests/replay/fixtures/captured_stream.json` | `python scripts/capture_stream.py SECONDS [outfile]` (default 60s) |

## Load/soak campaigns (signed testnet, artifacts in `data/`)

| Script | Purpose | Run |
|---|---|---|
| `hybrid_load_test.py` | Hybrid load: REAL signed-testnet master+followers plus SHADOW rows on `ShadowExchange`, shared coordinator/planner/executor/journal. Safety: refuses non-testnet REST URL, clones only encrypted REAL creds into a separate run DB (refuses if it exists), requires flat/order-free accounts, cleanup in `finally` | `--followers` (30) `--cycles` (100) `--quantity` (0.002) `--stop-distance` (500) `--source-db/--run-db/--report` `--timeout` (30.0); evidence in `docs/reports/HYBRID_LOAD_TEST_REPORT_2026-08.md` |
| `testnet_soak.py` | Two-hour Stage 1 soak: few min-size cycles (default 6×0.002, stop/TP 3000/2800) + forced listenKey reconnect + graceful restart + real SIGKILL of a child App with heartbeat recovery. Reuses `hybrid_load_test` helpers | Gate: both `CT_TESTNET=1` and `CT_SOAK=1` (else `RuntimeError`); `--duration` (7200) `--cycles` (6) `--quantity` (0.002) `--source-db/--run-db/--report` |

## Repo checks (safe, local)

| Script | Purpose | Run |
|---|---|---|
| `check_doc_links.py` | Verifies every relative `.md` link in root/test/scripts/miniapp `AGENTS.md`, `PRODUCT.md`, `README.md`, `docs/**/*.md` resolves to an existing file (`--strict` also checks `#fragment` anchors). Run before docs commits per `docs/AGENTS.md` conventions | `python scripts/check_doc_links.py [--strict]` |
| `benchmark_history.py` | Offline synthetic capacity/commit probe over a temporary SQLite database; no Settings, credentials or network | `PYTHONPATH=src .venv/bin/python scripts/benchmark_history.py [--accounts N]` |
| `check_doc_budgets.py` | Offline line-budget gate for agent maps: compares each file against `doc-budgets.json`, exit 1 on excess | `python scripts/check_doc_budgets.py` |
| `check_doc_facts.py` | Offline facts-gate for standing docs: pins Mini App route / package / screenshot / asset / section counts and claim-vs-truth consistency (probes P-POST…P-SECTIONS); hot suite counters print SKIPPED, suites never run | `python scripts/check_doc_facts.py` |

## Conventions

- Exchange-touching scripts go through `Settings()` + `Database` + `SqliteCredentials` — secrets only from env/DB, never from argv. `src/` reachability is via `sys.path` insert (`capture_stream`, `manual_master_order`, `benchmark_history`) or the environment (`hybrid_load_test`, `testnet_soak`, `preview_miniapp_engine`); stdlib-only checks (`check_doc_links`, `detach_process`, `verify_menu_button`) and the offline `benchmark_history` probe (temporary SQLite, `Database` only) use neither.
- Campaigns write JSON reports (`--report`) and never touch prod DB directly: clone it (hybrid/soak) or backup-API-copy it (preview).
- New ops scripts go here with usage in the docstring (`Usage:`/`Commands:`/`Run:` block); visual previews follow `preview_miniapp_engine.py` (backup copy + dev user + `--confirm-*` flag).

**Tunnel hostname DNS gate (dev-up.sh):** after starting cloudflared, `wait_tunnel_dns`
waits (a) for the new hostname in public DoH and only then (b) for this machine's own
resolver. Asking the local resolver first makes macOS cache the NXDOMAIN for the zone's
1800s SOA minimum, and Telegram WebView then fails with ERR_NAME_NOT_RESOLVED for ~30
minutes although the tunnel and edge are healthy; the only recovery is `--rotate`, which
invalidates the Telegram menu button for minutes. The reuse path applies the same check and
fails loudly instead of serving READY on a locally unresolvable hostname.
