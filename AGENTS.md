# Repository Guidelines

## Overview

This repository hosts research and development for an AsterDex (V3 API) copy-trade bot: a Telegram bot that mirrors a master trader's positions for subscribers. It is currently in the research phase and evolving into a Python implementation.

## Project Structure & Module Organization

- Root-level Markdown files - research reports and planning docs (e.g., `ASTER_API_RESEARCH_2026-08.md`, `DEV_PLAN_SKELETON.md`). Name new reports `TOPIC_YYYY-MM.md` and log research dates in the report journal.
- `.firecrawl/` - Firecrawl scrape cache. Ignored by git; never commit.
- `firstcontext.md` - the original project brief (the user's requirements for the bot).
- `wallet.txt` - local key assembly: main account + 4 copy accounts (currently testnet), ready for integration runs. Local-only, git-ignored.
- Source code will live under `bot/` (aiogram app, entry point `python -m bot`) with modules for trading (`bot/trading/`), risk (`bot/risk/`), and notifications (`bot/notifications/`).
- `tests/` - unit and integration tests (pytest).

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

- Use pytest with pytest-asyncio for async tests; target at least 80% coverage on `bot/trading` and `bot/risk`.
- Name tests `test_<unit>_<behavior>.py`, e.g. `test_risk_position_sizing.py`.
- Run: `pytest --cov=bot`.
- Integration tests run only against the testnet (`fapi.asterdex-testnet.com`) - never mainnet or real funds.

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
