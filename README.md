# CopyTradeAster

Небольшой асинхронный copy-trade bot для AsterDex Futures V3: один master и до
10 REAL follower accounts в текущем fail-closed launch-cap (`CT_MAX_FOLLOWERS=10`);
масштаб 30–100 остаётся будущей целью после новых замеров.
Stage 1 trading core, Stage 2 Telegram-контур и Stage 3 (Builder onboarding +
follower Mini App) работают как один modular monolith на Python 3.12, asyncio,
aiogram 3.30.0 и SQLite WAL.

Stage 2 включает private inline Telegram UI, captcha + admin approval, direct
V3 onboarding, риск на Stop, pause/resume, account lifecycle, админку и
Telegram notification outbox. Trading core Stage 1 при этом не переписан.

Stage 3 добавляет комбинированный ApproveAgent (Builder), account attestation и
follower Mini App: 4-табавая оболочка, история сделок, activity feed и live-
обновление позиций/PnL без штатной кнопки обновления (данные приходят по
authenticated live-каналу, единственный ручной жест — pull-to-refresh).

> Текущий запуск и acceptance выполняются на Aster testnet; mainnet и реальные
> средства остаются вне scope. Engine уже зеркалирует `CROSSED`/`ISOLATED` и
> применяет isolated safety-gates, а mainnet rollout по-прежнему fail-closed.

## Быстрый старт для разработки

```bash
uv sync                                    # .venv + dev-группа; Python 3.12 uv ставит сам
uv run pytest -q -m "not testnet"
uv run ruff check src tests scripts
```

`uv.lock` лежит в репозитории: окружение воспроизводится из него, а не из
текущего состояния PyPI. Dev-инструменты (pytest, ruff, respx, coverage) — в
PEP 735 группе `dev`, поэтому обычный `uv sync` без флагов ставит и их.

Signed testnet-сценарии требуют локальных testnet credentials и явного
`CT_TESTNET=1`. Приватные ключи, `.env`, `wallet.txt` и run-БД не должны
попадать в git.

Запуск общего engine + Telegram polling process:

```bash
uv run python -m copytradeaster run-bot
```

Пересобрать окружение после изменения зависимостей: `uv sync` (или `uv add`
/`uv remove` для правки `pyproject.toml` и lockfile в одном шаге).

Начальная точка документации — [docs/AGENTS.md](docs/AGENTS.md). Текущий
статус торгового ядра ведётся только в
[docs/ENGINE_TEST_STATUS_2026-08.md](docs/ENGINE_TEST_STATUS_2026-08.md), а
workflow и safety-инварианты — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
`STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` — исторический snapshot
Stage 2; его старые ограничения не переопределяют текущую реализацию.
