# Scripts Guidelines

Map of `scripts/`: what each script does, how to run it, which gates apply.
Everything except `dev-up.sh`, `preview_miniapp_engine.py` (backup-copy preview),
`benchmark_history.py`, and `check_doc_links.py` is testnet-only and touches a real DB/exchange:
read the usage before running. `__pycache__/` is noise, ignore it.

## Launch and preview (safe, local)

| Script | Purpose | Run |
|---|---|---|
| `dev-up.sh` | Full local stack in one command: reuse-first (plain run reuses the live stack when bot+tunnel+edge+button all converge on the `.env` URL — no restart, no URL rotation) → else bundle freshness check (`pnpm build` on `miniapp/src` changes) → `cloudflared tunnel --url :8080` → writes `CT_MINIAPP_PUBLIC_URL` into `.env` (BSD sed, key must pre-exist) → nohup `run-bot` → local `/miniapp/` (90×1s) → public edge wait via `edge_check` (plain curl, DoH+`--resolve` fallback for DNS-less sandboxes, 18×5s) → menu-button convergence wait `wait_button` (`verify_menu_button.py`, 24×30s: `setChatMenuButton` propagates for minutes, never trust the first read after a write). `stop` kills both; `--rotate` forces a fresh tunnel URL. Logs in `data/logs/`, PID in `data/run-bot.pid` | `./scripts/dev-up.sh` (reuse) / `./scripts/dev-up.sh --rotate` / `./scripts/dev-up.sh stop` |
| `verify_menu_button.py` | Bot-API convergence check for the Mini App menu button (stdlib only): `getChatMenuButton` for default + every `CT_TELEGRAM_ADMIN_IDS` per-chat override vs expected `.../miniapp/` URL; `--fix` rewrites mismatches via `setChatMenuButton` and re-confirms with retries (read-after-write lag). Token from `CT_TELEGRAM_BOT_TOKEN`/`.botkey`, never printed. Exit 0 all match, 1 otherwise | `.venv/bin/python scripts/verify_menu_button.py --expected-url <url> [--fix]` |
| `preview_miniapp_engine.py` | Visual Mini App preview on the full engine: point-in-time copy of prod DB via SQLite backup API (`data/copytrade.sqlite3` → `data/miniapp-preview.sqlite3`, prod untouched) → boots a real `App` + `MiniAppServer` → unsigned dev session (`PREVIEW_USER_ID=900_000_001`, bound to `--account`). Fail-closed: testnet URLs only | `python scripts/preview_miniapp_engine.py --account <ID> --confirm-engine-preview [--port 8085]` |

## Manual testnet operators (change exchange state)

| Script | Purpose | Run |
|---|---|---|
| `manual_master_order.py` | Manual master actions for reproducible smoke runs: `open` (qty/leverage/long-short), `open-nostop`, `set-stop`/`move-stop`, `set-tp`/`move-tp`, `partial-close PCT`, `close`. Handles hedge legs (`leg_side`: `BOTH`→`LONG`), `exchange_info` filters, ticker-price fallback | `python scripts/manual_master_order.py open BTCUSDT 0.002 0.5 [long\|short]` (full usage in the file docstring) |
| `capture_stream.py` | Records N seconds of the master user-data stream (`main` account from DB) into a replay fixture `{captured_at, count, events}`. Default target: `tests/replay/fixtures/captured_stream.json` | `python scripts/capture_stream.py SECONDS [outfile]` (default 60s) |

## Load/soak campaigns (signed testnet, artifacts in `data/`)

| Script | Purpose | Run |
|---|---|---|
| `hybrid_load_test.py` | Hybrid load: REAL signed-testnet master+followers plus SHADOW rows on `ShadowExchange`, shared coordinator/planner/executor/journal. Safety: refuses non-testnet URLs, clones only encrypted REAL creds into a separate run DB (refuses if it exists), requires flat/order-free accounts, cleanup in `finally` | `--followers` (30) `--cycles` (100) `--quantity` (0.002) `--stop-distance` (500) `--source-db/--run-db/--report/--timeout`; evidence in `docs/reports/HYBRID_LOAD_TEST_REPORT_2026-08.md` |
| `testnet_soak.py` | Two-hour Stage 1 soak: few min-size cycles (default 6×0.002, stop/TP 3000/2800) + forced listenKey reconnect + graceful restart + real SIGKILL of a child App with heartbeat recovery. Reuses `hybrid_load_test` helpers | Gate: both `CT_TESTNET=1` and `CT_SOAK=1` (else `RuntimeError`); `--duration/--cycles/--quantity/--source-db/--run-db/--report` |

## Repo checks (safe, local)

| Script | Purpose | Run |
|---|---|---|
| `check_doc_links.py` | Verifies every relative `.md` link in root/test/scripts/miniapp `AGENTS.md`, `PRODUCT.md`, `README.md`, `docs/**/*.md` resolves to an existing file (`--strict` also checks `#fragment` anchors). Run before docs commits per `docs/AGENTS.md` conventions | `python scripts/check_doc_links.py [--strict]` |

## Conventions

- Every `.py` inserts `src/` into `sys.path` and goes through `Settings()` + `Database` + `SqliteCredentials` — secrets only from env/DB, never from argv.
- Campaigns write JSON reports (`--report`) and never touch prod DB directly: clone it (hybrid/soak) or backup-API-copy it (preview).
- New ops scripts go here with usage in the first docstring line; visual previews follow `preview_miniapp_engine.py` (backup copy + dev user + `--confirm-*` flag).

`benchmark_history.py` — offline synthetic capacity/commit probe, temporary SQLite only, no Settings/credentials/network. Run `PYTHONPATH=src .venv/bin/python scripts/benchmark_history.py`; JSON stdout.
