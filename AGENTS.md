# Repository Guidelines

## Project Overview

**copytradeaster** is an async-first copy-trading system built for AsterDex (Futures V3 API). It mirrors positions from a single master trader account onto multiple follower accounts (target scale: 10–30 users, maximum ~100) using a state-reconciliation model.

The system features:
- Core trading engine with durable SQLite execution journal, automated symbol leverage control-plane, and protective order convergence (SL/TP).
- Telegram control plane (aiogram 3.30) with captcha-gated access requests and admin approvals.
- Onboarding via direct V3 API credentials or Telegram Mini App (Vite + React 19 + TypeScript) using Aster Code / Builder authorization (EIP-712 `ApproveAgent` with builder fee cap).
- Recipient-specific notification outbox delivered asynchronously via SQLite queue.

---

## Architecture & Data Flow

The project follows a **modular monolith + state reconciliation + durable execution journal** architecture:

```text
ASTER MASTER (User-Data WebSocket: ORDER_TRADE_UPDATE, ACCOUNT_UPDATE)
    │
    ▼
MasterProjector (deduplication, event ordering, trading episode tracking)
    │ normalized master state delta
    ▼
Coordinator (fans out to followers via FollowerRegistry immutable snapshots)
    │
    ├─► RiskEngine (pure math: equity_base × risk_percent / stop_distance)
    │
    ├─► LeverageManager (RAM candidate catalog + confirmed account leverage)
    │
    └─► Planner (actual vs target diff -> minimal prioritized ExecutionPlan)
            │
            ▼
        Executor (persists commands to SQLite journal, mutation-locked per follower)
            │
            ▼
        AsterClient (EIP-712 signed REST requests, error classification)
            │
            ▼
        ASTER FOLLOWERS
            │
            ▼
        Reconciler (periodic sweep & boot recovery; actual == target -> 0 actions)
```

### Key Architectural Invariants
1. **State Replication, Not Command Mirroring**: WebSocket is a low-latency trigger, but REST snapshots and reconciliations are the authoritative source of truth.
2. **Fail-Closed Safety**:
   - New accounts start in `allow_new_entries=False` (`PAUSED`).
   - Resuming copying requires `SystemState.READY`, a fresh snapshot, and an explicit one-shot confirmation token.
   - Missing or unknown leverage blocks risk-increasing actions (`MARKET_OPEN`) while allowing risk-reducing actions (`MARKET_CLOSE`, cancellations).
3. **Key Isolation & Security**:
   - Master wallet private keys and user wallet signatures are never stored in SQLite, backend memory, or logs.
   - Signer private keys are encrypted using AES-256-GCM (`credentials/crypto.py`) with `CT_ENCRYPTION_KEY`.
   - Temporary builder onboarding attempts use a 4-state durable lifecycle (`PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`) in `builder_onboarding_attempts` and are purged immediately upon commit.
4. **Concurrency & Storage**:
   - Single Python process with `asyncio`.
   - SQLite in WAL mode with `PRAGMA busy_timeout=30000` and a process-local `Database.write_lock` serializing transactions.
   - Per-account `asyncio.Lock` for mutation pipelines (nonces, orders, leverage).

---

## Key Directories

```text
copytradeaster/
├── src/copytradeaster/
│   ├── app.py                 # Composition root, boot recovery sequence & background loops
│   ├── config.py              # Pydantic V2 settings (CT_* env vars) and fail-closed guards
│   ├── logging_utils.py       # Rotating logger (0600 permissions) & secret redaction
│   ├── aster/                 # Aster V3 REST, management API, user-stream WS, EIP-712 signing
│   ├── master/                # MasterProjector: raw WS -> normalized state deltas & episodes
│   ├── risk/                  # RiskEngine: deterministic position sizing & protective math
│   ├── trading/               # Coordinator, Planner, Executor, Reconciler, LeverageManager
│   ├── storage/               # SQLite database, MIGRATIONS list, async repositories
│   ├── credentials/           # SqliteCredentials with AES-256-GCM encryption
│   ├── notifications/         # NotificationRouter, SQLite outbox & TelegramOutboxWorker
│   ├── onboarding/            # Direct & Builder onboarding services (4-state lifecycle)
│   ├── application/           # UserApplicationService (shared use cases), models & control
│   ├── telegram/              # aiogram 3.30 runtime, menu engine, access captcha, routers
│   ├── miniapp/               # aiohttp Mini App server (HMAC initData auth, static bundle)
│   └── cli/                   # main.py CLI dispatcher and subcommands
├── miniapp/                   # Frontend SPA (Vite + React 19 + TypeScript + Oxlint + Vitest)
│   ├── src/screens/           # SessionGate, BuilderWizard, HomeScreen, SuccessScreen
│   ├── src/components/        # UI components (TrustRail, Sheet dialog, SVG icons)
│   └── src/wallet.ts          # Injected EIP-1193 + lazy WalletConnect provider
├── tests/
│   ├── unit/                  # Fast offline unit tests (risk, planner, storage, miniapp, etc.)
│   ├── integration/           # FakeAster e2e, acceptance gates, full Mini App flow
│   ├── replay/                # Stream replay fixtures & snapshot regression tests
│   └── testnet/               # Signed Aster Testnet live tests (CT_TESTNET=1)
├── scripts/                   # Operational scripts (tunnel, preview, load tests, manual orders)
├── docs/                      # Living documentation, architecture & test status
└── data/                      # Local SQLite databases, logs, load test artifacts (git-ignored)
```

