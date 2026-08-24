# CopyTradeAster

Небольшой асинхронный copy-trade bot для AsterDex Futures V3: один master,
10–30 пользователей в первом запуске и максимум около 100 follower accounts.
Stage 1 trading core и Stage 2 Telegram-контур работают как один modular
monolith на Python 3.12, asyncio, aiogram 3.30.0 и SQLite WAL.

Stage 2 включает private inline Telegram UI, captcha + admin approval, direct
V3 onboarding, риск на Stop, pause/resume, account lifecycle, админку и
Telegram notification outbox. Trading core Stage 1 при этом не переписан.

> Проект работает только с Aster testnet. Mainnet, реальные средства, Builder,
> Mini App UI и принудительный Isolated margin mode в текущий scope не входят.

## Быстрый старт для разработки

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q -m "not testnet"
.venv/bin/ruff check src tests scripts
```

Signed testnet-сценарии требуют локальных testnet credentials и явного
`CT_TESTNET=1`. Приватные ключи, `.env`, `wallet.txt` и run-БД не должны
попадать в git.

Запуск общего engine + Telegram polling process:

```bash
.venv/bin/python -m copytradeaster run-bot
```

Начальная точка документации — [docs/docs_map.md](docs/docs_map.md). Текущий
статус торгового ядра ведётся только в
[docs/ENGINE_TEST_STATUS_2026-08.md](docs/ENGINE_TEST_STATUS_2026-08.md).
Фактический scope и честный readiness verdict Stage 2 — в
[docs/reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md](docs/reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md).
