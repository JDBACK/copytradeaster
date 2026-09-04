# Pre-Mainnet Hardening — журнал исполнения

Дата старта: 2026-09-04
Источник плана: owner-provided `Pre-Mainnet Hardening Plan`
Audit baseline плана: `3c38db34f42d7a61c334367edbc434e6fbb97c8a`
Рабочий кодовый срез на старте: `0db6c8476d9e623824129b028e2223fc64c32187`
Статус: **Packages A–B CLOSED — Package C / PM-B4 next**
Рабочая ветка: `codex/live-copy-tests-hardening`

Этот файл — append-only журнал исполнения нового pre-mainnet gate. Актуальные
счётчики suite остаются только в `../ENGINE_TEST_STATUS_2026-08.md`; roadmap и
milestone-чеклисты — в `../DEV_PLAN_SKELETON.md`.

## Цель и границы

Цель — закрыть safety gaps, способные вызвать duplicate execution, неверную
master projection, stale entry или незащищённую follower exposure, не меняя
базовую архитектуру modular monolith + state reconciliation + durable journal.

В scope входят пакеты:

- A — PM-B1: economic idempotency и UNKNOWN outcome;
- B — PM-B2/PM-B3: authoritative master boundary, dirty/generation и startup gate;
- C — PM-B4: immutable Mainnet/Testnet network profile и preflight;
- D — PM-B5/PM-B6/PM-B7: zero reason, naked-position flatten, liquidation safety;
- E — PM-B8 + health convergence: production snapshot envelope и метрики;
- F — signed Testnet qualification, restart/SIGKILL, failure isolation и load evidence.

Вне scope остаются микросервисы, Kafka/Redis/PostgreSQL, event sourcing,
trailing/OCO/OTOCO/multi-TP и другие feature/infrastructure expansions из
исходного плана.

## 2026-09-04 — чтение и первичная сверка

Прочитаны и сверены:

- корневой `AGENTS.md`, `PRODUCT.md`, `docs/AGENTS.md`, `tests/AGENTS.md`;
- `docs/ARCHITECTURE.md`, актуальный roadmap и TL;DR test-status;
- owner-provided hardening plan целиком;
- текущие Executor, Aster adapter/DTO, journal repository и релевантные тесты;
- официальный Aster V3 contract для current order, `allOrders`,
  `positionRisk`, `newOrderRespType=RESULT` и UNKNOWN-ответов.

Подтверждён root cause PM-B1:

1. runtime `_resolve_unknown()` считает только `query_order`;
2. readable `None` разрешает новый COID и повторный `place_order`;
3. ошибка самого recovery query также разрешает retry;
4. boot `resolve_boot_unknowns()` переводит readable `None` в `FAILED`;
5. `SUBMITTED` recovery использует ту же недостаточную модель;
6. Builder DIRECT fallback имеет отдельную retry-ветку и должен быть включён
   в единую UNKNOWN policy.

Существующий неизменённый focused baseline для Executor/Builder/boot/integration
прошёл. Это подтверждает отсутствие регрессий до начала изменений, но не
доказывает экономическую идемпотентность: обязательных accepted-but-response-lost,
query-unavailable и partial-fill сценариев в текущем контракте нет.

## Карта мест рефакторинга и тестирования

| Пакет | Production root | Основные тестовые поверхности |
|---|---|---|
| A | `trading/executor.py`, `aster/trading_api.py`, `aster/models.py`, journal payload/repository; общий runtime/boot resolver | `unit/trading/test_executor_journal.py`, Builder executor/corrective tests, Aster endpoint tests, `integration/fake_aster.py`, boot/E2E integration, отдельный signed fault-injection gate |
| B | `app.py`, `master/projector.py`, `aster/user_stream.py`, coordinator entry admission | projector unit/replay, app lifecycle, reconnect/queue race integration |
| C | `config.py`, App composition, signing, direct/builder onboarding и Mini App composition | config/signing/onboarding unit + production-App integration без order writes |
| D | `domain/models.py`, risk/coordinator/planner/reconciler/leverage | pure risk/planner matrices + FakeAster E2E + signed protection/liquidation scenarios |
| E | follower snapshot path, reconciler, limiter/health/status | production-equivalent shadow/load, failure isolation, latency/weight metrics |
| F | production App + operational testnet harness | signed scenarios, restart/SIGTERM/SIGKILL, final REST/SQLite audit |

## Рабочий TODO

