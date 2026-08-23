# Repository Guidelines

## Overview

AsterDex (V3 API) copy-trade bot: mirrors a master trader's positions on follower accounts. Stage 1 (trading core, testnet) is implemented in Python under `src/copytradeaster/`.

## Project Structure & Module Organization

- `docs/` - living documentation. Start with `docs/docs_map.md` - it explains what every document is and when to read it.
  - `docs/ARCHITECTURE.md` - architecture; source code docstrings reference its sections by number.
  - `docs/DEV_PLAN_SKELETON.md` - stage roadmap and checklists.
  - `docs/ENGINE_TEST_STATUS_2026-08.md` - THE single source of truth for current engine status: suite counters, fixed bugs #1-16, test campaigns. Update it after every meaningful change; do not duplicate its counters elsewhere.
  - `docs/ASTER_API_RESEARCH_2026-08.md` - Aster V3 API reference facts.
  - `docs/reports/` - per-campaign historical reports and run journals (factual snapshots; do not rewrite them retroactively).
  - `docs/reviews/` - verbatim external code-review texts (never edit or rephrase them).
  - `docs/history/` - executed plan archive and the original project brief.
  - New reports go to `docs/reports/`, named `TOPIC_YYYY-MM.md`, with research dates logged inside.
- `.firecrawl/` - Firecrawl scrape cache. Ignored by git; never commit.
- `wallet.txt` - local key assembly: main account + copy accounts (currently testnet). Local-only, git-ignored.
- `src/copytradeaster/` - the package: `aster/` (REST client, signing), `master/` (projector of the master stream), `trading/` (coordinator/planner/executor/reconciler), `risk/`, `storage/` (SQLite repos + migrations), `credentials/`, `notifications/`, `cli/`, entry points `app.py` / `__main__.py`.
- `tests/` - pytest suites: `unit/`, `integration/` (FakeAster e2e), `testnet/` (signed, marker-gated by `CT_TESTNET=1`), `replay/`.
- `scripts/` - operational scripts (`hybrid_load_test.py` etc.).
- `data/` - SQLite databases and load-run artifacts.

## Build, Test, and Development Commands

No build step is configured yet; the commands below apply once code lands.

- `python -m venv .venv` - create a virtual environment (Python 3.12).
- `pip install -e ".[dev]"` - install the package with dev dependencies.
- `pytest` - run the full test suite.
- `ruff check .` and `ruff format .` - lint and auto-format.

## Coding Style & Naming Conventions

- Python: PEP 8, 4-space indentation, snake_case for functions and variables, UPPER_SNAKE for constants, type hints on all public functions.
- Async-first code: `async def`, httpx async client, aiosqlite.
- Never hardcode API keys, addresses, or secrets; use a `.env` file (git-ignored).

## Testing Guidelines

- Use pytest with pytest-asyncio for async tests; current coverage gate is 87% (`src/copytradeaster`).
- Name tests `test_<unit>_<behavior>.py`, e.g. `test_risk_position_sizing.py`.
- Run: `pytest -q -m "not testnet"` (offline suite); signed testnet scenarios additionally require `CT_TESTNET=1`.
- Signed runs go only against the testnet (`fapi.asterdex-testnet.com`) - never mainnet or real funds.

## Commit & Pull Request Guidelines

- Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`.
- One logical change per commit; write pull request descriptions in Russian with a short summary, the linked issue, and what was tested (testnet order/WS flow).
- Branch prefix: `codex/`.

## Security & Configuration Tips

- Private keys never enter the database; store them encrypted and keep secrets out of logs.
- `wallet.txt` holds private keys in plaintext (main + copiers, currently testnet). Keep it local-only; migrate to encrypted storage as the code lands.
- Use testnet faucet funds; verify every flow (order, builder approve, user-stream WebSocket) on testnet before touching production.
- A VPS with a static IP is enough for outgoing WebSockets; configure the API `ipWhitelist`.

## Agent-Specific Instructions

- Web research: use Firecrawl only (skills `firecrawl-search`, `firecrawl-scrape`), never the built-in web search.
- Keep answers in Russian, direct, and scoped to API facts - no ethics or marketing tangents.
- Maintain the todo-plan in `DEV_PLAN_SKELETON.md` and record research progress in report journals.