---

## Development Commands

### 1. Environment Setup
```bash
# Python backend (Python 3.12)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Frontend Mini App (Node >= 20, pnpm v11+)
cd miniapp
pnpm install
pnpm build               # builds dist bundle served by aiohttp
cd ..
```

### 2. Configuration (`.env`)
Create `.env` from `.env.example`. Key settings:
```env
# Storage & Crypto
CT_DB_PATH=data/copytrade.sqlite3
CT_ENCRYPTION_KEY=<64-char-hex-key>   # python -c "import secrets; print(secrets.token_hex(32))"

# Telegram Control Plane
CT_TELEGRAM_BOT_TOKEN=123456:ABC...
CT_TELEGRAM_ADMIN_IDS=[123456789]

# Mini App Transport (Stage 3)
CT_MINIAPP_ENABLED=true
CT_MINIAPP_STATIC_DIR=miniapp/dist
CT_MINIAPP_PUBLIC_URL=https://<tunnel-hash>.trycloudflare.com

# Testnet Environment (Fail-Closed Guards)
CT_REST_BASE_URL=https://fapi.asterdex-testnet.com
CT_WS_USER_BASE=wss://fstream.asterdex-testnet.com/ws
CT_ALLOW_MAINNET=false
CT_BUILDER_ADDRESS=0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8
CT_BUILDER_NAME=copytradeaster
CT_MAX_FEE_RATE=0.0001
```

### 3. Step-by-Step Launch Flow

#### Step A: Build Frontend Bundle
```bash
cd miniapp && pnpm build && cd ..
```

#### Step B: Start HTTPS Tunnel (for Telegram WebApp)
```bash
# Runs cloudflared quick tunnel on port 8080 and prints public URL:
./scripts/dev-miniapp-tunnel.sh 8080
# Copy the resulting https://<hash>.trycloudflare.com URL to CT_MINIAPP_PUBLIC_URL in .env
```

#### Step C: Initialize Database & Run the Bot
```bash
# Initialize SQLite schema & run migrations
.venv/bin/python -m copytradeaster init-db

# Run unified process (Telegram Bot + Mini App aiohttp + Trading Core)
.venv/bin/python -m copytradeaster run-bot
# or:
copytradeaster run-bot
```
*Note: Upon startup, the bot automatically registers `MenuButtonWebApp(text="ASTER COPY")` pointing to `CT_MINIAPP_PUBLIC_URL/miniapp/` in private chats and for all configured admin IDs.*

### 4. CLI Operations & Health Inspection
```bash
# Health & engine diagnostics (heartbeat age, runtime state, active trades, recent commands)
copytradeaster status

# Run standalone trading core (without Telegram bot)
copytradeaster run

# Force manual reconciliation sweep across followers
copytradeaster reconcile
copytradeaster reconcile --account <account_id>

# Trading pause / resume (safe control-plane)
copytradeaster pause    # sets allow_new_entries=false
copytradeaster resume   # sets allow_new_entries=true & clears emergency_active

# Emergency liquidation (flat verification across active followers)
copytradeaster emergency-close-all --yes

# Telegram user management
copytradeaster telegram-user add <telegram_user_id>
copytradeaster telegram-user list
copytradeaster telegram-user block <telegram_user_id>

# Import credentials from wallet.txt
copytradeaster import-wallet wallet.txt [--wipe]

# Batch approve builder / agent on-chain
copytradeaster approve-builder [--accounts all|<id1,id2>] [--max-fee-rate 0.0001]
```

### 5. Local Dev & Preview Without Telegram
```bash
# Isolated standalone browser preview (MetaMask/Rabby, dev session bypassing Telegram initData)
.venv/bin/python scripts/preview_miniapp.py 8080
# Open in browser: http://127.0.0.1:8080/miniapp/

# Vite live dev server (with hot reloading & backend proxy)
cd miniapp && pnpm dev
```

### 6. Testing & Quality Gates
```bash
# Full offline Python test suite (unit + integration + replay)
pytest -q -m "not testnet"

# With coverage report (threshold >= 87%)
pytest -q -m "not testnet" --cov=copytradeaster --cov-report=term-missing

# Python lint & formatting checks
ruff check .
ruff format --check .
ruff format .             # auto-format

# Frontend quality checks
cd miniapp
pnpm lint                 # oxlint
pnpm test                 # vitest
pnpm build                # tsc + vite build
cd ..

# Signed live Aster Testnet test suite (requires CT_TESTNET=1 and testnet wallet.txt)
CT_TESTNET=1 pytest tests/testnet/ -v
```
---

