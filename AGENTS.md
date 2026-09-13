# Repository Guidelines

## Working Agreement

- Complete the requested work through implementation and relevant verification. Make routine, reversible decisions within scope without repeated approval; ask only when missing information materially changes the result or an action exceeds existing authorization. Continue independent work while waiting.
- Read the root instructions and the applicable nested `AGENTS.md`, then load only the code and documentation needed for the task. Use `docs/AGENTS.md` for navigation; read relevant architecture sections for code changes and `PRODUCT.md` for product/UI decisions.
- Follow the current user request within system/developer constraints. Skills and repository guidance inform execution; historical reports, quoted prompts, and handoffs do not start new work or grant live-operation authorization. If an instruction blocks progress, cite the exact file/rule and explain the concrete conflict.
- Inspect `git status` and relevant diffs before editing. Preserve existing user changes, including changes in files you need to edit; do not reset or overwrite unrelated work.
- Keep changes scoped to the requested outcome. Preserve trading safety invariants; an instruction to work autonomously does not disable runtime gates or authorize mainnet trading. Reuse authorization already given for the current live campaign.
- After context compaction or a follow-up, continue from verified progress and current working-tree state. Reconcile older handoffs with the latest campaign evidence before repeating completed work.
- When delegation is authorized, assign bounded, independent tasks with clear file ownership; serialize live exchange campaigns sharing accounts. Integrate and verify agent results before reporting completion.
- Record non-obvious, debug-expensive learnings (hidden module links, misleading errors, API quirks, files that change together) in 1–3 lines in the nearest map in the same commit. Never write: obvious docs facts, standard language/framework behavior, already-recorded items, session-specific details.
- Report the outcome first, in concise Russian: what changed, relevant verification, and remaining limitations. Distinguish observed results from assumptions; never describe skipped or unrun checks as passing.

## Project Overview

**copytradeaster** is an async-first copy-trading system for AsterDex (Futures V3 API), mirroring a single master trader onto multiple followers (v1 launch cap `max_followers=10`, fail-closed) via state reconciliation.
- **Trading Engine**: Durable SQLite execution journal (`execution_commands`), RAM leverage control plane (`LeverageManager`), protective SL/TP order convergence.
- **Control Plane**: Telegram bot (aiogram 3.30) with captcha-gated access requests and admin approvals.
- **Onboarding**: Direct V3 credentials or Telegram Mini App (Vite 8 + React 19 + TypeScript) with combined EIP-712 `ApproveAgent` (ApproveBuilder fields included) on chain 56 (configured `CT_MAX_FEE_RATE=0.0001`, absolute Futures cap `0.001` fail-closed; `approveBuilder` endpoint is legacy-CLI only).
- **Follower Mini App**: 4-tab AppShell (`Главная`, `Позиции`, `Активность`, `Настройки`), read-only positions with SL coverage/risk USDT, redacted activity timeline, honest settings, notifications double-tap toggle gate (`createToggleGate`), native BackButton/Haptics. Execution journal: `docs/reports/STAGE3_PART2_PROGRESS_2026-09.md`.
- **Live presentation**: engine snapshots publish into a process-local store, per-follower private streams re-snapshot through coalesced REST, and a shared public mark-price stream drives quotes; the browser gets resource invalidations over an authenticated Mini App WebSocket (single-use ticket). No manual refresh button exists anywhere — the rail appears only while the channel is silent, and pull-down is the single manual path. Evidence: `docs/reports/MINIAPP_LIVE_MODE_PLAN_2026-09.md`.
- **Outbox & Shadow Engine**: Async SQLite notification delivery (`TelegramOutboxWorker`); in-memory `ShadowExchange` for load tests. App gate `CT_SHADOW_ACCOUNTS_ENABLED=true` ≠ test gate `CT_LOAD=1` — do not conflate.

---

## Architecture & Data Flow

