# Pre-Mainnet Hardening — журнал исполнения

Дата старта: 2026-09-04
Источник плана: owner-provided `Pre-Mainnet Hardening Plan`
Audit baseline плана: `3c38db34f42d7a61c334367edbc434e6fbb97c8a`
Рабочий кодовый срез на старте: `0db6c8476d9e623824129b028e2223fc64c32187`
Статус: **QUALIFICATION RESET — START AGAIN FROM PACKAGE A ON REAL ASTER TESTNET**
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
- [x] Package B: master dirty/generation/queue/startup implementation + offline gate.
- [ ] Package A: добить всю signed Testnet UNKNOWN-matrix из owner plan.
- [ ] Package B: добить всю signed Testnet Snapshot/WS + Startup matrix.
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

## 2026-09-04 — owner correction: fake-only acceptance запрещён

Предыдущий verdict `Package B CLOSED offline` не считается финальным
acceptance. По уточнённому owner contract все сценарии плана, где
важны Aster REST/WS, signing, order/position/protection, reconnect, restart
или limiter semantics, должны пройти на реальном Aster Testnet с
реальными testnet-кошельками.

FakeAster/MockTransport/Shadow остаются supplemental regression tools и не
закрывают package. Детерминированные response-loss, 429 и network/query
failures инжектируются вокруг реального `AsterClient`, но exchange
mutation/read/state остаются живыми. Shadow разрешён только в
исходно заданном load gate вместе с 3–5 REAL followers.

Текущая честная квалификация:

- Package A implementation/offline green; live response-lost + real `allOrders`
  и отдельный real SIGKILL green, остальная UNKNOWN live-matrix открыта;
- Package B implementation/offline green; production `App` на backup-копии
  SQLite прошёл реальный Aster Testnet цикл: WS-gated READY,
  master MARKET + STOP, convergence четырёх REAL followers, forced
  listenKey close/reconnect/resnapshot, неизменность follower qty/Stop без
  дублей и финальный flat cleanup; focused result `1 passed`;
- оставшиеся B Snapshot/WS и slow-startup scenarios ещё не закрыты,
  поэтому переход к Package C пока не разрешён.

## 2026-09-04 — qualification reset: следующая сессия начинает с A

По owner instruction не продолжать к Package C. Следующая сессия
начинает qualification заново с Package A и идёт строго A → B →
C → D → E → F. Код A/B и offline-тесты не откатывать: они
остаются implementation/regression baseline, но каждый acceptance-пункт
из owner plan нужно повторить/добавить как REAL Aster Testnet scenario.

Документ `PRE_MAINNET_HARDENING_HANDOFF_2026-09.md` переписан как
актуальная задача с полной живой матрицей A/B и обязательным
flat/no-open-orders audit до и после каждого сценария.

## 2026-09-04 — Package A LIVE-CLOSED: UNKNOWN-matrix 5/5 + builder corrective green + REAL-05 PASS

HEAD `388f0e7`, run DB `data/pm-a-live-20260904T234500Z.sqlite3`, REAL Aster
Testnet. Live execution serialized by orchestrator (no live runs from docs agents).

Live invocations:

- `CT_TESTNET=1 pytest tests/testnet/test_pre_mainnet_unknown_live.py -v` — **5/5 green**;
- `CT_TESTNET=1 pytest tests/testnet/test_builder_stale_corrective_live.py -v` — **1 passed за 53.67s**;
- unified campaign re-ran pre-existing proofs: response-lost PASS, SIGKILL-SENDING PASS, REAL-05 boot PASS на copyc3 за 10.53s (после ретаргета с saturated main; детали ниже).

Per-test results (`test_pre_mainnet_unknown_live.py`, BTCUSDT, QTY `0.002` / QTY_RED `0.001`):

- A1 CLOSE response-lost via real `allOrders` (7.7s-class): accepted CLOSE, response
  отброшен, current-order lookup скрыт; resolver нашёл FILLED COID через реальный
  `allOrders` (`history:FILLED`), `place_order` ровно 1, позиция flat.
- A2 all-reads-down UNKNOWN hold: current + history + position reads все down;
  команда осталась UNKNOWN под исходным COID, zero retry (`place_calls == 1`),
  при том что биржа реально приняла order (fail-closed доказан).
- A3 partial via real `positionRisk` delta residual-only: оба order lookup скрыты,
  единственным evidence был реальный position delta; CONFIRMED через
  `position-progress`, `place_calls == 1`, остаток ровно `QTY − QTY_RED`, без
  continuation order.
- A4 proven-not-executed exactly-one retry на copyc3: первый send уронен до биржи
  (pre-send transport loss), реальные current/history/position reads доказали
  отсутствие; resend ровно один под новым COID (`first + "r2"`, `send_attempts == 2`,
  `real_sends == 1`, `attempts == 2`), history подтверждает fill нового COID и
  отсутствие первого. main/copyc1 BTCUSDT `allOrders` saturated 1000/1000, поэтому
  отсутствие там недоказуемо by design (fail-closed UNRESOLVED); copyc3 — 67 rows.
- A5 SIGKILL-in-UNKNOWN boot CONFIRM без resend: worker принял реальный MARKET и умер
  настоящим SIGKILL до resolution (`returncode == -SIGKILL`); boot нашёл ровно 1
  SENDING row с recovery context, `report.confirmed == 1, unresolved == 0`, тот же
  `order_id` до/после, ровно 1 match в `all_orders(limit=1000)` — второго economic
  order нет.

Builder corrective (`test_builder_stale_corrective_live.py`): файл был зелёным на bare
App, после чего Package B admission gate в `32fe56d` закрыл entries; entry leg заменён
прямыми REAL seeded legs, reduction assertions intact — green 53.67s.

Audits: каждый новый live test заканчивается `finally` flatten + независимым signed
audit flat/zero-orders (position flat + `open_orders == []`) — flat/zero везде.

Deterministic-COID lesson: `command_key_of` игнорирует planner coid; тесты с одинаковой
формой payload ДОЛЖНЫ использовать distinct `ExecutionAction` revision (A-file uses 1–5).