## Code Conventions & Common Patterns

### Python Backend
- **Type Annotations**: Strict typing on all public methods (`from __future__ import annotations`).
- **Async First**: `async def`, `httpx.AsyncClient`, `aiosqlite`, `asyncio.Lock`.
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes/DTOs, `UPPER_SNAKE` for constants.
- **DTOs & Schemas**: Pydantic V2 models for exchange/API contracts and config; `dataclass(slots=True)` for domain models and storage rows.
- **Dependency Injection**: Explicit protocols and factory injection (e.g. `OnboardingStore`, `ApplicationProtocol`, `Notifier`). No service locators or global singletons.
- **Error Handling**:
  - Typed exceptions extending domain bases (`CopyTradeError`, `OnboardingError`).
  - Exchange errors wrapped with numeric codes (e.g. `-1006 transport timeout`, `-2016 fee rate limit`).
  - Unknown transport outcomes (503/504/-1000/-1006/-1007) are marked `UNKNOWN` and resolved via exchange queries before retrying.
- **Database Migrations**: Add new SQL schema migrations to the `MIGRATIONS` tuple in `src/copytradeaster/storage/database.py`.

### Frontend (Mini App)
- **Framework**: React 19 with strict TypeScript (`tsconfig.app.json`).
- **Styling**: Vanilla CSS tokens in `src/index.css` (Aster Attestation Console dark theme: `#070707` canvas, `#F2C98D` champagne accent). No heavy UI component libraries.
- **Networking**: Centralized `api.ts` with 15-second `AbortController` timeout and typed `ApiError` mapping.
- **Icons & Accessibility**: Inline SVG icons (no system emoji), progress rail connector lines, `:focus-visible`, semantic dialog roles (`role="dialog"`, `aria-modal="true"`).

---

## Important Files

| File | Purpose |
|---|---|
| `src/copytradeaster/app.py` | Composition root, boot sequence, clock sync, and startup reconciliation |
| `src/copytradeaster/config.py` | Application settings, environment loading (`CT_*`), testnet guards |
| `src/copytradeaster/trading/coordinator.py` | Distributes master deltas to follower target positions |
| `src/copytradeaster/trading/planner.py` | Pure transition planner generating execution actions |
| `src/copytradeaster/trading/executor.py` | Journal-driven order execution with persistent command keys |
| `src/copytradeaster/trading/leverage.py` | RAM control-plane managing candidate & confirmed leverage |
| `src/copytradeaster/onboarding/builder.py` | 4-state durable Builder onboarding lifecycle & recovery |
| `src/copytradeaster/application/service.py` | Transport-neutral use cases for Telegram bot, Mini App, and CLI |
| `src/copytradeaster/miniapp/server.py` | aiohttp server hosting Mini App API & static assets |
| `src/copytradeaster/cli/main.py` | CLI entry point and subcommands |
| `miniapp/src/App.tsx` | Mini App routing, session gating, and runtime recovery |
| `docs/ENGINE_TEST_STATUS_2026-08.md` | Single source of truth for test status, counters, and bug registry (#1–26) |
| `docs/ARCHITECTURE.md` | Authoritative system architecture reference |

---

## Runtime & Tooling Preferences

- **Python Runtime**: Python `3.12` (strict).
- **Frontend Tooling**: Node.js `>=20`, package manager `pnpm` (v11+).
- **Linter / Formatter**: `ruff` for Python (`line-length = 100`), `oxlint` for TypeScript/React.
- **Test Runners**: `pytest` + `pytest-asyncio` for Python; `vitest` for frontend.
- **Network Guards**: All development and tests target Aster Testnet (`fapi.asterdex-testnet.com`, trading chain `714`, registration chain `56`). Mainnet access is strictly fail-closed by config guards.

---

## Testing & QA Guidelines

- **Coverage Requirement**: The offline Python test suite must maintain $\ge 87\%$ statement coverage across `src/copytradeaster`.
- **Test File Naming**: `test_<module>_<behavior>.py` under `tests/unit/`, `tests/integration/`, or `tests/testnet/`.
- **Offline Safety**: Default `pytest -q -m "not testnet"` must run hermetically without external network access or real credentials.
- **Live Testnet Protocol**: Live scenarios require `CT_TESTNET=1` and `wallet.txt` containing testnet faucet accounts. Always ensure clean flat positions after tests.
- **Living Documentation**: Update `docs/ENGINE_TEST_STATUS_2026-08.md` after any test suite change. Do not duplicate test counters across other documentation files.

---

## Agent-Specific Instructions

- **Language & Scope**: Keep user communications in Russian, technical, direct, and scoped to API/code facts without marketing filler.
- **Plan Maintenance**: Maintain stage milestones in `docs/DEV_PLAN_SKELETON.md` and record campaign progress in `docs/reports/`.
