# Финальный отчёт follow-up Stage 1 — 2026-08

Дата основного follow-up: 2026-08-22; дополнено full load gate: 2026-08-23
Ветка: `codex/stage1-core`
Основание: `review_stage1_new.md`, `REFACTOR_STAGE1_REPORT.md`,
`docs/ARCHITECTURE.md`, `docs/ENGINE_TEST_STATUS_2026-08.md`.

## Итог

Финальные замечания нового ревью закрыты в коде и тестах. Торговое ядро Stage 1
проверено hermetic-контуром и signed-сценариями на AsterDex futures testnet.
Mainnet и реальные средства не использовались.

Builder e2e не включён в блокеры торгового ядра: для него нет main private keys
в локальном хранилище, а его onboarding относится к Stage 3.

## Что было найдено и исправлено

| Замечание ревью | Реализация | Проверка |
|---|---|---|
| Неуспешный новый Stop мог удалить старый | `ExecutionAction.depends_on_client_order_id`; planner связывает `PLACE_* → CANCEL`, executor блокирует cancel при `FAILED/SUBMITTED` | executor test: старый cancel не вызывается |
| Неизменный target проглатывал повторный drift | Coordinator создаёт новую revision generation только при свежем `actual != target` и старом `CONFIRMED` | acceptance: Stop удалён вручную, новый Stop реально отправлен |
| HEDGE REST rebuild терял защиту без flags | удалено второе `_pick_protective`; добавлен `_closes_leg` предикат | LONG + SELL STOP без `reduceOnly/closePosition` восстановлен |
| Stop-in-profit зависел от старого mark | перед delta App делает symbol-scoped `positionRisk`; projector обновляет mark | acceptance использует REST mark `66500`, Stop `66200` копируется |
| Liquidation/MARGIN_CALL лечились только sweep’ом | `MARGIN_CALL`, `CALCULATED`, autoclose/ADL и неизвестные execution types вызывают immediate callback; App делает rebuild + reconcile | projector и acceptance critical-recovery tests |
| ONE_WAY live proof отсутствовал | добавлен signed ONE_WAY цикл с восстановлением исходного режима | testnet passed; финальный REST snapshot flat |

Дополнительно закрыты два хвоста ревью:

- `NonceStore` резервирует nonce атомарным SQLite upsert с `RETURNING`; добавлен
  concurrent allocation test.
- `entry_quantity()` получает effective leverage, выбранный Coordinator как
  `min(master_leverage, policy.max_leverage)`, и проверяет margin именно по нему.

При проверке также исправлена race post-fill recompute: задача ждёт окончания
текущего account mutation lock и не видит промежуточное состояние до установки
новой защиты.

## Верификация

Hermetic на момент закрытия review-specific доработок:

- `pytest -q --cov=src/copytradeaster --cov-report=term-missing` — **240 passed,
  10 skipped**, total coverage **89%**.
- `ruff check src tests scripts` — clean.
- `ruff format --check src tests scripts` — clean.

После последующей доработки full hybrid load финальное состояние репозитория
проверено повторно: **247 passed, 11 skipped**, coverage **87%**; `ruff check`,
`ruff format --check` и `git diff --check` — clean. Отдельный production-path
load integration `CT_LOAD=1` также прошёл: **1 passed** (30 shadow followers ×
100 детерминированных циклов).

Signed testnet (`CT_TESTNET=1`, локальные credentials, endpoint с `testnet`):

- полный `tests/testnet/test_signed_scenarios.py` — **9 passed, 1 skipped**;
- финальный rerun one-tick replacement + ONE_WAY — **2 passed**;
- Builder — единственный skip из-за отсутствия main private keys;
- read-only REST после cleanup: `main` в HEDGE; `main`, `copyc1`, `copyc2`,
  `copyc3` — без non-zero positions и с `open_orders=0`.

Подробный журнал действий: [FINAL_STAGE1_FOLLOWUP_JOURNAL_2026-08.md](FINAL_STAGE1_FOLLOWUP_JOURNAL_2026-08.md).

## Остаточные ограничения

- Symbol-scoped follower snapshot для масштаба около 30–100 пользователей —
  следующий этап оптимизации; текущий контур рассчитан на testnet и небольшой
  fan-out.
- Зеркалирование висящих лимиток мастера — Stage 2.
- Builder/Aster Code onboarding — Stage 3.

## Собственная дополнительная доработка: full hybrid load

После закрытия review-specific fixes отдельно проверен будущий 30-way fan-out:
100 полноценных циклов на `https://fapi.asterdex-testnet.com`, 3 реальных
подписанных follower-а и 27 shadow slots. Финальный результат — 12 302/12 302
`CONFIRMED`, ingest drops 0, cleanup errors 0 и независимый REST-аудит четырёх
реальных testnet accounts flat/no-orders.

Это отдельный load gate, а не подмена 27 credentials: shadow evidence покрывает
production journal/planner/executor pipeline. Подробный отчёт:
[HYBRID_LOAD_TEST_REPORT_2026-08.md](HYBRID_LOAD_TEST_REPORT_2026-08.md),
журнал действий: [HYBRID_LOAD_TEST_JOURNAL_2026-08.md](HYBRID_LOAD_TEST_JOURNAL_2026-08.md).
