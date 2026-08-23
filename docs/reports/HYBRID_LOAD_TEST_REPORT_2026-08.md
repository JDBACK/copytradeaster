# Отчёт hybrid load campaign — 2026-08

Дата отчёта: 2026-08-23
Ветка: `codex/stage1-core`
Основание: `review_stage1_new.md`, `REFACTOR_STAGE1_REPORT.md`,
`docs/ARCHITECTURE.md`, `docs/ENGINE_TEST_STATUS_2026-08.md`.

## Итог

Нагрузочный acceptance-run на AsterDex futures testnet успешно завершён:

- **30 follower slots**: 3 реальных testnet follower-а (`copyc1`–`copyc3`) и
  27 явно помеченных `SHADOW` follower-ов;
- **100 циклов** master open → follower copy → protection → master stop cancel
  → master close → follower flatten;
- **12 302 / 12 302 команд `CONFIRMED`** до cleanup;
- `FAILED`, `UNKNOWN`, `SENDING`, `SUBMITTED` и `BLOCKED` в финальной run-БД —
  **0**;
- `ingest_dropped=0`, cleanup без ошибок;
- независимая signed REST-сверка после cleanup: `main`, `copyc1`, `copyc2`,
  `copyc3` — без non-zero positions и с `open_orders=0`.

Это достаточное evidence для передачи текущих Stage 1 доработок на ревью в
части 30-way fan-out, journal/idempotency и реального testnet-пути трёх
кошельков. Это **не** доказательство того, что 30 разных Aster credentials
одновременно подписывают запросы: 27 аккаунтов намеренно shadow.

## Что именно проверялось по новому ревью

| Область из `review_stage1_new.md` | Итоговая реализация | Evidence |
|---|---|---|
| Safety при замене Stop/TP | зависимый `CANCEL_ORDER` разрешён только после `PLACE_* = CONFIRMED` | executor/planner tests, signed replacement |
| Новая generation при фактическом drift | revision в `sync_basis` меняется для нового desired-state transition | acceptance/reconciliation tests |
| HEDGE REST rebuild | защита определяется по leg/`positionSide`, а не только по reduce-only flags | projector payload tests, signed HEDGE flow |
| Свежий mark price | перед decision по delta выполняется symbol-scoped authoritative `positionRisk` refresh | acceptance/testnet flow |
| MARGIN_CALL/liquidation/autoclose | critical execution types вызывают rebuild + reconcile | projector/acceptance tests |
| Nonce | атомарный `INSERT ... ON CONFLICT ... RETURNING` под общим DB write lock | concurrent nonce test, live signed run |
| Post-fill race | per-account lock закрывает snapshot → plan → submit generation; callback async и ждёт idle | coordinator tests, live run |
| WS readiness | `READY` публикуется после успешного user-stream handshake | user-stream tests, live run |

## Load-specific доработки

1. В `accounts` добавлен `execution_mode` (`REAL`/`SHADOW`). Shadow rows не
   требуют credentials, не допускаются для `main` и разрешены только при
   testnet endpoint и явном `CT_SHADOW_ACCOUNTS_ENABLED=true`.
2. `ShadowExchange` реализует только exchange surface, используемый
   coordinator/executor: positions, conditional orders, fills, cleanup и
   metrics. У него нет HTTP/signing/credentials path; в финальном run отчётный
   счётчик `shadow_signed_requests=0`.
3. `scripts/hybrid_load_test.py` создаёт отдельную run-БД, клонирует только
   зашифрованные REAL credentials, делает read-only preflight, запускает
   production `App`, а в `finally` отменяет ордера, flatten-ит accounts,
   восстанавливает position modes и пишет audit.
4. Для обычного production snapshot по умолчанию остаётся all-symbol
   authoritative fetch. Load profile явно ограничен `BTCUSDT`, чтобы измерять
   30-way execution/journal fan-out без искусственного умножения REST weight.
5. SQLite переведён на explicit autocommit; read/write cursors закрываются в
   `finally`, repository writes идут через `Database.execute_write`, а nonce
   allocation использует тот же `Database.write_lock` и connection. Это
   устранило найденный live `database is locked` при смешении aiosqlite и
   отдельного nonce connection.
6. Executor распознаёт gateway timeout variants как outcome-unknown и делает
   query-before-retry. Отдельно `-2022 ReduceOnly` после race с protective fill
   подтверждается только после authoritative `positionRisk`, если leg уже
   flat; non-flat leg остаётся `FAILED`.
7. Для deterministic shadow load auto-trigger synthetic protections отключён.
   Protection orders всё равно реально проходят placement, verification,
   cancel и flatten через planner/executor. Отдельный unit-test сохраняет
   проверку auto-trigger поведения ShadowExchange.