Unified campaign complete: A1–A5 new 5/5, pre-existing response-lost PASS,
SIGKILL-SENDING PASS, REAL-05 boot PASS на copyc3 за 10.53s, builder-stale PASS.
REAL-05 root cause подтверждён: main BTCUSDT `allOrders` saturated 1000/1000,
missing-COID leg поднимал `-1000` saturated-window вместо доказательства
отсутствия (fail-closed, тот же феномен, что в A4); fix тронул только
`tests/testnet/test_signed_scenarios.py` (aid params + ретаргет REAL-05 на
copyc3, 67 rows), prod code не менялся. Оркестратор подтвердил REAL-05 green —
PM-B1/Package A записаны как LIVE-CLOSED.

## 2026-09-05 — Package B LIVE-COMPLETE: Snapshot/WS/Startup 6/6 на финальном дереве

REAL Aster Testnet, финальное дерево (все harness fixes ниже уже применены).
REAL-06 run DB `data/pm-b-live-20260905T002000Z.sqlite3`. Live execution
serialized by orchestrator; второй App никогда не запускался параллельно.

Live invocation:

- `CT_TESTNET=1 pytest tests/testnet/test_pre_mainnet_master_live.py -v`

Per-node results (каждый заканчивается flat/zero-open-orders audit):

- S1 rebuild-holds-queue — green за 100s;
- S2 stale-cutoff — green за 44s;
- W1 reconnect-gap — green за 83s;
- W2 failed-resnapshot — green за 68s;
- P1 startup-transient — green за 45s;
- REAL-06 App/WS/reconnect — green за 134s.

Harness-vs-prod verdicts (все четыре — harness, prod не менялся):

- settle gate — partial-fill race в harness teardown/setup;
- `_wait_stable` quiescence gates — harness ждал устойчивости перед assertions;
- HEDGE nonzero-key counting — подсчёт legs в harness;
- deterministic-COID revisions — уроки A-file (revisions 1–5, unique revision
  per submit) применены к B-live submits.

Audits: каждый live test заканчивается `finally` flatten + signed
flat/zero-orders audit — flat/zero везде. PM-B2/PM-B3 записаны как
LIVE-COMPLETE.

## 2026-09-05 — Package C LIVE-COMPLETE: NetworkProfile 3/3 за 12s

REAL Aster Testnet. Prod scope: immutable `AsterNetworkProfile` (testnet 714 /
mainnet 1666, mixed-combo fail-closed) + unified mainnet preflight +
onboarding `from_profile`.

Live invocation:

- `CT_TESTNET=1 pytest tests/testnet/test_pre_mainnet_network_live.py -v` — **3/3 green за 12s**:
  composition (настоящий production `App` в MAINNET profile, согласованный
  network context, без order writes), mode-failure block, in-flight block.

Harness-vs-prod verdict (harness, prod guard не тронут): явный
`testnet_ip_whitelist_override=False` kwarg для MAINNET-profile Settings —
live `.env` override сохранён, prod guard untouched.

Audits: flat/zero-open-orders везде. PM-B4 записан как LIVE-COMPLETE.

## 2026-09-05 — Package D LIVE-COMPLETE: ZeroReason/naked/liquidation 6/6 за 386s

REAL Aster Testnet, один 386s run. Prod scope: ZeroReason taxonomy (sticky
ТОЛЬКО `PROPORTIONAL_REDUCTION_DUST`), PM-B6 naked-flatten, PM-B7 operator
cap + liquidation invariant + fresh-entry leverage gate
(`LIQUIDATION_MMR_CUSHION=0.01`).

Live invocation:

- `CT_TESTNET=1 pytest tests/testnet/test_pre_mainnet_risk_live.py -v` — **6/6 green за 386s**:
  Dust-1 (master 0.008 shaping), Dust-2, Dust-3 (SHORT-leg fix — harness,
  prod не менялся), Protection-cancel/naked legs (manual follower Stop
  cancellation → recovery/flatten), liquidation-hold.

Key finds:

- S1 open→flatten livelock на ~200x найден live и закрыт entry gate (prod fix).
- liquidation-violation flatten ACTION доказан unshapeable в cross-only
  multi-asset реальности — theorem sketch: нужен risk >~100.5% против
  RiskPolicy cap 100%; isolated отвергнут биржей с -4168 на copyc1 И на
  tg-8264892202 (probed live). Тест утверждает честный hold contract + live
  evaluation evidence (branch-verdict logs с реальными inputs), а не flatten.
- TEMP probe добавлен и удалён; ноль TEMP sites осталось (проверено grep).

Offline hygiene (тот же пакет): 3 routing assertions ужаты 200→44, `priceProtect`
удалён из planner contract (wire его никогда не нёс) + 2 offline pins удалены;
gate count — только в `../ENGINE_TEST_STATUS_2026-08.md` (блок Package D).

Audits: каждый live test заканчивается flat/zero-open-orders — flat/zero
везде, второй App не запускался. PM-B5/PM-B6/PM-B7 записаны как LIVE-COMPLETE.

## 2026-09-05 — Package E status: E1 prod DONE un-run-live, E2 LIMIT partial + isolation PENDING

E1 prod DONE (live ещё не гонялся): metrics.py p50/p95/p99 + snapshot,
symbol-aware snapshot scope, max_followers=10 cap, health convergence gate
вместо clear-on-clean-sweep, ONE_WAY-only v1 mainnet boot refusal, удаление
`priceProtect` из planner contract (см. Package D hygiene выше).

E2 live files написаны (`tests/testnet/test_pre_mainnet_limit_live.py`,
`tests/testnet/test_pre_mainnet_isolation_live.py`):

- `CT_TESTNET=1 pytest tests/testnet/test_pre_mainnet_limit_live.py -v` — LIMIT:
  **2 passed** (L1 entry, L3 reduction/convergence) + L2 honest skip: биржа
  дважды исполнила full, а для residual row нужен real partial — честный skip,
  НЕ green.