```text
Master WS (ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ACCOUNT_CONFIG_UPDATE, MARGIN_CALL) -> MasterProjector (copy_trades)
  -> Coordinator (FollowerRegistry) -> [LeverageManager gate + RiskEngine sizing]
  -> Planner (diff -> ExecutionPlan DAG) -> Executor (journal -> AsterClient / ShadowExchange)
  -> Followers -> Reconciler (REST sweep every `reconcile_interval_s`, default 15s)

Presentation (never a trading input): follower snapshot -> FollowerSnapshotStore (revision + captured_at)
  -> MiniAppLiveHub -> authenticated `/miniapp/api/live` WebSocket -> resource invalidation -> weak cache read
  follower private stream (REAL accounts only) -> coalesced REST re-snapshot; public mark stream (or REST fallback) -> quotes only
```

Boot (`App.start`): migrate → shadow-boot validation → `credentials.load` → `registry.load_at_boot` → clock-drift check (max 2000ms) → `resolve_boot_unknowns` (fail-closed on any in-flight UNKNOWN/SENDING/SUBMITTED) → mainnet preflight (mainnet only) → position-mode bootstrap → exchangeInfo filters → `projector.rebuild_full` (authoritative REST) → `leverage.refresh_catalog` (best-effort, worker retries) → startup reconcile → ingest + stream + stream-ready wait (30s; timeout stays close-only, not READY) + periodic sweep → READY via post-connect master boundary. `Stage2Process` wraps App + NotificationRouter/SQLite outbox + TelegramOutboxWorker + TelegramRuntime + `MiniAppLiveRuntime` + MiniApp aiohttp + HistoryWorker; teardown stops the live runtime before the history worker.

### Key Invariants
1. **State Replication, Not Command Mirroring**: WebSocket is a low-latency trigger; REST snapshots and reconciliations are authoritative. Consistent state → reconciler emits 0 commands.
2. **Fail-Closed Safety**:
   - Stage-2 onboarding starts `PAUSED` (`allow_new_entries=False`); legacy CLI `import-wallet` keeps old unpaused default.
   - Resuming requires `SystemState.READY`, fresh snapshot, and single-use confirmation token (TTL 60s).
   - Unknown leverage blocks `MARKET_OPEN` (`LEVERAGE_NOT_READY`) while allowing `MARKET_CLOSE` and cancellations.
   - An authoritative follower `non-zero→flat` without a newer confirmed engine close whose *executed* quantity covers the prior quantity or a linked protective fill latches an episode-bound external-close block; same-episode manual reopen is closed, not adopted. Own Stop/TP `FILLED` proof is durable per episode; pre-#61 flat observations with linked protection are quarantined by #62 because old fill provenance was not stored.
   - ExecutionPlan DAG (`depends_on_action_id`): authoritative master `marginType` mirrors to the follower through `SET_MARGIN_TYPE → SET_LEVERAGE → MARKET_OPEN`; unknown/conflicting master mode blocks new exposure. An occupied symbol blocks a mode switch, but growth in an already matching mode is allowed. ISOLATED collateral is at least 2× fee-buffered Stop loss before initial entry and growth, with the liquidation guard retained; account-wide Multi-Assets is not auto-switched.
  - Journal `PENDING→SENDING→SUBMITTED→CONFIRMED/UNKNOWN/FAILED`; UNKNOWN (HTTP 503/504, codes `-1000/-1006/-1007`, timeouts) resolved by exchange query before retry; `-5026` is the only authoritative Builder rejection (entry fail-closed, reduction single DIRECT retry).
3. **Key Isolation & Security**:
   - Private keys and user signatures never logged or stored in plaintext. Credential blobs + builder attempts use AES-256-GCM (`CT_ENCRYPTION_KEY`, 32-byte hex).
   - Builder onboarding 4-state lifecycle (`PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`) in `builder_onboarding_attempts`, purged on commit.
   - Uncommitted credentials in memory isolated via `EphemeralIdentityProvider` / `EphemeralKeyProvider`.
4. **Concurrency & Storage**:
   - Single Python process. `Stage2Process` (`application/engine.py`) orchestrates Database, App, Telegram, Outbox, MiniApp, HistoryWorker.
   - SQLite WAL + `busy_timeout=30000` + process-local `Database.write_lock`; **65 migrations (indices 0..64)**.
   - Per-account `asyncio.Lock` serializes order/nonce/leverage pipelines; `FollowerRegistry` is copy-on-write (lock-free reads, CAS publish under mutation lock).
