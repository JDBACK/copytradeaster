# Repository Guidelines

## Project Overview

**copytradeaster** is an async-first copy-trading system for AsterDex (Futures V3 API), mirroring a single master trader onto multiple followers (10–30 target, max ~100) via state reconciliation.
- **Trading Engine**: Durable SQLite execution journal (`execution_commands`), RAM leverage control plane (`LeverageManager`), protective SL/TP order convergence.
- **Control Plane**: Telegram bot (aiogram 3.30) with captcha-gated access requests and admin approvals.
- **Onboarding**: Direct V3 credentials or Telegram Mini App (Vite 8 + React 19 + TypeScript) with EIP-712 `ApproveAgent` / `approveBuilder` on chain 56 (max fee rate cap 0.001).
- **Follower Mini App (Stage 3 Part 2)**: 4-tab AppShell (`Главная`, `Позиции`, `Активность`, `Настройки`), read-only positions with SL coverage/risk USDT, redacted activity timeline, honest settings, notifications double-tap toggle gate (`createToggleGate`), native BackButton/Haptics.
- **Outbox & Shadow Engine**: Async SQLite notification delivery (`TelegramOutboxWorker`); in-memory `ShadowExchange` for 30–100 follower load tests (`CT_SHADOW_ACCOUNTS_ENABLED=true` / `CT_LOAD=1`).

---

## Architecture & Data Flow

```text
Master WS (ORDER_TRADE/ACCOUNT/CONFIG_UPDATE) -> MasterProjector (copy_trades)
  -> Coordinator (FollowerRegistry) -> RiskEngine (80% margin cap) -> LeverageManager
  -> Planner (diff -> ExecutionPlan DAG) -> Executor (journal -> AsterClient / ShadowExchange)
  -> Followers -> Reconciler (REST sweep every 15s)
```

