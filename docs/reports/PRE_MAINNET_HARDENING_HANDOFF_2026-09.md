# Pre-Mainnet Hardening — полная задача следующей сессии

Дата фиксации: 2026-09-04.

## ОБЯЗАТЕЛЬНЫЙ КОНТРАКТ

ВСЯ PRE-PRODUCTION QUALIFICATION ПРОХОДИТ НА РЕАЛЬНОМ ASTER TESTNET С
РЕАЛЬНЫМИ TESTNET-КОШЕЛЬКАМИ. UNIT, FAKEASTER, MOCKTRANSPORT И
SHADOW-ONLY ПРОГОНЫ НЕ ЗАСЧИТЫВАЮТСЯ КАК ACCEPTANCE И НЕ ЗАКРЫВАЮТ
PACKAGE.

Offline-тесты остаются supplemental regression-защитой. Для
response-loss, 429 и network/query failure допустим fault injector вокруг
реального `AsterClient`, но ордера, REST/WS, positions и exchange state
должны оставаться реальными Testnet. `ShadowExchange` разрешён только
в исходном hybrid-load gate вместе с 3–5 REAL followers.

MAINNET REST/WS и любые Mainnet order writes до полного закрытия
Packages A–F запрещены.

## Репозиторий и Git

- Репозиторий: `JDBACK/copytradeaster`.
- Абсолютный локальный путь: `/Users/jd_ebash/copytradeaster`.
- Рабочая ветка: `codex/live-copy-tests-hardening`.
- Базовый HEAD перед этим documentation-only handoff: `cf537df`.
- Коммит с кодом Package B: `32fe56d`.
- Начальный Package A/B checkpoint: `918bf46`.
- В начале сессии обязательно выполнить `git status --short`,
  `git branch --show-current` и `git rev-parse --verify HEAD`; сообщить
  фактический HEAD до новых изменений.

## Все обязательные документы и зачем они нужны

В новой сессии открывать файлы по обычным абсолютным путям;
никакие markdown-гиперссылки для навигации не требуются.

1. `/Users/jd_ebash/copytradeaster/AGENTS.md`
   - Главные engineering-правила репозитория.
   - Обязывает искать все call sites, читать тесты/типы/модели,
     воспроизводить failure до fix и проверять edge/error paths.
2. `/Users/jd_ebash/.codex/attachments/4c783ff4-2f1d-4bba-a422-3a40fbb07370/pasted-text-1.txt`
   - Исходный owner-provided Pre-Mainnet Hardening Plan.
   - Это первичный contract по PM-B1…PM-B8, TESTNET PROOF и Packages A…F.
3. `/Users/jd_ebash/copytradeaster/docs/plan_live_test.md`
   - Рабочая копия owner plan.
   - Содержит актуальные `[x]`, `[~]`, `[ ]` для каждого обязательного
     REAL Aster Testnet-сценария.
   - Чекбокс можно менять на `[x]` только после фактического live-
     прогона и финального exchange audit.
4. `/Users/jd_ebash/copytradeaster/docs/reports/PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`
   - Append-only журнал кампании.
   - После каждого live-сценария записывать точную команду,
     result, реальные аккаунты/путь движка, injected fault, число
     economic sends, journal verdict и final flat/order-free audit.
5. `/Users/jd_ebash/copytradeaster/docs/ENGINE_TEST_STATUS_2026-08.md`
   - Единственная точка истины для актуальных счётчиков offline,
     frontend и signed Testnet suite.
   - Счётчики не дублировать в других документах.
6. `/Users/jd_ebash/copytradeaster/docs/ARCHITECTURE.md`
   - Авторитетная архитектура: journal, projector, reconciliation, concurrency,
     boot/startup и recovery boundaries.
7. `/Users/jd_ebash/copytradeaster/docs/DEV_PLAN_SKELETON.md`
   - Верхнеуровневый roadmap и статус Packages A–F.
8. `/Users/jd_ebash/copytradeaster/docs/AGENTS.md`
   - Карта всей документации; показывает, что является living truth,
     а что историей.
9. `/Users/jd_ebash/copytradeaster/tests/AGENTS.md`
   - Карта offline/integration/replay/testnet suites, кошельков, guards и
     cleanup-протокола.
10. `/Users/jd_ebash/copytradeaster/scripts/AGENTS.md`
    - Карта operational live/hybrid/soak scripts и их safety gates.

## Конфигурация и секреты

- `/Users/jd_ebash/copytradeaster/.env`
  - Текущая Testnet-конфигурация. Не печатать секретные поля в
    логи или отчёт.
- `/Users/jd_ebash/copytradeaster/wallet.txt`
  - Git-ignored source реальных Testnet credentials для `main`, followers и
    Builder-сценариев. Не читать/не выводить private keys в terminal output.
- `/Users/jd_ebash/copytradeaster/data/copytrade.sqlite3`
  - Рабочая SQLite с зашифрованными credentials. Не запускать live-
    кампанию на ней напряму; создавать point-in-time copy через
    SQLite backup API и передавать её в `CT_DB_PATH`.