## Финальный signed hybrid run

Run report: [data/hybrid-load-20260822T233258Z.json](data/hybrid-load-20260822T233258Z.json)
Run DB: [data/hybrid-load-20260822T233258Z.sqlite3](data/hybrid-load-20260822T233258Z.sqlite3)
Runner: [scripts/hybrid_load_test.py](scripts/hybrid_load_test.py)

| Метрика | Результат |
|---|---:|
| Endpoint | `https://fapi.asterdex-testnet.com` |
| Followers loaded | 30 |
| REAL followers | 3 |
| SHADOW followers | 27 |
| Cycles | 100 / 100 |
| Суммарное время 100 циклов | 1500.862 s (~25.0 min) |
| Среднее время цикла | 15.009 s |
| Min / max cycle | 12.908 s / 25.363 s |
| Commands before cleanup | 12 302 `CONFIRMED` |
| Master unknown outcomes | 2; оба обработаны retry path |
| Master retries | 2 |
| Authoritative recoveries | 0 в финальном run |
| WS ingest dropped | 0 |
| Shadow signed requests | 0 |
| Cleanup errors | 0 |
| Audit before cleanup | 31/31 flat, no open orders |
| Audit after cleanup | 31/31 flat, no open orders |

Shadow metrics за run: `place_order=8100`, `cancel_order=2700`,
`position_risk=24891`, `open_orders=27591`, `balance=18087`,
`set_leverage=27`. Эти числа подтверждают прохождение shadow followers через
production execution path, но не являются запросами к Aster.

Независимый REST-аудит исходной БД после завершения run:

```text
endpoint: https://fapi.asterdex-testnet.com
main:   positions=[], open_orders=0
copyc1: positions=[], open_orders=0
copyc2: positions=[], open_orders=0
copyc3: positions=[], open_orders=0
```

## Тестовый контур

- `pytest -q --cov=src/copytradeaster --cov-report=term` — **247 passed,
  11 skipped**, total coverage **87%**;
- `CT_LOAD=1 pytest -q tests/integration/test_shadow_scale.py` — **1 passed**,
  30 shadow followers × 100 deterministic cycles, 13.83 s;
- `ruff check src tests scripts` — clean;
- `ruff format --check src tests scripts` — clean;
- `git diff --check` — clean;
- отдельный storage stress: **600 journal commands + 600 nonce allocations** —
  `600/600 CONFIRMED`, 3 monotonic nonce rows.

Hermetic tests не подменяют live evidence: они проверяют инварианты planner,
coordinator, executor и journal без сети; финальный hybrid run выше использовал
реальные подписанные запросы только для имеющихся testnet-кошельков.

## Что было найдено во время live-прогонов

Промежуточные неуспешные прогоны не скрыты:

- `STALE_MASTER` на 20/100 — freshness capture делался повторно внутри
  медленного fan-out; исправлено снимком freshness на generation;
- transport timeout master close — добавлен query/position residual recovery;
- потерянный/запаздывающий master event — добавлен authoritative rebuild +
  reconcile fallback в runner;
- `database is locked` на 26–35/100 — устранены implicit transactions,
  cursor leaks и cross-connection nonce allocation;
- `-2022 ReduceOnly` на запоздалом close после protective fill — добавлена
  проверка фактического flat state;
- shadow stop пересекался с живым testnet mark price — load profile переведён
  в явно deterministic shadow mode, а auto-trigger отдельно оставлен
  покрытым unit-test-ом.

Каждый остановленный run завершал cleanup; перед финальным запуском и после
него исходные реальные кошельки дополнительно проверялись signed REST.

## Ограничения перед следующим этапом

- Для полноценного 30-real test нужны ещё 27 независимых Aster testnet
  accounts/credentials и faucet balances. Генерировать fake keys и считать их
  signed evidence нельзя.
- Финальный load profile проверял только `BTCUSDT` и HEDGE position mode.
  Normal production snapshot остаётся all-symbol; multi-symbol load следует
  проводить после выдачи дополнительных реальных ключей и согласования REST
  weight budget.
- Shadow deterministic mode не проверяет случайное срабатывание защиты от
  рынка; этот сценарий покрыт отдельно unit-тестом, а реальные три follower-а
  в live run использовали настоящие testnet conditional orders.
- Зеркалирование висящих лимиток мастера остаётся Stage 2; Builder onboarding
  остаётся Stage 3.

## Решение

Hybrid load gate пройден. Код и отчёт готовы к передаче на ревью с указанными
ограничениями. Mainnet и реальные средства не использовались.