### Key Invariants
1. **State Replication, Not Command Mirroring**: WebSocket is a low-latency trigger; REST snapshots and reconciliations are authoritative.
2. **Fail-Closed Safety**:
   - New accounts start `PAUSED` (`allow_new_entries=False`).
   - Resuming requires `SystemState.READY`, fresh snapshot, and single-use confirmation token.
   - Unknown leverage blocks `MARKET_OPEN` (`LEVERAGE_NOT_READY`) while allowing `MARKET_CLOSE` and cancellations.
   - ExecutionPlan supports DAG dependencies (`depends_on_action_id`): `SET_LEVERAGE` must succeed before `MARKET_OPEN` (Bug #25).
3. **Key Isolation & Security**:
   - Private keys and user signatures are never logged or stored in plaintext. Signer keys use AES-256-GCM (`CT_ENCRYPTION_KEY`).
   - Builder onboarding uses a 4-state lifecycle (`PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`) in `builder_onboarding_attempts`, purged on commit.
   - Uncommitted credentials in memory are isolated via `EphemeralIdentityProvider` and `EphemeralKeyProvider`.
4. **Concurrency & Storage**:
   - Single Python process. `Stage2Process` (`application/engine.py`) orchestrates Database, App, Telegram, Outbox, and MiniApp.
   - SQLite WAL mode with `PRAGMA busy_timeout=30000` and process-local `Database.write_lock` serializing transactions across 27 migrations (indices 0..26).
   - Per-account `asyncio.Lock` serializes order/nonce/leverage pipelines.

---

## Key Directories

```text
copytradeaster/
├── src/copytradeaster/
│   ├── app.py                 # Composition root, boot recovery & background reconciliation
│   ├── config.py              # Pydantic V2 settings (CT_* env vars) & testnet guards
│   ├── domain/                # Canonical models (models.py) with _DecimalStrict coercion
│   ├── aster/                 # REST (trading/management), WS stream, EIP-712, ShadowExchange
│   ├── master/                # MasterProjector: WS -> normalized state deltas & copy_trades
│   ├── risk/                  # RiskEngine: deterministic sizing & protective order math
│   ├── trading/               # Coordinator, Planner, Executor, Reconciler, LeverageManager, FollowerRegistry
│   ├── storage/               # SQLite Database, MIGRATIONS (0..26), async repositories
│   ├── credentials/           # SqliteCredentials with AES-256-GCM encryption (crypto.py)
│   ├── notifications/         # NotificationRouter, SQLite outbox, TelegramOutboxWorker
│   ├── onboarding/            # Direct & Builder (4-state lifecycle) onboarding services
│   ├── application/           # UserApplicationService, Stage2Process (engine.py), control.py
│   ├── telegram/              # aiogram 3.30 runtime, menu engine, access captcha, FSM flows
│   ├── miniapp/               # aiohttp server (HMAC initData auth, 15 API routes, static host)
│   └── cli/                   # main.py CLI dispatcher and subcommands
├── miniapp/                   # Frontend SPA (Vite 8 + React 19 + TS 6 + Oxlint + Vitest)
│   ├── src/screens/           # Home, Positions, Activity, Settings, Notifications, Wizard
│   ├── src/components/        # AppShell, TrustRail, Sheet, PositionCard, SVG icons
│   ├── src/wallet.ts          # Injected EIP-1193 + lazy WalletConnect provider
│   └── src/api.ts             # Typed API client with 15s AbortController timeout
├── tests/                     # unit/ (500+ tests), integration/ (FakeAster), replay/, testnet/ (29 live)
├── scripts/                   # dev-up.sh, preview_miniapp_engine.py, manual_master_order.py, etc.
├── docs/                      # docs_map.md, ARCHITECTURE.md, DEV_PLAN_SKELETON.md, ENGINE_TEST_STATUS_2026-08.md
└── data/                      # Local SQLite databases, logs, test artifacts (git-ignored)
```

---

## Development Commands

### 1. Environment & Configuration
```bash
# Backend (Python 3.12 strict) & Frontend setup
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
cd miniapp && pnpm install && pnpm build && cd ..
```
Key `.env` settings (`.env.example` has full template):
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
CT_BUILDER_ADDRESS=0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8
CT_BUILDER_NAME=copytradeaster
CT_MAX_FEE_RATE=0.0001
```

### 2. Launch & Previews
```bash
# 1. Recommended full dev stack (builds bundle, cloudflared tunnel, sets CT_MINIAPP_PUBLIC_URL, runs bot):
./scripts/dev-up.sh             # stop: ./scripts/dev-up.sh stop

# 2. Manual launch fallback:
cloudflared tunnel --url http://127.0.0.1:8080   # copy URL to CT_MINIAPP_PUBLIC_URL in .env
.venv/bin/python -m copytradeaster init-db && copytradeaster run-bot
# Bot automatically registers MenuButtonWebApp(text="ASTER COPY") to CT_MINIAPP_PUBLIC_URL/miniapp/

# 3. P2.0+ Real-data engine visual preview (boots trading engine on SQLite backup copy):
.venv/bin/python scripts/preview_miniapp_engine.py --account <ACCOUNT_ID> --confirm-engine-preview [--port 8085]

# 4. Vite frontend dev server (with hot reload and API proxy to :8080):
cd miniapp && pnpm dev
```

### 3. CLI Operations
```bash
copytradeaster status                                    # health, runtime state, active trades
copytradeaster run                                       # standalone trading core (no Telegram)
copytradeaster reconcile [--account <id>]                # manual state reconciliation sweep
copytradeaster pause | resume                            # control-plane pause / resume
copytradeaster emergency-close-all --yes                 # emergency flat liquidation
copytradeaster telegram-user add|list|block|unblock <id> # manage authorized Telegram users
copytradeaster import-wallet wallet.txt [--wipe]         # import credentials from wallet.txt
copytradeaster approve-builder [--accounts all|<id1..>]  # batch EIP-712 builder approval
```

### 4. Testing & QA
```bash
pytest -q -m "not testnet"                               # fast offline backend suite (~773 tests)
pytest -q -m "not testnet" --cov=src/copytradeaster      # backend coverage (threshold >= 87%)
ruff check . && ruff format --check .                    # Python lint and format (ruff format .)
cd miniapp && pnpm lint && pnpm test && pnpm build && cd .. # frontend Oxlint + Vitest (102 tests) + build
CT_TESTNET=1 pytest tests/testnet/ -v                    # signed Aster Testnet suite (29 live scenarios)
CT_LOAD=1 pytest tests/integration/test_shadow_scale.py  # 30 & 100 follower scale load test
```

---

## Code Conventions & Common Patterns

### Python Backend
- **Type Annotations**: Strict typing on all public methods (`from __future__ import annotations`).
- **Strict Decimals**: Never use `float` for prices/amounts. Enforce `_DecimalStrict` in `domain.models`.
- **Async First**: `async def`, `httpx.AsyncClient`, `aiosqlite`, `asyncio.Lock`.
- **Naming & Models**: `snake_case` functions, `PascalCase` classes/DTOs, `UPPER_SNAKE` constants. Pydantic V2 for API/config; `dataclass(slots=True)` for domain models/DB rows.
- **Dependency Injection**: Explicit protocols (`typing.Protocol`) and constructor injection. No service locators.
- **Error Handling**: Typed exceptions (`CopyTradeError`, `OnboardingError`). Transport errors classified; unknown outcomes marked `UNKNOWN` and resolved via exchange queries before retrying (`resolve_boot_unknowns`).
- **Concurrency & Storage**: `Database.write_lock` serializes SQLite writes. Per-account `asyncio.Lock` serializes execution pipelines. ExecutionPlan uses DAG dependencies (`depends_on_action_id`). New migrations appended to `MIGRATIONS` in `storage/database.py`.

### Frontend (Mini App)
- **Framework & Linting**: React 19 + TypeScript (`tsconfig.app.json`). Oxlint for linting (ESLint is NOT used).
- **Styling**: Vanilla CSS tokens in `src/index.css` (Aster dark theme: `#070707` canvas, `#F2C98D` accent). No heavy UI libraries.
- **UI Contract Tests**: Vitest in Node environment with React 19 `renderToStaticMarkup` (no browser DOM emulation).
- **UX & Accessibility**: Inline SVG icons (no emoji), native BackButton/Haptics, synchronous toggle lock (`createToggleGate`), `:focus-visible`, semantic dialog roles.

---

## Important Files

|File|Purpose|
|---|---|
|`src/copytradeaster/app.py`|Composition root, boot sequence, WS ingest loop, periodic reconciliation|
|`src/copytradeaster/config.py`|Pydantic V2 settings (`CT_*` env vars), fail-closed testnet guards|
|`src/copytradeaster/domain/models.py`|Canonical domain models with `_DecimalStrict` coercion & explicit enums|
|`src/copytradeaster/trading/coordinator.py`|Fans out master deltas to follower target positions|
|`src/copytradeaster/trading/planner.py`|Pure transition planner generating ExecutionPlan with DAG dependencies|
|`src/copytradeaster/trading/executor.py`|Durable execution journal (`execution_commands`) with UNKNOWN resolution|
|`src/copytradeaster/trading/leverage.py`|RAM control-plane for candidate & confirmed leverage generations|
|`src/copytradeaster/trading/registry.py`|In-memory FollowerRegistry isolating runtime state from storage|
|`src/copytradeaster/aster/shadow.py`|In-memory ShadowExchange simulating execution for load-testing followers|
|`src/copytradeaster/onboarding/builder.py`|4-state durable Builder onboarding lifecycle (`PREPARED..COMMITTED`)|
|`src/copytradeaster/application/service.py`|Transport-neutral use cases for Telegram bot, Mini App, and CLI|
|`src/copytradeaster/application/engine.py`|`Stage2Process` orchestrator (Database, App, Telegram, Outbox, MiniApp)|
|`src/copytradeaster/miniapp/server.py`|aiohttp server hosting Mini App API (`/miniapp/api/*`) & static assets|
|`src/copytradeaster/cli/main.py`|CLI entry point and subcommands|
|`miniapp/src/App.tsx`|Mini App routing, session gating, 4-tab AppShell, and runtime recovery|
|`scripts/dev-up.sh`|One-command dev orchestrator: builds bundle, manages tunnel, runs bot|
|`scripts/preview_miniapp_engine.py`|Visual preview running full engine on point-in-time SQLite backup copy|
|`docs/docs_map.md`|Central navigation map for all project documentation|
|`docs/ENGINE_TEST_STATUS_2026-08.md`|Single source of truth for test status, counters, and bug registry (#1–26)|
|`docs/ARCHITECTURE.md`|Authoritative system architecture reference (§1–§15)|
|`docs/reports/STAGE3_PART2_PROGRESS_2026-09.md`|Stage 3 Part 2 execution journal (P2.0–P2.3 closed, P2.4–P2.7 pending)|

---

## Runtime & Tooling Preferences

- **Python Runtime**: Python `3.12` strict (`>=3.12,<3.13`).
- **Frontend Tooling**: Node.js `>=20`, package manager `pnpm` (v11+, lockfile 9.0; `allowBuilds: {'@reown/appkit': true}`).
- **Linters / Formatters**: `ruff` (`line-length = 100`, rules `E,F,I,UP,B,SIM`) for Python; `oxlint` for TypeScript/React.
- **Test Runners**: `pytest` + `pytest-asyncio` + `pytest-cov` + `respx`; `vitest` for frontend.
- **Network Guards**: Aster Testnet default (`fapi.asterdex-testnet.com`, chains `714`/`56`). Mainnet access is strictly fail-closed (`CT_ALLOW_MAINNET=false`).

---

## Testing & QA Guidelines

- **Coverage Requirement**: $\ge 87\%$ statement coverage across `src/copytradeaster`.
- **Test Status**:
  - Offline Backend: **773 passed / 2 skipped / 32 deselected** (~18s, 87% coverage).
  - Frontend Vitest: **102 passed across 11 test files** (clean Oxlint, clean Vite build).
  - Signed Testnet: **29 state-changing/live scenarios passed** against `fapi.asterdex-testnet.com`.
- **Authoritative Bug Registry**: Bugs #1–26 are fully closed in `docs/ENGINE_TEST_STATUS_2026-08.md` (no bug #27).
- **Naming & Isolation**: `test_<module>_<behavior>.py`. Default `pytest -q -m "not testnet"` must run hermetically without network or private keys.
- **Live Testnet Protocol**: Live scenarios require `CT_TESTNET=1` and `wallet.txt` with testnet faucet accounts. Always ensure clean flat positions after tests.
- **Living Documentation**: Update `docs/ENGINE_TEST_STATUS_2026-08.md` after any test suite change. Do not duplicate counters across files.

---

## Agent-Specific Instructions

- **Language & Scope**: Keep user communications in Russian, technical, direct, and scoped to API/code facts without marketing filler.
- **Plan Maintenance**: Maintain stage milestones in `docs/DEV_PLAN_SKELETON.md` and record campaign progress in `docs/reports/` (e.g. `STAGE3_PART2_PROGRESS_2026-09.md`). Update `docs/docs_map.md` when introducing new documentation files.