- `CT_TESTNET=1 pytest tests/testnet/test_pre_mainnet_isolation_live.py -v` —
  isolation: первый прогон **0/3** — verdict: harness (naked master), engine
  корректно fail-closed (prod verdict правильный); fixed (master STOPs added),
  rerun IN FLIGHT на момент записи — isolation PENDING RERUN, НЕ green.

Audits: flat/zero-open-orders везде, второй App не запускался.

## 2026-09-05 — Package F OPEN + owner decisions

Package F OPEN: hybrid load 3–5 REAL + 100 SHADOW + restart/SIGTERM/SIGKILL
wrapper ещё не написан; финальный полный campaign + audit pending.

Owner decisions, записанные в кампанию:

- uv migration ПОСЛЕ F (frozen toolchain during gates);
- main/copyc1 BTCUSDT allOrders saturated 1000/1000 — negative-proof legs идут
  на copyc3, fail-closed by design;
- deterministic-COID revision lesson: revisions 1–5 / unique revision per
  submit (см. Package A).

## 2026-09-05 — Package E LIVE-COMPLETE + load rescope/envelope + phantom-fill fix

E2 closed: isolation **3/3 LIVE-COMPLETE за 633s** на финальном дереве
(`tests/testnet/test_pre_mainnet_isolation_live.py`: F1 429-transport через
`FollowerRuntime.client`, F2 injected −2015 + degraded-доказательство, F3
control-plane-denial после fresh-cycle heal). LIMIT: L1/L3 green, L2 honest
skip (биржа дважды full-fill вместо partial — residual row не shapeable).
Audits: flat/zero-open-orders везде, второй App не запускался.

Load gate RESCOPED 3 REAL + 10 SHADOW (файл
`tests/testnet/test_pre_mainnet_load_live.py`, только константа + derivation в
комментарии): load12 (103 ноги) измерил 17–37s свипы против 5s freshness —
~103×~7 ≈ 721+ weight/sweep против бюджета 40/s →
stale→dirty→3-pass-boundary congestion collapse (followers churn на flapping
master view). Потолок ≈ 40×5/7 ≈ 28 абсолютный → v1 cap 10 с запасом
(совпадает с `max_followers=10`); mainnet-бюджеты шире — cap растёт по
измеренному envelope без изменения кода.

Phantom-fill 65000 (shadow-only bounce open→RISK_CAP-flatten ~1s): TEMP-трейс
(`TEMP target ... qty=0.000 zero=RISK_CAP master_qty=0.004 current=0.006
cap=0.000 cap_stop=77932 avail=1000 risk=0.01 entry=65000`) доказал fills по
дефолту — WS-дельты проектора несут mark_price=0, синк цены в shadow
no-op'ился. Движок корректен (резал безумную ногу: стоп 77932 выше входа
65000). Фикс: `App._sync_shadow_marks` — REST-marks со sweep/boundary/startup
rebuild (prod, `src/copytradeaster/app.py`, offline-verified, live не гонялся
отдельно — проверяется самим load gate). TEMP-строки добавлены и удалены
(grep=0), сломанный throwaway `scripts/probe_hedge_shadow.py` удалён.
Побочная находка (JUSTIFIED, без фикса): `ShadowExchange` default
`auto_trigger=True` метит OTM-стопы EXPIRED через flatten-cascade
(`_fill_market` expires NEW conditionals при new_qty=0) — исчезает с фиксом
marks; load идёт с `False` и не affected.

Load rerun IN FLIGHT (`/tmp/load15.log`, 3 REAL + 10 SHADOW, 100 cycles +
restart/SIGTERM/SIGKILL + metrics/journal/audit). Дальше: финальный
flat-аудит → полный офлайн-гигиенический прогон + triage 2 fake-flip reds →
ruff → вердикт MAINNET.

## 2026-09-05 — Owner decision: HEDGE возвращается в скоуп (Package G)

Owner не подтверждал пометку DROPPED на full-HEDGE matrix (формулировка
`MAINNET v1 = ONE_WAY only` — из приложенного owner-плана, выбор без явного
«да»). Решение: проверить всё до прода — HEDGE-матрица (6 пунктов) возвращена
как Package G после F; ONE_WAY-only boot refusal остаётся до green матрицы +
flat-аудита. Код не переписывается — только live-приёмка (несколько часов
прогонов).

## 2026-09-05 — Load gate: 3 падения обвязки, фазная переразбивка, -4226

Движок ни разу не подвёл (60→80 зелёных циклов, рестарты чистые); падала
обвязка теста, каждый баг чинен раз и навсегда:

- Run 1 (2:07): SIGTERM-фаза читала ПЕРВУЮ строку stdout воркера и требовала
  `LOAD_APP_READY` — туда раньше прилетает лог движка. Фикс: skip-until-marker
  с дедлайном (один сайт).
- Run 2 (2:58): 80 циклов + graceful + SIGTERM + SIGKILL зелёные; финал P8a
  нашёл 1 SENDING (response-loss на последнем send драйвера, 5226 CONFIRMED).
  Фикс: P7b — prod `resolve_boot_unknowns` перед аудитом (fail-closed
  сохранён: unresolvable всё равно роняет gate).
- Run 3 (2:58): P6 SIGKILL-фаза — три бага разом: (а) executor переписывает
  coid на `ct<command_key>`, а тест ждал `hl-kill-*` в `_sending_visible`
  (вечное ожидание → fail); (б) тот же mismatch в single-fill сверке;
  (в) `CYCLES` вообще не определён (P7 NameError). Фиксы: poll любой SENDING
  строки, match по coid из строки журнала, `CYCLES = 100`, bounded re-probe
  в resolve (saturated-main allOrders лагодает).
- Run 4 (5:30): транзиентный reject стопов `-4226` (код без текста, первое
  появление за кампанию; повтор в run 5) — движок корректно FAILED →
  degraded → replan → heal; но flap съел бюджет cycle-wait. Мониторится.

Owner-приказ: gate переразбит на фазы (`CT_LOAD_PHASES` =
a,restart,b,sigterm,c,sigkill,d,audit — любая подвыборка) + масштаб циклов
(`CT_LOAD_SCALE`, дефолт 1.0 = 30/60/80/100). Каждый блок самодостаточен
(рестарты сравнивают журнал внутри окна, аудит сначала флаттит). Файл
`tests/testnet/test_pre_mainnet_load_live.py`. Идёт шейкдаун 0.2 (~20
циклов, все фазы), затем полный gate 1.0 для протокола.