5. **Presentation Is Not Authority**: `FollowerSnapshotStore` revisions, private-stream hints and mark-price quotes only invalidate Mini App DTOs — they never mutate trading target, admission or `captured_at` freshness. Mark ticks change presentation quotes/PnL only; REST snapshots and reconciliation stay authoritative.

---

## Key Directories

```text
copytradeaster/
├── src/copytradeaster/
│   ├── app.py                 # Composition root, boot recovery & background reconciliation
│   ├── config.py              # Pydantic V2 settings (CT_* env vars) & testnet guards
│   ├── metrics.py             # Bounded in-RAM load-gate metrics (reconcile/follower/limiter/ingest)
│   ├── logging_utils.py       # Secret-safe logging setup (durable bounded file + console sink)
│   ├── domain/                # Canonical models (models.py) with _DecimalStrict coercion
│   ├── aster/                 # REST (trading/management), WS user_stream, EIP-712 signing, ShadowExchange
│   ├── master/                # MasterProjector: WS -> normalized state deltas & copy_trades
│   ├── risk/                  # RiskEngine: pure sizing math, 80% margin cap (MARGIN_SAFETY=0.8)
│   ├── trading/               # Coordinator, Planner, Executor, Reconciler, LeverageManager, FollowerRegistry, BuilderAttribution, FollowerSnapshotStore (snapshots.py)
│   ├── history/               # Durable fill import, trade episodes/events, reconciliation and replay
│   ├── storage/               # SQLite Database, MIGRATIONS (0..64), async repositories
│   ├── credentials/           # SqliteCredentials with AES-256-GCM encryption (crypto.py)
│   ├── notifications/         # NotificationRouter, SQLite outbox, TelegramOutboxWorker
│   ├── onboarding/            # Direct & Builder (4-state lifecycle) onboarding services
│   ├── application/           # UserApplicationService, Stage2Process (engine.py), control.py
│   ├── telegram/              # aiogram 3.30 runtime, menu engine, access captcha, FSM flows, routers/
│   ├── miniapp/               # aiohttp server (HMAC initData auth, 27 POST /miniapp/api/*, single-use-ticket live WebSocket, static host)
│   └── cli/                   # main.py CLI dispatcher (13 subcommands)
├── miniapp/                   # Frontend SPA (Vite 8 + React 19 + TS ~6 + Oxlint + Vitest); map: miniapp/AGENTS.md
│   ├── src/screens/           # Home, Positions, TradeList (open/closed), TradeDetail, Activity, Settings, Notifications, Risk, SystemStatus, AccountBuilder, BuilderWizard, BuilderAttemptRecovery, SessionGate, SuccessScreen, InfoScreens (Security/Help)
│   ├── src/components/        # AppShell.tsx + ui.tsx (Brand/TrustRail/Sheet/Row/ErrorBox; SVG icons inline) + PositionSummaryCard.tsx, TradeSummaryCard.tsx, TradeEventRow.tsx, RiskRuler.tsx, LiveStatus.tsx (stale rail), PullToRefresh.tsx
│   ├── src/live.ts            # Session-scoped live client: ticket -> WebSocket, epoch/revision watermark, reconnect, staleness
│   ├── src/wallet.ts          # Injected EIP-1193 + lazy WalletConnect provider
│   └── src/api.ts             # Typed API client with 15s AbortController timeout
├── tests/                     # unit/, integration/, replay/, testnet/; map: tests/AGENTS.md
├── scripts/                   # dev-up.sh, detach_process.py, preview_miniapp_engine.py, verify_menu_button.py, audit_miniapp_browser.mjs, check_mark_stream.py, check_doc_links.py, check_doc_budgets.py, doc-budgets.json, check_doc_facts.py, benchmark_history.py, benchmark_episode_money.py, verify_episode_money_bounds.py, probe_builder_fee_source.py, manual_master_order.py, hybrid_load_test.py, testnet_soak.py, capture_stream.py; map: scripts/AGENTS.md
├── docs/                      # AGENTS.md (docs map), ARCHITECTURE.md, DEV_PLAN_SKELETON.md, ENGINE_TEST_STATUS_2026-08.md, reports/ (plan_live_test.md, stage journals + acceptance-s3/ + acceptance-p27/), reviews/, history/, ASTER_API_RESEARCH_2026-08.md
└── data/                      # Local SQLite databases, logs, test artifacts (git-ignored)
```