- [x] Прочитать owner plan и обязательные repository contracts.
- [x] Сверить audit baseline с текущим HEAD и локализовать PM-B1 call sites.
- [x] Запустить неизменённый focused baseline.
- [x] Зафиксировать package map, тестовые поверхности и журнал исполнения.
- [x] Package A: ввести явный `FOUND / PROVEN_NOT_EXECUTED / UNRESOLVED`.
- [x] Package A: сохранять pre-mutation position evidence и requested delta.
- [x] Package A: объединить runtime, SUBMITTED и boot MARKET recovery policy.
- [x] Package A: разрешать retry только для доказанного residual quantity.
- [x] Package A: доказать отсутствие обхода через Builder DIRECT fallback.
- [x] Package A: unit + Fake negative + integration regression gate.
- [x] Package A: signed Testnet fault injection + restart/SIGKILL evidence.
- [x] Package B: master dirty/generation/queue/startup gate.
- [ ] Package C: immutable `AsterNetworkProfile` + Mainnet preflight.
- [ ] Package D: zero/protection/leverage-liquidation invariants.
- [ ] Package E: production snapshot envelope + health convergence + launch cap.
- [ ] Package F: финальная signed qualification и Mainnet readiness verdict.

## Решения Package A, требующие подтверждения кодом

- `UNRESOLVED` остаётся non-terminal и никогда не разрешает новую economic mutation.
- MARKET recovery сначала ищет исходный COID в order history, затем сверяет
  authoritative position delta относительно сохранённой pre-mutation позиции.
- Только доказанный `executedQty < requestedQty` создаёт residual retry;
  отсутствие order/history без согласованного position proof недостаточно.
- STOP/TP и cancellation не должны механически наследовать MARKET policy:
  для них доказательство строится по существованию/отсутствию конкретного ордера
  и authoritative open-order state.
- Runtime и boot вызывают один resolver, чтобы restart не менял safety semantics.

## 2026-09-04 — Package A / PM-B1 закрыт

Красная репродукция до исправления доказала оба исходных дефекта: accepted-but-response-
lost MARKET повторялся до max attempts, а недоступный recovery query также разрешал
новый COID. После root-cause fix:

- migration #33 добавляет nullable `execution_commands.recovery_context`; pre-position
  и requested signed delta пишутся до `SENDING`;
- `AsterClient.query_order_history()` использует symbol-scoped `allOrders`; насыщенная
  страница 1000 строк даёт `UNRESOLVED`, а не ложное отсутствие;
- один resolver обслуживает runtime `SENDING/UNKNOWN/SUBMITTED` и boot;
- history/position ambiguity сохраняет non-terminal row и degraded; новый economic
  send возможен только после трёх согласованных history-absent + position-unchanged
  samples;
- partial progress завершает исходную mutation, а реальный Planner/Reconciler создаёт
  только residual transition;
- Builder `-5026 → DIRECT` reduction не имеет отдельного обхода UNKNOWN policy.

Regression matrix покрывает open/close, full/partial progress, unavailable current/
history/position, proven non-acceptance с точным числом send calls, legacy boot rows,
SENDING/UNKNOWN/SUBMITTED, malformed context, Builder fallback и restart semantics.
Полный актуальный offline/coverage счётчик хранится только в
`../ENGINE_TEST_STATUS_2026-08.md`.

Signed Aster Testnet evidence:

1. Aster принял реальный MARKET, response был намеренно отброшен, current-order lookup
   скрыт; resolver нашёл FILLED COID через реальный `allOrders`, `place_order` вызван
   ровно один раз, после cleanup account flat и без open orders.
2. Отдельный Python worker записал recovery context, перешёл в `SENDING`, получил
   успешный ответ реального Aster MARKET и немедленно завершился настоящим `SIGKILL`.
   Новый процесс поднял WAL journal, подтвердил тот же order id через boot resolver,
   не отправил новый order; финальный exchange audit flat/0 orders.

Package A acceptance выполнен: неоднозначный исход никогда не создаёт новый COID.
Следующий разрешённый пакет — B (PM-B2/PM-B3); до его начала требуется красная race-
репродукция stale queued TRADE и startup entry window.

## 2026-09-04 — Package B / красная race-фаза

До production-изменений добавлены и запущены шесть focused regressions. Подтверждены:

- REST snapshot с уже учтённым fill `0.002`, после которого dequeue delayed TRADE
  повторно применяет `0.002`, получает ошибочные `0.004`;
- `UserStream` ждёт `on_connected` до первого `recv`, поэтому frames, накопленные во
  время post-connect snapshot, нельзя пометить старой generation;
- callback немедленного disconnect отсутствует, runtime entry admission не закрывается
  до reconnect;
- bounded ingest queue при overflow молча удаляет oldest raw event и только увеличивает
  counter — dirty marker и немедленный recovery отсутствуют;