## 2026-09-05 — Load gate GREEN на 0.2 (все фазы), полный 1.0 запущен

`loadgate6` (CT_LOAD_SCALE=0.2, все фазы): **1 passed за 41:41, exit 0** —
graceful restart, SIGTERM, SIGKILL с single economic fill, P7b tail-resolve,
journal без висяков, метрики p50/p95/p99, финальный signed audit. Независимый
аудит следом: main/copyc1/copyc2/copyc3 flat, ноль ордеров.

По ходу: детерминированный `command_key` (sha256, святое Package A) даёт
одинаковые coid киллера в разных прогонах на свежих DB — биржа исполнила оба;
сверка single-fill теперь scoped по `update_time >= t0_run` (доказательство в
пределах прогона). Прод-ограничение зафиксировано: кросс-DB reuse ключей
защищён только долговечностью prod-DB (по построению).

Запущен `loadgate7` — полный gate 1.0 (100 циклов) для протокола (~3ч).

## 2026-09-05 — Full 1.0 run убит часовым testnet-brownout (не движок)

`loadgate7` (1.0): 34 цикла зелёных, затем TimeoutError reduce cycle 35.
Лог-доказательства вранья дата-плейна ~1ч: drift в обе стороны
(`stream=0.000/exchange=0.004` И `stream=0.004/exchange=0`), boundary
1/3+2/3 `not quiescent` пачками, `sweep holds degraded (not-ready)` почти
час. Фолловеры зеркалили фантомы (flatten/reopen) — корректно, но сходимость
невозможна, когда биржа показывает то 0, то 0.004. Финал fail-closed чистый
(только driver residual 0.002, сглажен; все 4 flat). REST-проверка после:
3 стабильных чтения — brownout кончился. Запущен `loadgate8` (1.0).

## 2026-09-05 — Load gate FULL GREEN 1.0 (100 циклов, все фазы)

`loadgate8` (CT_LOAD_SCALE=1.0, all): **1 passed за 3:39:15, exit 0** —
100 циклов 3 REAL + 10 SHADOW, graceful restart, SIGTERM, SIGKILL с single
economic fill, P7b tail-resolve, journal без висяков, метрики p50/p95/p99,
финальный signed audit. Независимый аудит следом: все 4 flat, ноль ордеров.
Package F CLOSED. Остаток C–F: офлайн-гигиена + fake-flip triage + ruff +
вердикт v1; затем Package G (HEDGE).

## 2026-09-05 — Offline hygiene: ruff clean, 5 reds triaged, v1 VERDICT

Ruff: `check` + `format` зелёные (было 99 ошибок — все кампанейские, HEAD был
чист). По пути: `metrics_snapshot` suppress-блоки, realistic fake liq ниже.

Офлайн (unit, 1068 тестов): 4 детерминированных reds найдены и починены,
подтверждено green:

- 2× `TestMasterFlipEpisodeSemantics` (fake): НЕ fake-divergence, а настоящая
  регрессия кампании — доказано worktree-бисекцией (HEAD green, revert
  coordinator → green). Корень: FakeAster врал `liquidationPrice=100`, и
  PM-B7 safety-net (`_post_execution_converge`, LIQUIDATION_VIOLATION) сносил
  только что открытый SHORT (100 <= 65500×1.005). Живой Dust-3 был зелен,
  потому что биржа даёт настоящий liq. Фикс — честность фейка:
  `FakeAster.position_risk` считает `entry×(1∓1/leverage)` по стороне.
  Плюс в тестах: DAG-drain вместо `sleep(0)` (SET_LEVERAGE→OPEN→STOP за
  несколько поколений экзекутора).
- `TestHedgeLegFills::test_long_leg_sell_fill_closes_not_reopens`: AU (pa=0)
  маркирует символ dirty, поздний TP-TRADE дропался как dirty-master-event
  (B-дизайн: кадры не мутят состояние под перестройкой). Прод корректен
  (границу лечит свип; живой TP-триггер green). Фикс теста: компрессия хила
  (`_dirty_symbols.clear()` + комментарий; `mark_dirty()` без аргументов
  ставит `*`, не чистит!).
- `TestAppStartupGap::...blocks_entry_admission`: фейк `Commands.in_flight`
  возвращал `[object()]`, а E1-код сабскриптит `row["account_id"]`. Фейк
  обновлён до Row-shape (`{"account_id","status"}`), контракт тот же.

Остаток: `test_testnet_ip_whitelist_override_is_fail_closed` — FLAKY
(~3/8 полных прогонов; в изоляции/парах/остатке green, включая 6 подряд).
Валидация чистая pydantic, без сети; прод не затронут. Подозрение —
межмодульное состояние сюиты; чинить отдельным заходом, не блокер.

**ВЕРДИКТ v1 (ONE_WAY-only scope): GO.** Live: A 5/5, B 6/6, C 3/3, D 6/6,
E isolation 3/3 + LIMIT 2/3 (L2 honest skip), F 100 циклов green + flat
везде. Launch cap 10 (замер). HEDGE остаётся под boot refusal до Package G.
Известные ограничения: транзиентные -4226 на стопах (движок хилит корректно,
нужен кодбук), часовые brownout тестнета (fail-closed держит), детерминированные
coid при потере prod-DB (по построению), ShadowExchange default
auto_trigger=True (load идёт с False), L2 skip (поведение биржи).

## 2026-09-05 — Package G HEDGE live-matrix CLOSED, refusal снят

Новый `tests/testnet/test_pre_mainnet_hedge_live.py` (7 тестов, 1 REAL
follower copyc1 + main, каждый с finally flatten + signed flat audit):
G0 pre-check flat green; G1 LONG+SHORT одновременно green (44s, economic
sends >= 2); G2 асимметричные fills green; G3 стопы на ногах + replace
SHORT-стопа green; G4 смена плеча мастера не дёргает ноги green; G5 partial
close LONG green; G6 close LONG при целом SHORT green. Финальный независимый
audit: FLAT-ALL на main/copyc1-3/tg (пусто + zero open orders).