---

## Development Commands

### 1. Environment & Configuration
```bash
# Backend (Python 3.12 strict, uv-managed venv) & Frontend setup
uv sync                          # creates .venv; runtime + PEP 735 `dev` group
cd miniapp && pnpm install && pnpm build && cd ..
```
Key `.env` settings (`.env.example` is the full template — 48 `CT_*` fields; below is a subset):
```env
CT_DB_PATH=data/copytrade.sqlite3
CT_ENCRYPTION_KEY=<64-hex>        # python -c "import secrets; print(secrets.token_hex(32))"
CT_TELEGRAM_BOT_TOKEN=123456:ABC # or BOT_KEY in .botkey
CT_TELEGRAM_ADMIN_IDS=[123456789]
CT_MINIAPP_ENABLED=true
CT_MINIAPP_STATIC_DIR=miniapp/dist
CT_MINIAPP_PUBLIC_URL=https://<tunnel-hash>.trycloudflare.com # auto-set by dev-up.sh
CT_REST_BASE_URL=https://fapi.asterdex-testnet.com
CT_ALLOW_MAINNET=false           # strict fail-closed guard
CT_BUILDER_ADDRESS=0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8 # example only; shipped default is empty
CT_BUILDER_NAME=copytradeaster
CT_MAX_FEE_RATE=0.0001
```

### 2. Launch & Previews
```bash
# 1. Recommended full dev stack (builds bundle, cloudflared tunnel, sets CT_MINIAPP_PUBLIC_URL, runs bot):
./scripts/dev-up.sh             # stop: ./scripts/dev-up.sh stop

# 2. Manual launch fallback:
cloudflared tunnel --url http://127.0.0.1:8080   # copy URL to CT_MINIAPP_PUBLIC_URL in .env
uv run python -m copytradeaster init-db && uv run copytradeaster run-bot
# Bot automatically registers MenuButtonWebApp(text="ASTER COPY") to CT_MINIAPP_PUBLIC_URL/miniapp/

# 3. Real-data engine visual preview (boots trading engine on SQLite backup copy):
uv run python scripts/preview_miniapp_engine.py --account <ACCOUNT_ID> --confirm-engine-preview [--port 8085]

# 4. Vite frontend dev server (with hot reload and API proxy to :8080):
cd miniapp && pnpm dev
```

### 3. CLI Operations
```bash
copytradeaster init-db                                   # create/migrate SQLite DB
copytradeaster run-bot                                  # bounded Stage2 process: bot + MiniApp + core
copytradeaster status                                   # health, runtime state, active trades
copytradeaster run                                      # standalone trading core (no Telegram)
copytradeaster reconcile [--account <id>]               # manual state reconciliation sweep
copytradeaster pause | resume                           # control-plane pause / resume
copytradeaster emergency-close-all --yes                # emergency flat liquidation
copytradeaster telegram-user add <id> [--chat-id <id>]|list|block|unblock <id> # manage authorized Telegram users
copytradeaster import-wallet wallet.txt [--wipe]        # import credentials from wallet.txt
copytradeaster approve-builder [--accounts all|<id1..>] [--max-fee-rate <rate>] # batch EIP-712 builder approval
copytradeaster prove-builder-fee --account <id> --yes # measure the Builder fee source on testnet and stamp the money policy
copytradeaster backfill-income --account <id> [--days 30] # import commissions/funding older than the live 7-day window
```

### 4. Testing & QA
```bash
uv run pytest -q -m "not testnet"                        # fast offline backend suite
uv run pytest -q -m "not testnet" --cov=src/copytradeaster # backend coverage (documentary >= 87%, no fail_under gate)
uv run ruff check . && uv run ruff format --check .      # Python lint and format (uv run ruff format .)
cd miniapp && pnpm lint && pnpm test && pnpm build && cd .. # frontend Oxlint + Vitest + build
CT_TESTNET=1 uv run pytest tests/testnet/ -v             # signed Aster Testnet suite (needs wallet.txt, flat positions after)
CT_LOAD=1 uv run pytest tests/integration/test_shadow_scale.py # 30x100 + 100x20 shadow scale preflight (load marker itself unused)
uv run python scripts/check_mark_stream.py [--network mainnet|testnet]  # public mark-stream gate: 0 STREAMING, 3 PARTIAL, 1 SILENT, 2 probe failure
node scripts/audit_miniapp_browser.mjs smoke|series|nested|jitter|churn|swipe|status|shot --init <stub.js>  # real-Chromium acceptance against a running bot
```