- `/Users/jd_ebash/copytradeaster/data/`
  - Git-ignored каталог для run DB, JSON reports и logs. Имена артефактов
    должны быть уникальными; не перезаписывать старые evidence.

## Production-файлы Package A, которые нужно перепроверять

- `/Users/jd_ebash/copytradeaster/src/copytradeaster/trading/executor.py`
  - `ResolutionOutcome`, `MarketRecoveryContext`, `_resolve_exchange_outcome`, runtime
    recovery, boot recovery, residual handling и Builder DIRECT boundary.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/aster/trading_api.py`
  - Реальные signed `place_order`, `query_order`, `all_orders`,
    `query_order_history` и `position_risk` endpoints.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/aster/models.py`
  - Typed Aster order/position DTO и `executedQty` semantics.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/storage/repositories.py`
  - Durable `execution_commands`, status transitions, recovery context и in-flight reads.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/storage/database.py`
  - Migration #33 для `execution_commands.recovery_context`.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/trading/builder_attribution.py`
  - Builder attribution verdict и граница `-5026`.

## Test-файлы Package A

- `/Users/jd_ebash/copytradeaster/tests/testnet/test_signed_scenarios.py`
  - Главный signed live harness Package A.
  - Уже содержит real response-lost/`allOrders`, real SIGKILL worker и
    real production App/WS reconnect сценарий.
  - Сюда либо в отдельный `tests/testnet/test_pre_mainnet_unknown_live.py`
    добавить недостающую live-matrix; при новом файле обновить
    `/Users/jd_ebash/copytradeaster/tests/AGENTS.md`.
- `/Users/jd_ebash/copytradeaster/tests/unit/trading/test_executor_journal.py`
  - Supplemental exhaustive offline matrix для open/close/partial/unavailable/proven-
    absent/restart. Использовать как список веток, но не как live evidence.
- `/Users/jd_ebash/copytradeaster/tests/unit/trading/test_builder_stale_corrective.py`
  - Supplemental Builder DIRECT fallback/UNKNOWN regression matrix.
- `/Users/jd_ebash/copytradeaster/tests/testnet/test_builder_stale_corrective_live.py`
  - Существующий real Testnet Builder reduction fallback; перепроверить,
    что он доказывает именно отсутствие UNKNOWN bypass.

## Фактический статус Package A

Код реализован и offline-green, но Package A НЕ CLOSED.

Уже пройдено на REAL Aster Testnet:

- MARKET_OPEN accepted, HTTP response намеренно отброшен;
- current-order lookup намеренно скрыт, order найден через real
  `allOrders`, economic send ровно один;
- отдельный Python worker убит настоящим `SIGKILL` после принятого
  MARKET, boot/WAL recovery не создал второй order;
- оба focused сценария завершились flat/zero-open-orders audit.

Обязательно осталось пройти/повторить на REAL Testnet:

1. MARKET_OPEN accepted + response lost — повторить в цельном Package A
   campaign.
2. MARKET_CLOSE accepted + response lost — новый сценарий.
3. Accepted mutation при недоступных current/history/position recovery reads:
   journal остаётся `UNKNOWN`, economic send ровно один.
4. Definitely-not-accepted first attempt: реальные current/history/position reads
   доказывают absence, после чего разрешён один реальный retry.
5. Partial execution/outcome recovery с residual-only continuation; не подменять
   чисто synthetic position за live evidence.
6. Restart для обоих `SENDING` и `UNKNOWN`; повторить SIGKILL в едином
   campaign.
7. Тот же набор для MARKET_CLOSE.
8. Builder DIRECT fallback не обходит UNKNOWN safety.

## Production-файлы Package B для последующей qualification

- `/Users/jd_ebash/copytradeaster/src/copytradeaster/app.py`
  - Master ingest envelope, queue overflow/backlog, dirty recovery, boundary passes,
    startup/reconnect admission и READY publication.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/master/projector.py`
  - Snapshot generation, Aster server-time cutoff, stale event rejection, durable episode
    sync и critical rebuild requests.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/aster/user_stream.py`
  - ListenKey lifecycle, concurrent receive, connect/disconnect callbacks и reconnect.
- `/Users/jd_ebash/copytradeaster/src/copytradeaster/trading/coordinator.py`
  - Process-local entry-admission gate: increases блокируются, reductions проходят.
- `/Users/jd_ebash/copytradeaster/tests/testnet/test_signed_scenarios.py`
  - Существующий production App/real WS/reconnect scenario.
- `/Users/jd_ebash/copytradeaster/tests/unit/master/test_projector_payloads.py`
  - Supplemental generation/cutoff/failure matrix, которую нужно перенести в
    live Testnet scenarios, а не засчитывать саму по себе.
- `/Users/jd_ebash/copytradeaster/tests/unit/test_coverage_gaps.py`
  - Supplemental startup/disconnect/in-flight/continuous-event matrix.

## Фактический статус Package B

Код реализован и offline-green, но Package B НЕ CLOSED.

Уже пройден один REAL Aster Testnet-сценарий:

- production `App` на backup SQLite;
- live WS-gated READY;
- real master MARKET + STOP;
- convergence четырёх REAL followers;
- forced `close_listen_key`, reconnect и authoritative resnapshot;
- follower quantity и один Stop на account не изменились/не удвоились;
- cleanup подтвердил flat followers и master;
- focused result: `1 passed in 107.07s`.