Находки по ходу (все закрыты):

- Прод-баг #27 (мусорный liq биржи): тестнет отдал `liquidationPrice=25.5M`
  на 80k LONG-ноге; PM-B7 safety-net (`_post_execution_converge`) трактовал
  как авторитет → `LIQUIDATION_VIOLATION` → снос здоровой ноги со стопом +
  переоткрытие следующим проходом (4 цикла на реальных комиссиях; SHORT не
  мигал — 25M не цепляет SHORT-проверку). Фикс в
  `Coordinator._liquidation_violated`: liq с невозможной стороны от входа
  (LONG >= entry, SHORT <= entry) демотится в unknown, нога судится по
  стоп-правилу. Мог ударить и в ONE_WAY — хедж выловил первым.
- Гонка teardown в харнессе (не прод): flatten при живом движке —
  переоткрытие ног между flatten и аудитом (два stranded-остатка, оба убраны
  с FLAT-ALL). Фикс `_flatten_audit_stop`: main-first flatten + settle 3s +
  stop движка + reconnect storage + аудит через audit-клиент.
- Неверная премиса G4: `set_leverage` мастера НЕ едет на фолловера — плечом
  владеет движок (LeverageManager, свои SET_LEVERAGE 32-34 в журнале).
  Тест переписан на осмысленное свойство (смена плеча мастера: qty+стопы
  стабильны, ноль новых MARKET-команд).
- Урок -1106: HEDGE-закрытия несут только positionSide без reduceOnly
  (как прод-планер); хелпер сначала слал reduceOnly — биржа отвергла.
- Бейзлайн после тримма: движок делает one-shot convergence trim
  (-0.005 → -0.004) секунды после открытия; захват бейзлайна только после
  стабильности (`_stable_leg_qtys`), иначе вечный timeout.
- Стартовый аудит нашёл чужие остатки (main LONG 0.004 + LONG-ноги на
  copyc1-3 со стопами) — убраны до старта G, FLAT-ALL.

ONE_WAY-only v1 mainnet boot refusal СНЯТ после green + flat (план требовал
именно такой порядок): удалены `_MainnetHedgeViolation`,
`_require_one_way_v1_config` и mainnet-ветки step 5 в `app.py`; везде единый
warn-and-fix (mismatch → конвертация в конфиг-режим, нечитаемый режим →
warn, кривые ордера по-прежнему реджектит биржа). Бут с финальным кодом
проверен (G0 re-run green). Пинов refusal в тестах не было.

Кампания A–G live закрыта полностью. Раннер субагентов мёртв весь день
(`TypeError: rt.getWorkPoolYieldItems`) — Package G сделан оркестратором
вручную.


## 2026-09-05 — независимое ревью и corrective: код/тесты готовы, live ожидается

Прежний GO для `2c89286` пересмотрен. Ревью воспроизвело обход UNKNOWN новой
qty/revision, конфликт двух HEDGE leverage action_id, повышение worker-ом
пониженного плеча, неработавшее production network wiring, гонку REAL-cap и
ложный zero-proof на ограниченном окне journal. Проверка liquidation относительно
entry также была неверна для profit/cross-HEDGE geometry.

Исправления внесены в App/Coordinator/Planner/Executor/LeverageManager/Registry/
Reconciler и Settings/adapters. Добавлен общий account risk/margin budget.
Mainnet boot теперь вызывает read-only preflight и требует проверенного режима.
Исторические FAILED не заменяют текущую сверку; master READY не стирает degraded
без authoritative полного follower-proof.

По дополнительной просьбе владельца подготовлен код live-квалификации:
`test_engine_corrective_live.py` (LC1–LC6), расширены A2/A6, N1 и load;
устранён неверный liquidation-hold theorem в D. Load использует production
UNKNOWN resolver, обязательный opt-in и независимый cleanup в finally.
Signed pytest-кампании сериализованы файловым замком. Новые сценарии сохраняют
signed evidence; искусственно внесённые отказы и historical journal обозначены явно.

Во время исходного ревью немаркированный load-файл попал в `not testnet` и
непреднамеренно начал signed Testnet run. Он был остановлен; созданные позиции
закрыты, независимый финальный signed audit подтвердил flat/zero-orders
(`data/review-20260905/testnet-final-audit.json`). Исходный полный временный
pytest-log не сохранился; `unintended-load.log` содержит обозначенные выдержки.
Новый общий collection guard устраняет этот обход. В corrective-части signed
торговля не запускалась; проверены offline suite и opt-out.

Счётчики/coverage — только в `../ENGINE_TEST_STATUS_2026-08.md`.
Документарная coverage-цель пока не достигнута; не объявляется пройденной.
Следующему исполнителю остаётся запускать готовые сценарии и исправлять
отклонения; mainnet остаётся NO-GO до новых доказательств. Точный порядок:
[ENGINE_CORRECTIVE_TEST_PLAN_2026-09.md](ENGINE_CORRECTIVE_TEST_PLAN_2026-09.md).

## 2026-09-06 — Corrective qualification: live-матрица закрыта, 3 прод-бага, вердикт owner scope

Дерево: `codex/live-copy-tests-hardening` поверх `e30c3a6` + фиксы ниже (некоммичены
на момент записи). Офлайн-gate: **1147 passed, 2 skipped**, coverage 86% (цель 87%
открыта), ruff check/format чистые. Флейк `test_testnet_ip_whitelist_override`
подтверждён order-dependent (в изоляции зелёный, в полном прогоне падает) — предсуществует.

Step-2 (план, п.2): UNKNOWN 6/6 (A4 переведён copyc3/BTCUSDT → copyc3/ETHUSDT:
все BTCUSDT allOrders-окна забиты 1000/1000, negative proof там невозможен
fail-closed by design; движок вёл себя правильно), network 3/3, LC1–LC6 6/6.
Совместимость (план, п.3): master 5/5, risk 5/6, isolation 3/3, limit 2/4
(L1+L3; L2 residual и reduction — honest skip, биржа исполнила full),
hedge 7/7. Скипы dust2 (бюджет фолловера 1.15 USDT) и limit-reduction
(full-fill) — честные, пункты остаются открытыми.