---

## Code Conventions & Common Patterns

### Python Backend
- **Type Annotations**: Strict typing on all public methods (`from __future__ import annotations` in most modules; a few `__init__`/entry files omit it).
- **Strict Decimals**: Never use `float` for prices/amounts. Enforce `_DecimalStrict` in `domain.models`; wire format via `format(x, "f")`; fee/maxFeeRate as strings compared via `Decimal`.
- **Async First**: `async def`, `httpx.AsyncClient`, `aiohttp.web` (Mini App transport), `aiosqlite`, `asyncio.Lock`.
- **Naming & Models**: `snake_case` functions, `PascalCase` classes/DTOs, `UPPER_SNAKE` constants. Pydantic V2 for API/config; `dataclass(slots=True)` for domain models/DB rows.
- **Dependency Injection**: Explicit protocols (`typing.Protocol`) and constructor injection. No service locators. Ports: `aster/ports.py`, `onboarding/providers.py`, `application/service.py` (+ `control.py`/`engine.py`), `notifications/protocol.py`; `application/adapters.py` is the concrete composition seam.
- **Error Handling**: Typed exceptions (`AsterError`/`ManagementError`/`RateLimitError`, `OnboardingError(Code)` secret-safe, `ApplicationError` with `public_code` → HTTP 400/403/409/503; 401 is the session-auth `invalid_session` path). Transport errors classified; unknown outcomes marked `UNKNOWN` and resolved via exchange queries before retrying (`resolve_boot_unknowns`).
- **Concurrency & Storage**: `Database.write_lock` serializes SQLite writes; `NonceStore` persist-before-send. Per-account `asyncio.Lock` serializes execution pipelines. ExecutionPlan uses DAG dependencies (`depends_on_action_id`). New migrations appended to `MIGRATIONS` in `storage/database.py`.

### Frontend (Mini App)
- **Framework & Linting**: React 19 + TypeScript (`tsconfig.app.json`, `erasableSyntaxOnly`, `noUnused*`). Oxlint for linting (ESLint is NOT used).
- **Styling**: Vanilla CSS tokens in `src/index.css` (Aster dark theme: `#070707` canvas, `#F2C98D` accent). No heavy UI libraries.
- **UI Contract Tests**: Vitest in Node environment (`environment: 'node'`, `src/**/*.test.{ts,tsx}`) with React 19 `renderToStaticMarkup`.
- **UX & Accessibility**: Inline SVG icons (no emoji), native BackButton/Haptics, synchronous toggle lock (`createToggleGate`), `:focus-visible`, semantic dialog roles.

---

## Important Files

