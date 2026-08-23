# Журнал hybrid load campaign — 2026-08

Цель: перед передачей Stage 1 на ревью проверить будущий масштаб в 30
follower slots, не создавая 30 реальных кошельков вручную. Реальные торговые
действия выполнялись только на Aster futures testnet локальными ключами из
`wallet.txt`; недостающие slots — только явный `SHADOW` execution mode в
отдельной run-БД.

## 2026-08-22 — подготовка

- Изучены `AGENTS.md`, `docs/`, architecture, предыдущий refactor report,
  engine status и `review_stage1_new.md`.
- Зафиксирована исходная конфигурация: `main` + `copyc1`–`copyc3` REAL,
  HEDGE, testnet; исходные кошельки flat/no-orders.
- Принято решение: 3 REAL signed followers + 27 SHADOW followers; fake
  credentials не создаются и не попадают в credentials table.
- Добавлены `execution_mode`, `ShadowExchange`, `scripts/hybrid_load_test.py`,
  отчётный JSON/audit и safety gates testnet-only/preflight/cleanup.
- Добавлен production-path integration test `CT_LOAD=1`: 30 shadow followers,
  100 deterministic cycles, passed.

## 2026-08-22/23 — live testnet итерации

1. Pilot на 1 cycle: **passed**, 30 loaded, 96 commands confirmed, dropped
   events 0, cleanup/audit clean.
2. Run на 100 cycles остановился на 20/100 из-за `STALE_MASTER`; freshness
   стала сниматься один раз на весь master-delta fan-out.
3. Следующий run остановился на 3/100 из-за transport timeout master close;
   добавлены query-by-client-order-id, position-state recovery, residual
   quantity и bounded retry.
4. Следующий run остановился на 28/100 при запаздывающем master close event;
   добавлен authoritative `rebuild_full + reconcile` fallback. Cleanup clean.
5. Прогон после этого дошёл до 26/100 и обнаружил `database is locked` в
   смешении aiosqlite writes и отдельного synchronous nonce connection.
   Добавлены autocommit, busy timeout, nonce cursor finalization и process
   serialization.
6. Следующий run дошёл до 35/100: реальный follower получил корректный
   `-2022 ReduceOnly` на stale close после protective fill. Добавлена
   authoritative flat-state проверка; non-flat leg по-прежнему считается
   failed.
7. Deterministic run дошёл до 57/100: живая testnet mark price пересекла
   synthetic shadow stop у 11 slots. Это не баг API, но недетерминированный
   фактор для scale acceptance; добавлен явный `shadow_auto_trigger_conditionals`
   load flag. Unit-test auto-trigger сохранён.
8. Прогон с прежней cursor-схемой подтвердил, что lock ещё возможен; процесс
   остановлен interrupt после штатного cleanup.
9. Storage переведён на единый aiosqlite connection: `Database.write_lock`,
   `execute_write`, закрытие всех read/write cursors и atomic nonce
   `INSERT...RETURNING`. Stress: 600 commands + 600 nonce allocations passed.

## 2026-08-23 — финальный acceptance-run

Команда:

```text
./.venv/bin/python scripts/hybrid_load_test.py \
  --followers 30 --cycles 100 --timeout 180
```

Результат:

- report: `data/hybrid-load-20260822T233258Z.json`;
- 100/100 cycles;
- 30 followers = 3 REAL + 27 SHADOW;
- 12 302/12 302 `CONFIRMED` до cleanup;
- 2 master unknown outcomes, 2 retries, без unresolved command;
- 0 ingest drops, 0 shadow signed requests;
- run audit до и после cleanup: 31/31 flat/no-orders;
- cleanup errors: 0;
- независимый REST-аудит исходной БД: `main`, `copyc1`, `copyc2`, `copyc3` —
  positions empty, `open_orders=0`.

Суммарное время 100 циклов — 1500.862 s, среднее — 15.009 s.

## 2026-08-23 — финальная автоматическая проверка

- Full suite: **247 passed, 11 skipped**, coverage **87%**.
- `CT_LOAD=1` shadow-scale integration: **1 passed**, 13.83 s.
- `ruff check src tests scripts`: clean.
- `ruff format --check src tests scripts`: clean.
- `git diff --check`: clean.

## Вывод

Гейт 30-slot hybrid load пройден. В отчёте явно разделены доказательства:
3 аккаунта проверены реальным signed testnet path; 27 аккаунтов проверены
production journal/planner/executor path без signing. Для перехода к 30-real
нагрузке нужны 27 независимых testnet accounts/credentials, а не fake keys.