- Coordinator знает durable user pause, но не знает process-local startup/reconnect
  admission; при закрытом ожидаемом runtime gate всё равно отправляет MARKET_OPEN;
- post-connect path делает только один reconcile и выставляет stream-ready; close-only
  follower validation перед открытием entry admission отсутствует.

Ожидаемая root-модель: generation/cutoff и dirty принадлежат authoritative master
boundary, receiver читает и тегирует frames параллельно REST rebuild, overflow/
disconnect закрывают только process-local increases, а startup делает close-only sweep
до отдельного entry reconcile. Package B остаётся **IN PROGRESS**.

## 2026-09-04 — checkpoint перед остановкой

Текущий срез зафиксирован в отдельной ветке
`codex/live-copy-tests-hardening`.

- Package A остаётся **CLOSED**: после его закрытия полный hermetic gate
  дал `1107 passed, 2 skipped, 45 deselected`, coverage 87%; два focused signed
  Aster Testnet UNKNOWN/SIGKILL-сценария прошли. Актуальные счётчики и
  детали хранятся в `../ENGINE_TEST_STATUS_2026-08.md`.
- Package B остаётся **WIP / NOT GREEN**. Красная race-репродукция
  выполнена; в working tree есть частичная реализация generation/dirty,
  concurrent WS receive, disconnect/overflow recovery и process-local entry gate. После
  этих production-изменений focused/full tests ещё не подтверждались.
- На checkpoint прошли только static sanity checks: targeted `ruff check`,
  `ruff format --check`, Python `compileall` и `git diff --check`. Это не заменяет
  функциональные тесты Package B.
- Этот checkpoint не является Mainnet readiness verdict и не разрешает
  live trading.

Точка продолжения Package B:

- [x] Исправить READY transition: не терять pre-boundary state и не слать
  повторные READY notifications на каждом boundary.
- [x] Обновить app-lifecycle fakes под `mark_dirty`, envelope/generation и
  `clear_degraded`.
- [x] Запустить шесть focused race-tests после implementation, исправить все
  падения и добавить negative/repeated reconnect cases.
- [x] После focused green запустить полный `not testnet` suite с coverage,
  `ruff check`, `ruff format --check` и `git diff --check`.
- [x] Только после этого закрывать Package B и переходить к Package C.

## 2026-09-04 — Package B / PM-B2 + PM-B3 закрыт offline

После checkpoint шесть исходных красных race-регрессий доведены до green,
а соседние error/boundary/repeated cases добавлены по реальному контракту.

Реализовано:

- `MasterProjector` публикует новую generation только после всех REST-чтений
  и durable episode sync; неудачный snapshot сохраняет прежние state/
  generation и оставляет master dirty;
- frames, полученные до/во время boundary, не могут мутировать новый state;
  delayed current-generation mutation сверяется с cutoff в Aster server-time;
- граница выбрана явно: `< cutoff` разрешал бы event в той же millisecond;
  `<= cutoff` считает его не доказанно post-snapshot и требует rebuild, что
  соответствует fail-closed wording PM-B2;
- `UserStream` запускает receive до connected-boundary callback и сообщает
  disconnect до завершения медленного callback;
- App envelope несёт generation + monotonic receive time; queue overflow и backlog
  старше 5 s синхронно закрывают local entry gate, ставят dirty и
  coalesce recovery; `age == 5s` ещё fresh, `age > 5s` stale — это совпадает
  с существующим Coordinator master-freshness contract;
- startup/reconnect boundary ограничен тремя coalesced passes: master snapshot →
  close-only follower sweep → финальный master snapshot → entry reconcile; при
  continuous events, disconnect, dirty или in-flight commands READY не публикуется;
- process-local admission требует одновременно READY, live stream и clean
  projector; closes/reductions проходят при закрытом gate;
- `ACCOUNT_CONFIG_UPDATE` обновляет обе HEDGE legs и запрашивает authoritative
  rebuild; position-mode update больше не остаётся только log-line.

Проверены целые затронутые unit/replay/integration files, отдельная
App + real Projector + SQLite + FakeAster queue/rebuild integration и полный
hermetic backend suite с documentary coverage gate. Актуальные счётчики
хранятся только в `../ENGINE_TEST_STATUS_2026-08.md`; ruff, format,
compileall и diff-check clean.

Package B acceptance закрыт на offline/hermetic уровне. Signed Testnet Snapshot/WS,
startup-change и reconnect-resnapshot failure scenarios остаются в Package F;
это не является Mainnet readiness verdict. Следующий пакет — C / PM-B4.