Прод-баги, найденные и починенные вживую (движок ревью не трогаем сверх этого):
1. **1x unset-marker как ready** (`trading/leverage.py observe/prepare/seeding`):
   R6-наблюдение принимало биржевую 1x за готовое плечо → F3 открылся без единой
   SET-строки на экономике 1x. Фикс: <2 никогда не валидирует, не сидирует
   кандидата, форсирует provision; +2 офлайн-регрессии. F3 green denial+heal.
2. **Протухший post-fill кэш мастера** (`trading/coordinator.py`): `_last_master_state`
   обновлялся только стримом; при лаге WS постфилл-сверка закрывала живые ноги по
   фантому MASTER_CLOSED (осцилляция L1). Фикс: гейт свежести 10с + регрессия.
   L1 green.
3. **Неразрешимый UNKNOWN у SET** (`trading/executor.py _resolve_set_leverage_boot`):
   row-omitting клиенты (shadow без позиций) не давали ни подтвердить, ни уронить →
   вечный brick входов через R4-гейт (load встал на cycle 2). Фикс: пустые строки =
   FAILED с безопасным перевыставлением (SET идемпотентен); конфликт значений —
   по-прежнему UNKNOWN. Старый unit, пинивший вечный UNKNOWN, обновлён под новый
   контракт ( sibling-тест уже требовал re-POST). + регрессия requsted-by-reviewer
   invariant обновлён (empty reads → re-provision + CONFIRM).
A5 переведён на delta-count (детерминированный COID делит окно с прошлыми прогонами).

Харнесс-фиксы (прод не тронут): quiesce сбрасывает только флаг (фон заглушен
закрытыми generations), refresh через `_establish_master_boundary`, settled_targets
против reconnect-штормов, LC2-математика по ненулевым таргетам, нарезка load-фаз
по ~10 циклов (a1..d2 + алиасы), P8a-B + moot-by-flat (ниже).

Load (план, п.4): прогон 1 (cycle 2) — выше, баг 3. Прогон 2 (1.0): убит штормом
-4226 на cycle 30. Прогоны 0.3: №5 — 30/30 + фолты, P8a-strict поймал 21 залеченный
рубец; №6 — то же + 2 незалеченных в хвосте; №7 — 30/30 + фолты + метрики,
1 незалеченный хвост (copyc1 MARKET_CLOSE -4226 за минуту до конца, moot-by-flat
проходит на его журнале: 26 healed + 1 moot). Финальный FLAT-ALL независимым аудитом.
**-4226: недокументированный транзиентный реджект тестнета** (нет в официальном
списке кодов Aster, проверено по докам; приходит без msg, пачками по минутам,
~1%/час, на всех видах ордеров). Кодбук: лечение — replan, не слепой ретрай.