Обязательно осталось пройти на REAL Testnet:

1. Real WS TRADE удержан в queue во время real REST rebuild.
2. Queue overflow на frames, реально полученных из Aster WS.
3. Delayed stale TRADE из real WS не мутирует post-snapshot state.
4. Reconnect с fill, совершённым в gap.
5. Reconnect resnapshot временно падает; entry admission закрыт,
   после recovery final state совпадает с real REST.
6. Startup reconciliation искусственно замедлен; master реально
   меняется между initial snapshot и WS READY; REAL followers не открывают
   stale entry.
7. После каждой recovery-ветки projector и follower state совпадают
   с реальным `positionRisk`/`openOrders`.

## Что осталось после A и B

1. Package C / PM-B4: immutable `AsterNetworkProfile`, согласованные
   Testnet/Mainnet hosts/chains/signing/onboarding и Mainnet composition preflight без
   order writes; exchange-facing paths пройти на REAL Testnet.
2. Package D / PM-B5–PM-B7: explicit zero reason, naked-position flatten,
   operator leverage cap и liquidation-vs-stop invariant; Dust/Protection/HEDGE
   matrix пройти на REAL Testnet.
3. Package E / PM-B8 + health convergence: symbol-aware production follower snapshots,
   request-weight/latency metrics, truthful degraded и measured launch cap; acceptance —
   3–5 REAL Testnet followers + 100 SHADOW slots, не SHADOW-only.
4. Package F: без новых features повторить весь REAL Testnet campaign,
   restart/SIGTERM/SIGKILL, App E2E, multi-follower failure isolation, hybrid load
   и финальный REST/SQLite audit.

## Точная первая задача новой сессии

НАЧАТЬ ЗАНОВО С PACKAGE A. НЕ ПЕРЕХОДИТЬ К PACKAGE B ИЛИ C,
ПОКА ВСЯ PACKAGE A LIVE-МАТРИЦА НЕ ПРОЙДЕНА И НЕ ЗАПИСАНА.

1. Прочитать все абсолютные пути из раздела выше.
2. Сверить каждую ветку unit matrix с production Executor и real Aster DTO/API.
3. Добавить недостающие signed tests для Package A. Не писать test,
   который только синтетически меняет position и выдаёт это за live-доказательство.
4. Создать свежую backup-копию SQLite и запускать focused signed-
   тесты с `CT_TESTNET=1` и `CT_DB_PATH=<absolute run-db path>`.
5. Перед каждым сценарием доказать, что другой `run-bot`, App,
   soak или pytest не запущен.
6. В `finally` каждого теста отменить bot-owned/master orders, flatten
   master/followers и затем выполнить независимый signed audit.
7. После каждого green/red live-результа сразу обновлять:
   `/Users/jd_ebash/copytradeaster/docs/plan_live_test.md`,
   `/Users/jd_ebash/copytradeaster/docs/reports/PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`
   и при изменении счётчиков
   `/Users/jd_ebash/copytradeaster/docs/ENGINE_TEST_STATUS_2026-08.md`.
8. Package A можно пометить `CLOSED` только когда каждый его
   обязательный live-пунк зелёный, economic send count доказан и
   final exchange state flat/order-free.

## Шаблон safety-запуска

Ниже не готовая команда с именем будущего артефакта; новая
сессия должна выбрать свой уникальный absolute run DB path и не
перезаписывать старый evidence:

```text
cd /Users/jd_ebash/copytradeaster
git status --short
git branch --show-current
git rev-parse --verify HEAD
ps ax -o pid=,command= | rg "[c]opytradeaster|[h]ybrid_load_test|[t]estnet_soak|[p]ytest"
sqlite3 /Users/jd_ebash/copytradeaster/data/copytrade.sqlite3 ".backup '/Users/jd_ebash/copytradeaster/data/<UNIQUE-PM-A-RUN>.sqlite3'"
sqlite3 /Users/jd_ebash/copytradeaster/data/<UNIQUE-PM-A-RUN>.sqlite3 "PRAGMA integrity_check;"
CT_TESTNET=1 CT_DB_PATH=/Users/jd_ebash/copytradeaster/data/<UNIQUE-PM-A-RUN>.sqlite3 /Users/jd_ebash/copytradeaster/.venv/bin/pytest <EXACT-LIVE-TEST-NODES> -vv -s --tb=short
```

## Критерий передачи после Package A

- Все обязательные Package A scenarios прошли REAL Aster Testnet.
- Ни один `UNKNOWN` не привёл к новому COID без
  `PROVEN_NOT_EXECUTED`.
- Partial progress привёл только к residual continuation.
- Runtime и boot дают одинаковый resolution verdict.
- Builder DIRECT fallback не обошёл UNKNOWN policy.
- Journal не содержит необъяснённых terminal/in-flight rows.
- Независимый signed REST audit показывает flat positions и zero open
  orders для всех задействованных Testnet-аккаунтов.
- Документы и тестовые счётчики обновлены по факту.