|File|Purpose|
|---|---|
|`src/copytradeaster/app.py`|Composition root, boot sequence, WS ingest loop, periodic reconciliation|
|`src/copytradeaster/config.py`|Pydantic V2 settings (`CT_*` env vars), fail-closed testnet guards|
|`src/copytradeaster/domain/models.py`|Canonical domain models with `_DecimalStrict` coercion & explicit enums|
|`src/copytradeaster/trading/coordinator.py`|Fans out master deltas to follower target positions (LeverageManager gate + RiskEngine sizing)|
|`src/copytradeaster/trading/planner.py`|Pure transition planner generating ExecutionPlan with DAG dependencies|
|`src/copytradeaster/trading/executor.py`|Durable execution journal (`execution_commands`) with UNKNOWN resolution|
|`src/copytradeaster/trading/leverage.py`|RAM control-plane for candidate & confirmed leverage generations|
|`src/copytradeaster/trading/registry.py`|In-memory FollowerRegistry isolating runtime state from storage|
|`src/copytradeaster/trading/reconciler.py`|Full actual-vs-target sweep (`ReconcileReport`, 0-command invariant)|
|`src/copytradeaster/trading/builder_attribution.py`|Builder fee attribution verdicts (`DIRECT`/`BUILDER_VALID`/`BUILDER_UNAVAILABLE`), `-5026`-only|
|`src/copytradeaster/aster/shadow.py`|In-memory ShadowExchange simulating execution for load-testing followers|
|`src/copytradeaster/aster/user_stream.py`|Master WS listenKey lifecycle, keepalive, backoff reconnect|
|`src/copytradeaster/aster/signing.py`|EIP-712 trading + `ApproveAgent`/`ApproveBuilder` registration signing|
|`src/copytradeaster/aster/management_api.py`|`approveAgent`/`approveBuilder` POSTs, unknown-outcome classification|
|`src/copytradeaster/onboarding/builder.py`|4-state durable Builder onboarding lifecycle (`PREPARED..COMMITTED`)|
|`src/copytradeaster/application/service.py`|Transport-neutral use cases for Telegram bot, Mini App, and CLI|
|`src/copytradeaster/application/engine.py`|`Stage2Process` orchestrator (Database, App, Telegram, Outbox, MiniApp)|
|`src/copytradeaster/application/control.py`|Transport-neutral emergency flatten with flat-proof reports|
|`src/copytradeaster/notifications/router.py`|Structured fan-out into console + SQLite outbox|
|`src/copytradeaster/notifications/worker.py`|Bounded leased outbox delivery with retry/backoff|
|`src/copytradeaster/miniapp/server.py`|aiohttp server hosting Mini App API (27 POST `/miniapp/api/*`) & static assets|
|`src/copytradeaster/miniapp/live.py`|Live presentation runtime: single-use ticket store, account-scoped latest-wins hub, per-REAL-follower private streams with coalesced REST re-snapshot, shared mark-price stream + per-symbol REST fallback|
|`src/copytradeaster/trading/snapshots.py`|Process-local `FollowerSnapshotStore` (snapshot + `captured_at` + monotonic revision, mark-price overlay, `republish`) that backs `refresh=false` reads and live invalidations|
|`src/copytradeaster/cli/main.py`|CLI entry point and subcommands|
|`miniapp/src/App.tsx`|Mini App routing, session gating, 4-tab AppShell, and runtime recovery|
|`miniapp/src/live.ts`|Session-scoped live client (ticket -> WebSocket), epoch/revision watermark, gap reconciliation, bounded reconnect, stale deadline and `railSpeaks`|
|`scripts/dev-up.sh`|One-command dev orchestrator: builds bundle, manages tunnel, runs bot|
|`scripts/preview_miniapp_engine.py`|Visual preview running full engine on point-in-time SQLite backup copy|
|`scripts/check_mark_stream.py`|Pre-mainnet gate for the public mark-price stream: per-symbol coverage, exit 0 `STREAMING` / 3 `PARTIAL` / 1 `SILENT` / 2 probe failure|
|`scripts/audit_miniapp_browser.mjs`|Read-only Chromium/CDP acceptance over the running Mini App (smoke/series/nested/jitter/churn/swipe/status/shot)|
|`scripts/hybrid_load_test.py`|Hybrid signed-testnet + ShadowExchange load campaign|
|`scripts/testnet_soak.py`|Signed testnet soak runner (requires `CT_TESTNET=1` + `CT_SOAK=1`)|
|`scripts/benchmark_episode_money.py`|Trade-result read cost on a real DB copy: per-episode and page-batch `load_episode_money` with SQL calls/rows per statement|
|`scripts/probe_builder_fee_source.py`|Signed testnet proof of where the Builder fee is charged (attributed vs control round trip); CLI twin `prove-builder-fee` writes the durable policy stamp|
|`scripts/verify_episode_money_bounds.py`|Proves the money-read range bounds return identical money/issues/proof (bounds honoured vs stripped) on a real DB copy|
|`docs/AGENTS.md`|Docs map: central navigation for all project documentation (what lives where, what is live vs history, reading order)|
|`docs/ENGINE_TEST_STATUS_2026-08.md`|Single source of truth for test status, counters, and bug registry (#1–27)|
|`docs/ARCHITECTURE.md`|Authoritative system architecture reference (§§1–20; §19 Footprint Ladder for new capabilities, §20 live presentation)|
|`docs/reports/STAGE3_PART2_PROGRESS_2026-09.md`|Stage 3 Part 2 execution journal (P2.0–P2.7 CLOSED)|

---

## Runtime & Tooling Preferences

- **Python Runtime**: Python `3.12` strict (`>=3.12,<3.13`), managed by `uv`; a system `python3.12` is not required — `uv sync` provisions the interpreter and `.venv`.
- **Package Manager**: `uv`. `uv.lock` is committed and is the source of truth for resolved versions (`uv sync` after a pull; `uv add`/`uv remove` to change dependencies). Dev tools live in the PEP 735 `dev` group, so plain `uv sync` installs them; run Python through `uv run` (`uv run pytest`, `uv run ruff`, `uv run python scripts/...`). `pip install -e ".[dev]"` is no longer the supported path. `*.egg-info/` is generated by the editable build and must stay untracked.
- **Frontend Tooling**: Node.js (no pinned `engines`/`.nvmrc` — use modern Node for Vite 8); package manager `pnpm` (lockfile 9.0; `allowBuilds: {'@reown/appkit': true}`).
- **Linters / Formatters**: `ruff` (`line-length = 100`, rules `E,F,I,UP,B,SIM`; format uses ruff defaults, no `[tool.ruff.format]`) for Python; `oxlint` for TypeScript/React.
- **Test Runners**: `pytest` + `pytest-asyncio` (`asyncio_mode=auto`, `testpaths=["tests"]`, markers `testnet`+`load`) + `pytest-cov` + `respx`; `vitest` (`vitest run`) for frontend.
- **Network Guards**: Aster Testnet default (`fapi.asterdex-testnet.com`, chains `714`/`56`). Mainnet access is strictly fail-closed (`CT_ALLOW_MAINNET=false`). No Dockerfile/CI — do not look for one.

---

## Testing & QA Guidelines

- **Proportionate Verification**: For docs-only changes, check links and the diff; do not run trading/backend/frontend suites. For code, run relevant regression tests and lint/type/build checks for the affected surface. Changes to execution, risk, signing, persistence, or concurrency need checks of affected failure/recovery paths. Complete any explicitly required campaign acceptance gates; expand or repeat checks only for new changes, failures, or unresolved risks. The command list above is a toolbox, not a mandatory checklist for every task.
- **Coverage Requirement**: $\ge 87\%$ statement coverage across `src/copytradeaster` — documentary convention (no `fail_under` / `--cov-fail-under` enforcement; eyeball `--cov` output).
- **Test Status**: counters and bug registry live only in `docs/ENGINE_TEST_STATUS_2026-08.md` (bugs #1–27 closed; баг #27: мусорный liq 25.5M → wrong-side guard) — never copy numbers here.
- **Naming & Isolation**: `test_<module>_<behavior>.py`. Default `pytest -q -m "not testnet"` must run hermetically without network or private keys (per-test `tmp_path` SQLite + `migrate()`, `respx` HTTP mocks, `FakeAster`/`ShadowExchange`, synthetic `0x..` keys). `load` marker is registered but unused — real scale gate is `CT_LOAD=1` on the explicit path.
- **Live Testnet Protocol**: Live scenarios require `CT_TESTNET=1` and `wallet.txt` (sections `main`/`copyc1`/`copyc2`/`copyc3`/`builder`/`builder-gate`) with testnet faucet accounts. Cleanup is flat-with-no-open-orders in `finally`; testnet accounts are disposable, but every signed campaign on a shared account is serialized by the file lock and must leave a state it can explain — see `tests/AGENTS.md`.
- **Living Documentation**: Update `docs/ENGINE_TEST_STATUS_2026-08.md` after any test suite change. Do not duplicate counters across files.

---

## Agent-Specific Instructions

- **Language & Scope**: Keep user communications in Russian, technical, direct, and scoped to API/code facts without marketing filler.
- **Plan Maintenance**: Update `docs/DEV_PLAN_SKELETON.md` when stage milestones change and the relevant `docs/reports/` journal when campaign progress changes. Routine instruction/copy edits do not require a new campaign report or milestone. Update `docs/AGENTS.md` (docs map) when introducing new documentation files.