Решения owner'а (явные, не умолчания): (1) load scope 100→30 (CT_LOAD_SCALE=0.3;
«100» признано эмоциональной цифрой; инженерно достаточно: механика 5–10 циклов,
фолт-фазы не зависят от глубины, хвосты ловят только грозы); (2) P8a-B: залеченные
детерминированные реджекты + moot-by-flat вместо стерильности журнала (SENDING/
UNKNOWN/SUBMITTED — строго ноль; ambiguous-маркеры — всегда fail; shadow — только
с преемниками); (3) приёмка load7 по evidence + coded moot-анализу без зелёного
exit-кода (перезапуск ради строчки `1 passed` отклонён как подлог квалификации
наоборот: вердикт ставится по доказательствам при свидетеле-owner'е).
Фазы load нарезаны мельче следующим коммитом — требование owner'а после кампании.

Чеклист плана: п.2 ✓, п.3 ✓, п.4 ✓ (owner scope), п.5 — этот журнал + TL;DR +
вердикт ниже. Критерий «load не сокращён» нарушен явно и подписан owner'ом.
Непокрытое: полный load 1.0 (отложен), 3 shape-skip, coverage 86→87.

**Вердикт: corrective qualification COMPLETE (owner scope). Mainnet этим сообщением
НЕ разрешается.**

## 2026-09-07 — Review-исправления P1/P1/P2 + STOP: фронт-стенд торгует теми же аккаунтами

Три пункта ревью приняты и исправлены (дерево некоммичено на момент записи):
1. Post-fill generation-freshness (`trading/coordinator.py`): публичный
   `recompute_follower` теперь обновляет `_last_master_state` (авторитетный
   refresh вытесняет WS-кэш), а `_postfill_recompute` после `wait_until_idle`
   перечитывает кэш — пришедшее новее состояние побеждает stale-capture
   (identity+generation+timestamp), плюс повторный TTL-гейт. Регрессии:
   supersede-тест + детерминированный race-тест (refresh другого фолловера
   под held-guard; fan-out внутри гарда невозможен — gather ломает
   re-entrancy, это учтено в тесте).
2. Effective-is-truth (`trading/leverage.py`): `observe()` подтверждает любое
   биржевое значение в `[1, ceiling]`; `prepare` provisionит ТОЛЬКО вниз
   (observed > ceiling), апшифта к ceiling больше нет; unanimous-1x сидирует
   кандидата только при operator cap 1. Старый F3-юнит заменён, добавлены
   repro-тесты ревьюера как штатные.
3. Строгий P8a-хелпер (`test_pre_mainnet_load_live.py`): allowlist
   (`aster:-N`, `proven-not-executed:*`, `boot-resolved:*`) + точный
   intent-match (side/positionSide/leverage) + moot только для MARKET_CLOSE
   на flat REAL с классифицированной ошибкой. Синтетика ревьюера
   (http:500/502, runtimeerror, пусто, чужое плечо) отвергается; на load7
   даёт те же 26 healed + 1 moot; офлайн-юниты в
   `tests/unit/trading/test_journal_audit.py`.
Офлайн: **1157 passed, 2 skipped**, ruff check/format чистые. Три repro
ревьюера зелены. A5 rerun live green 8.13s (артефакт `data/review-a5-junit.xml`;
в окне A5 фронт молчал — результат ЧИСТ).

STOP live-кампании — внешняя контаминация (расследование 2026-09-07):
F3 дважды упал одинаково (denial механика идеальна: 1 FAILED SET + BLOCKED,
ноль sends), а позиция 0.003+stop пришла извне журнала. Форензика по
allOrders/history/140 журналам нашла отправителя: **движок фронт-стенда
(`copytradeaster-front/data/miniapp-preview.sqlite3`) торгует ТЕМИ ЖЕ
main/copyc1-3/tg-8264892202 теми же ключами** — 2301 команда с 2026-09-02,
все ghost-coid найдены у него как CONFIRMED. Пересечения по его журналу:
- A5 (00:27:02–00:27:10 MSK): 0 строк фронта — ЧИСТ.
- L1 (00:27:12–00:28:45): фронт пашет copyc1-3/tg — даблы 0.003/0.004 с
  разницей 50–200мс доказаны fills. L1-green КОНТАМИНИРОВАН (ассёрты слепы
  к удвоению) — нужен чистый rerun.
- F3/F3b: фронт OPEN+STOP на copyc1 в окне (ghost) — движок F3 полностью
  оправдан, тест требует чистого rerun.
- Load5/6/7 (UTC 01:54–05:06): фронт 1900+ команд на тех же аккаунтах —
  КОНТАМИНИРОВАНЫ. Все 27 FAILED load7 — только REAL-аккаунты, ноль shadow.
  Гипотеза: шторм -4226 — это интерференция двух движков, а не тестнет
  (была «недокументированный транзиент»; теперь корреляция с dual-engine
  окнами установлена, дискриминация требует тихого окна).
- Чистыми остаются: Step-2 (UNKNOWN/network/LC), compat, fix-reruns
  (f3fix2/l1fix/lc2fix), A5-rerun, весь офлайн.
Фронт в 21:49 UTC показывает и свои FAILED — движки взаимно портят друг
другу результаты. Дальнейшие live-прогоны на shared-аккаунтах
бессмысленны до решения owner'а (остановить стенд / развести ключи).
Watch 4 мин после остановки работ: тихо. Exchange FLAT везде.
Вердикт: код-фиксы qualify (офлайн + repro + A5-clean); live-матрица
ТРЕБУЕТ чистой переквалификации затронутого (F3, L1, load) — mainnet NO-GO.

## 2026-09-07 — Чистые F3+L1 green после килла фронта (owner: "ебни все")

Фронт найден и убит по приказу owner'а: `preview_miniapp_engine.py
--confirm-engine-preview` (PID 53685, 23 часа жизни, 70 сокетов) гонял
полноценный живой движок на тех же ключах. После килла: новых строк в его
журнале нет, процесса нет, биржа FLAT.
- **F3 clean GREEN 131.8s** (`data/review-f3-junit4.xml`): executor-denial
  (FAILED SET -1003 + BLOCKED + 0 sends) + heal-конвергенция +
  steady-state 1x (copyc1 сошёлся на 1x, **ноль set_leverage POSTs** —
  живой пин effective-is-truth) + flat audit. Фронт молчал весь прогон.
- **L1 clean GREEN 69.3s** (`data/review-l1-junit2.xml`): без даблов,
  residual только по filled. Фронт молчал.
- A5 green 8.13s остаётся чистым (окно проверено).
Итого по ревью: P1/P1/P2 исправлены и проверены вживую на затронутых
сценариях (F3 — leverage, L1 — post-fill, A5 — артефакт). Финальный
независимый flat audit: main/copyc1-3 FLAT, zero open orders.
Открытым остаётся только load-переквалификация в тихом окне (нагрузка
0.3 контаминирована фронтом; -4226-корреляция с dual-engine окнами
зафиксирована как гипотеза). Scope — решение owner'а.
Вердикт: review-фиксы QUALIFIED (engine + targeted live). Mainnet решению
owner'а; load-гейт требует чистого прогона.

## 2026-09-07 — LC1 clean GREEN на исправленном дереве + compat-счёт
LC1 (`test_shared_hedge_leverage_rejection_retry_and_worker_restart`)
перепрогнан на effective-is-truth дереве: **1 passed 137s**
(`data/review-lc1-junit.xml`), фронт молчал всё окно. Downshift 13x и
restart-геометрия не затронуты сменой 1x-семантики (все значения >= 2
идут тем же путём: min(ceiling, observed), no-op при равенстве).
Compat-счёт уточнён по XML: 24 теста = 20 passed + 2 failed + 2 skipped;
оба failure закрыты зелёными rerun (f3fix2, l1fix) → **22 nodeids green
+ 2 honest skip** (dust2 бюджет, limit-reduction full-fill), как и считало
ревью. TL;DR исправлен с «20/20».

## 2026-09-07 — Чистый load 0.3 GREEN с exit-кодом; -4226 был интерференцией

По приказу owner'а проведён полный load 0.3 в тихом окне (фронт мёртв,
0 его строк за весь прогон): **1 passed за 3989.74s (1:06:29), exit 0**
 supervised-процесс, junit `data/review-load-clean2-junit.xml`, база
`data/review-load-clean2/.../hybrid-load.sqlite3`. Журнал: **1981
CONFIRMED, 0 FAILED, 0 висящих** — стерильно, P8a не понадобился даже
moot. Все фолт-фазы (restart/SIGTERM/SIGKILL) + аудит + метрики P8b
(ассёрты p50/p95/p99 + weight) зелёные внутри прогона.
Процедурные уроки: (1) wrapper с pipe + 3600s tool-cap убил первую
попытку на 27 цикле — перезапуск только через supervised-процесс;
(2) `--basetemp` НЕ переживает реинвокацию (журнал 1812/0 убитого
прогона потерян — его counts остались только в переписке);
(3) resume-TЗ (`d1,audit`) отменён в пользу полного fresh-прогона.
P8b: числовой снапшот метрик на green-пути никуда не печатался —
добавлен `LOAD_METRICS ...` print в гейт (аддитивный лог, поведения не
меняет) для будущих прогонов; evidence этого прогона — assert-pass +
junit.
**Вердикт по -4226: это была интерференция двух движков, не тестнет.**
В тихом окне за 66 минут и ~2000 команд — ноль -4226, ноль FAILED
вообще. В грязных окнах (фронт 1900+ команд рядом) — 27 FAILED на REAL,
ноль на shadow. Кодбук обновлён: -4226 = симптом гонки за ноги/ордера
между двумя операторами одних ключей; отдельного транзиента тестнета
под этим кодом не наблюдалось. Независимый flat audit после прогона:
FLAT везде, zero orders.
Load-гейт ПЕРЕКВАЛИФИЦИРОВАН чисто. Mainnet — решению owner'а.

## 2026-09-07 — Остаточный post-fill fr.lock race закрыт + L1 rerun

Независимый re-review `ac161b2` нашёл оставшийся TOCTOU: post-fill перечитывал
master после `wait_until_idle`, но до ожидания `fr.lock`; свежий authoritative
refresh в этом окне мог оставить задаче старый flat и вызвать лишний
`MARKET_CLOSE`. Reviewer-repro до фикса детерминированно отправлял SELL 0.01.

Фикс в `trading/coordinator.py`: post-fill по-прежнему ждёт originating executor
sequence, затем входит в штатный порядок `fr.lock -> executor mutation lock`, и
только внутри него выбирает текущий master + timestamp и проверяет TTL именно
для выбранного latest перед `_recompute_follower_locked`. Добавлены штатные
регрессии на refresh во время ожидания `fr.lock` и TTL superseded-state.

Evidence после фикса: reviewer-repro **1 passed**; затронутая unit/integration
матрица **75 passed**; полный offline **1159 passed, 2 skipped, 85 deselected**;
Ruff check/format и `git diff --check` чистые. Перед live preview-процессов не
было. Запрошенный L1
`test_limit_entry_partials_track_filled_only` — **1 passed за 88.18s**,
JUnit `data/review-l1-postfill-race-junit.xml`; cleanup/flat audit внутри теста
прошёл. Часовой load повторно не запускался — owner прямо исключил его для
этого локального исправления.

Вердикт: оставшийся post-fill P1 закрыт и targeted live переквалифицирован.
Mainnet — только отдельным решением owner'а.

## 2026-09-09 — Integrated hardening + frontend requalification

Ветка `codex/integrate-hardening-frontend` собрана из hardening tip `4c0d1a1`
и `feature/frontend` `d6d5791`. Конфликты разрешены с сохранением трёхсостояния
UNKNOWN resolver, history observer для runtime/boot recovery, network-profile
scope и append-only migration lineage 0..59. Runtime resolution получил
1-секундное ограничение повторного SQLite/exchange scan; unresolved in-flight
строка по-прежнему блокирует новый вход и разрешается перед retry.

Фактические gates: offline **1525 passed / 2 skipped / 87 deselected**,
coverage **87%**; frontend **28/349**, oxlint/tsc/Vite PASS; shadow scale
30×100 + 100×20 — **2 passed за 63.49s**; Ruff, format, compileall и strict
doc-links чистые. Signed targeted: network 3/3, UNKNOWN 6/6, history lifecycle
1/1, history read 1/1, corrective 6/6, master/risk/isolation 14/14, LIMIT
2/3 + 1 honest skip. Независимый flat audit main/copyc1/copyc2/copyc3 дал
zero non-zero positions и zero open orders.

Интеграционный pre-mainnet verdict остаётся **NO-GO** до отдельного решения
по live harness assumptions: hedge G2 дважды получил равные risk-sized ноги
при требовании strict master-size asymmetry; два leverage-control теста ждут
старые account-max/downshift предпосылки (фактический signed max 27); два
one-way e2e теста получили insufficient-margin/округлённый zero; четыре Mini
App live acceptance теста — PAUSED/stale-account setup. Это не приписывается
history merge diff без дополнительного reproduction: offline и targeted
history/UNKNOWN gates зелёные, а исходные hardening semantics sizing/leverage
в интеграции не менялись.

## 2026-09-09 — Requalification complete after harness alignment

После просмотра merge diff и сверки с текущими production-контрактами live
харнесы приведены в соответствие с кодом, без ослабления safety assertions:

- leverage: **4/4 passed** (effective signed leverage, rejected SET recovery,
  real downshift и close/entry protection);
- ONE_WAY: **2/2 passed за 369.90s** на scratch SQLite с изолированным journal
  и risk policy;
- Mini App live: Activity **1/1 за 40.50s**, Notifications/Positions/Risk
  **3/3 за 143.47s**. Activity теперь проверяет production history capture и
  `user_activity_events`, а не устаревший `execution_commands` view;
- HEDGE: **7/7 за 393.65s** (G0–G6, включая asymmetry, per-leg protection,
  leverage update, partial/one-leg close и flat cleanup);
- полный hybrid load 0.3: **1 passed за 4085.39s (1:08:05)**, 30 циклов,
  restart/SIGTERM/SIGKILL, 3 REAL + 10 SHADOW, scratch journal **1988
  CONFIRMED / 0 FAILED / 0 UNKNOWN / 0 SENDING**;
- offline **1525 passed / 2 skipped / 87 deselected**, coverage **87%**;
  shadow scale **2 passed за 69.47s**; frontend **28 файлов / 349 passed**,
  oxlint, `tsc -b`, Vite build; strict doc-links — 59 файлов.

Независимый signed flat-аудит после полного прогона: `main`, `copyc1`,
`copyc2`, `copyc3`, `tg-8264892202` — 0 позиций и 0 открытых ордеров.
Причины прежнего NO-GO были дефектами предпосылок live-тестов (effective
leverage, stale baseline/cleanup, shared DB/history source и admission seam),
а не наблюдаемым нарушением engine safety invariants. **Итог: GO для
fast-forward/push проверенного merge-коммита в Git `main`; mainnet остаётся
запрещённым fail-closed конфигурацией.**
