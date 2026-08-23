# CopyTradeAster

Небольшой асинхронный copy-trade engine для AsterDex Futures V3: один master
и до 100 follower accounts. Stage 1 реализован как modular monolith на Python
3.12, asyncio и SQLite.

> Сейчас проект работает только с Aster testnet. Mainnet и реальные средства
> не входят в Stage 1.

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

Начальная точка документации — [docs/docs_map.md](docs/docs_map.md). Текущий
статус торгового ядра ведётся только в
[docs/ENGINE_TEST_STATUS_2026-08.md](docs/ENGINE_TEST_STATUS_2026-08.md).
