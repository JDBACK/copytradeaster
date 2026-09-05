# Pre-Mainnet Hardening Plan

Проект: `JDBACK/copytradeaster`
Исходный audit baseline: `3c38db34f42d7a61c334367edbc434e6fbb97c8a`
Рабочая ветка: `codex/live-copy-tests-hardening`; `918bf46` — исходный
WIP-checkpoint Package B, а текущий факт исполнения вести по журналу ниже.
Подробное текущее состояние:
`/Users/jd_ebash/copytradeaster/docs/reports/PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`.
Задача следующей сессии с полными путям и описаниям:
`/Users/jd_ebash/copytradeaster/docs/reports/PRE_MAINNET_HARDENING_HANDOFF_2026-09.md`.
Единая точка счётчиков тестов:
`/Users/jd_ebash/copytradeaster/docs/ENGINE_TEST_STATUS_2026-08.md`.

Статус на 2026-09-05: **Packages A–G LIVE-COMPLETE на REAL Aster Testnet.
Package F hybrid load 3 REAL + 10 SHADOW закрыт (1 passed за 3:39:15, loadgate8,
CT_LOAD_SCALE=1.0, 100 циклов, restart/SIGTERM/SIGKILL, flat audit); Package G
HEDGE 7/7 live закрыт (test_pre_mainnet_hedge_live.py, прод-баг #27 wrong-side
guard, ONE_WAY-only boot refusal снят из src/copytradeaster/app.py, FLAT-ALL audit).
Кампания A–G закрыта полностью. Вердикт v1 (ONE_WAY + HEDGE): GO. Финальное
решение по выходу в mainnet за owner'ом.**

Обозначения: `[x]` — сделано и подтверждено; `[~]` — частично/WIP; `[ ]` — осталось.

## ОБЯЗАТЕЛЬНО: ВСЯ PRE-PRODUCTION QUALIFICATION ИДЁТ НА РЕАЛЬНОМ ASTER TESTNET

Ни один package не получает финальный `CLOSED` только по unit,
FakeAster, MockTransport или offline integration. Каждый сценарий этого
плана, где важны REST/WS, signing, order, position, protection,
reconnect, restart, limiter или exchange response, обязан пройти на
реальном Aster Testnet с реальными testnet-кошельками.

Offline fakes остаются быстрыми детерминированными regression-
тестами, но не acceptance evidence. Для response-loss, 429 и network/query
failure допустим детерминированный fault injector вокруг
реального `AsterClient`: exchange mutation/read/state должны оставаться
реальными. `ShadowExchange` допустим только в load gate вместе
с обязательными 3–5 REAL followers, как задано исходным планом.

## Цель

Подготовить trading core `copytradeaster` к безопасному первому запуску на Aster Mainnet.

Основной принцип этапа:

**не расширять продукт и не перестраивать архитектуру, а закрыть конкретные safety gaps, которые могут привести к duplicate execution, неверному master state, stale execution или позиции без корректной защиты.**

Текущая архитектура сохраняется:

* modular monolith;
* state-based copytrade;
* WS как быстрый сигнал;
* REST как authoritative recovery;
* durable execution journal;
* per-follower serialization;
* reconciliation;
* SQLite;
* один процесс.

Микросервисы, Kafka, Redis, PostgreSQL и другие инфраструктурные переделки в этот этап не входят.

---

# BLOCKERS

## PM-B1 — UNKNOWN economic outcome / duplicate execution — LIVE-CLOSED

Статус: `[x]` implementation/offline gate готовы, полная UNKNOWN live-matrix
закрыта на реальном Testnet (HEAD `388f0e7`, run DB
`data/pm-a-live-20260904T234500Z.sqlite3`). Unified Package A campaign:
новый `tests/testnet/test_pre_mainnet_unknown_live.py` **5/5 green** (A1 CLOSE
response-lost через real `allOrders`; A2 all-reads-down UNKNOWN hold без retry;
A3 partial через real `positionRisk` delta residual-only; A4 proven-not-executed
exactly-one retry на copyc3; A5 SIGKILL-in-UNKNOWN boot CONFIRM без resend),
pre-existing response-lost PASS, SIGKILL-SENDING PASS, REAL-05 boot PASS на
copyc3 за 10.53s, исправленный `tests/testnet/test_builder_stale_corrective_live.py`
**green за 53.67s** (Package B admission gate из `32fe56d`, entry leg заменён
прямыми REAL seeded legs, reduction assertions intact).

Saturation note: main/copyc1 BTCUSDT `allOrders` saturated 1000/1000 — отсутствие
ордера там недоказуемо by design, resolver отвечает fail-closed UNRESOLVED;
все negative-proof legs (A4, REAL-05) идут на copyc3 (67 rows). Фикс REAL-05
тронул только `tests/testnet/test_signed_scenarios.py` (aid params + ретаргет
REAL-05 на copyc3), prod code не менялся.

Deterministic-COID lesson: `command_key_of` игнорирует planner coid; тесты с
одинаковой формой payload ДОЛЖНЫ использовать distinct `ExecutionAction`
revision (A-file uses 1–5).

### Проблема

Текущий runtime Executor при неизвестном результате `place_order` делает:

`place_order → query_order → retry`

Но `GET /fapi/v3/order` не является достаточным доказательством отсутствия уже исполненного MARKET order.

Для Aster:

* HTTP 503 означает `UNKNOWN`, а не failure;
* FILLED/CANCELED order может уже не находиться через current-order query;
* transport error самого recovery query сейчас также может привести к retry.

В результате возможен сценарий:

1. Aster принял MARKET order.
2. Order исполнился.
3. HTTP response потерян.
4. recovery query не находит order или сам падает.
5. Executor создаёт новый COID.
6. MARKET отправляется повторно.

Это потенциальный duplicate economic mutation.

### Что изменить

Ввести явную модель resolution outcome:

* `FOUND`;
* `PROVEN_NOT_EXECUTED`;
* `UNRESOLVED`.

`UNRESOLVED` никогда не должен разрешать новую economic mutation.

Для MARKET recovery использовать не только current order query, но authoritative exchange state:

* order history / `allOrders`, где это возможно;
* actual `positionRisk`;
* ожидаемое изменение позиции;
* исходную позицию до mutation;
* requested delta.

При partial execution retry допускается только для доказанного residual quantity.

Runtime recovery и boot recovery должны использовать одну safety policy.

### Обязательные тесты

* MARKET accepted, response lost → только один economic fill;
* MARKET accepted, response lost, recovery query unavailable → zero retry;
* MARKET partial fill + response lost → только residual;
* MARKET definitely not accepted → retry разрешён;
* restart в `SENDING/UNKNOWN`;
* тот же набор для MARKET_CLOSE;
* Builder DIRECT fallback не должен обходить UNKNOWN safety.

### Acceptance

Ни один новый clientOrderId не создаётся, пока исходный economic outcome остаётся ambiguous.

---

## PM-B2 — Master snapshot / WS queue recovery boundary — LIVE-COMPLETE

Статус: `[x]` implementation/offline acceptance готовы и вся signed Testnet
Snapshot/WS-matrix закрыта на реальном Testnet на финальном дереве (run DB
`data/pm-b-live-20260905T002000Z.sqlite3` для REAL-06): S1 rebuild-holds-queue
100s, S2 stale-cutoff 44s, W1 reconnect-gap 83s, W2 failed-resnapshot 68s —
все green; каждый live test заканчивается flat/zero-open-orders, второй App
не запускался. Harness fixes: settle gate (partial-fill race),
`_wait_stable` quiescence gates, HEDGE nonzero-key counting,
deterministic-COID revisions.

### Проблема

Сейчас `TRADE` events намеренно допускаются out-of-order.

Одновременно `rebuild_full()` заменяет state authoritative REST snapshot.

Нет generation/watermark, который отделяет:

* events уже учтённые REST snapshot;
* events, которые действительно произошли после snapshot.

Возможен сценарий:

1. TRADE произошёл.
2. WS TRADE лежит в ingest queue.
3. `rebuild_full()` получает REST state, уже содержащий fill.
4. Старый TRADE затем dequeue.
5. Projector снова применяет fill.
6. Master position оказывается завышена.

Связанная проблема: queue overflow просто удаляет oldest event и ждёт будущего reconciliation.

### Что изменить

Сделать REST rebuild настоящей authoritative boundary.

После rebuild:

* pre-snapshot queued mutations не должны повторно применяться;
* stale backlog должен отбрасываться или invalidated generation;
* queue overflow должен немедленно переводить master projection в `dirty`;
* dirty state требует authoritative resnapshot до новых risk-increasing follower actions.

Не нужен полноценный event sourcing.

Предпочтительная модель:

`dirty/generation + coalescing + authoritative final REST state`.

### Дополнительно закрыть этим же workstream

* failed reconnect resnapshot;
* WS reconnect после gap;
* backlog age;
* ingest queue drop;
* critical resnapshot;
* periodic rebuild;
* startup transition REST → WS.

### Acceptance

После любого gap/drop/reconnect/rebuild master state невозможно изменить старым pre-snapshot TRADE.
## PM-B3 — Startup stale-entry gate — LIVE-COMPLETE

Статус: `[x]` implementation/offline acceptance готовы и signed Testnet
Startup-matrix закрыта на реальном Testnet на финальном дереве: P1
startup-transient 45s green, REAL-06 App/WS/reconnect 134s green (run DB
`data/pm-b-live-20260905T002000Z.sqlite3`); REAL followers не открывали stale
entry; каждый live test заканчивается flat/zero-open-orders, второй App
не запускался.
Startup/reconnect admission открывается только после live WS, первого
authoritative snapshot, close-only follower sweep, второго финального snapshot
и entry reconcile без in-flight commands. Disconnect/dirty закрывают admission
синхронно; REST-only fallback остаётся close-only.

### Проблема

Startup сейчас делает:

1. master REST snapshot;
2. follower reconciliation;
3. запускает master WS;
4. после WS connect делает ещё один REST snapshot.

Если durable `allow_new_entries=true`, startup reconciliation способен открыть follower position до того, как master stream подключён и подтверждён свежим post-connect snapshot.

На 30–100 followers это окно может стать значительным.

### Что изменить

Во время boot:

* closes разрешены;
* reductions разрешены;
* protection разрешена;
* risk-increasing entry/increase запрещены.

Entry admission открывать только после:

1. successful authoritative master snapshot;
2. успешного WS connection либо явно выбранного REST fallback режима;
3. final fresh snapshot непосредственно перед entry admission;
4. startup follower reconciliation без unresolved safety state.

Если WS недоступен, REST polling mode может работать, но entry должен открываться только после свежего REST snapshot с ограниченной freshness.

### Acceptance

Из stale startup snapshot невозможно открыть новую follower exposure.

---

## PM-B4 — Mainnet NetworkProfile — LIVE-COMPLETE

Статус: `[x]` prod `AsterNetworkProfile` (testnet 714 / mainnet 1666,
mixed-combo fail-closed) + unified mainnet preflight + onboarding
`from_profile`; live 3/3 green за 12s на REAL Aster Testnet (composition,
mode-failure block, in-flight block), без order writes; каждый live test
заканчивается flat/zero-open-orders. Harness fix: явный
`testnet_ip_whitelist_override=False` kwarg для MAINNET-profile Settings
(live `.env` override сохранён, prod guard не тронут).

### Проблема

Проект знает mainnet trading chain `1666`, но runtime всё ещё собирается вокруг testnet assumptions.

Testnet-specific значения находятся не только в `App`, но и в onboarding / Builder lifecycle.

Простая замена `714 → 1666` недостаточна.

### Что изменить

Ввести единый immutable `AsterNetworkProfile`.

Например:

### TESTNET

* exact REST host;
* exact WS host;
* trading EIP-712 chain = `714`;
* registration EIP-712 chain = `56`;
* соответствующий `asterChain`;
* testnet-only development flags разрешены.

### MAINNET

* exact mainnet REST host;
* exact mainnet WS host;
* trading EIP-712 chain = `1666`;
* registration EIP-712 chain = `56`;
* mainnet `asterChain`;
* dev/testnet overrides запрещены.

Все компоненты должны получать network data из одного profile:

* `App`;
* credentials identity construction;
* trading client;
* Direct onboarding;
* Builder onboarding;
* Builder reauthorization;
* preflight;
* signing;
* Mini App onboarding lifecycle.

Нельзя разрешать произвольные комбинации:

`mainnet URL + chain 714`,
`testnet URL + chain 1666`,
и т.п.

### Mainnet preflight

Перед READY:

* exact host/profile check;
* trading chain;
* clock;
* signer identity;
* Agent permissions;
* Builder proof;
* position mode;
* follower state readable;
* exchangeInfo readable;
* leverage preparation;
* no unresolved commands.

Position-mode metadata failure на Mainnet должен быть fail-closed.

### Acceptance

Один integration test должен собрать настоящий production `App` в MAINNET profile и доказать, что все компоненты получают согласованный network context без реального размещения order.

---

## PM-B5 — `zero_target` correctness — LIVE-COMPLETE

Статус: `[x]` prod ZeroReason taxonomy (sticky ТОЛЬКО
`PROPORTIONAL_REDUCTION_DUST`); live 6/6 green в одном 386s прогоне на REAL
Aster Testnet — Dust-1 (master 0.008 shaping), Dust-2, Dust-3 (SHORT-leg fix);
каждый live test заканчивается flat/zero-open-orders, второй App не запускался.

### Проблема

Sticky intentional-flat должен появляться только после proportional master reduction, которая округлилась до нуля.

Сейчас фактически используется слишком широкое условие:

`target_abs == 0 && current_abs > 0`.

Zero может возникнуть и по другой причине.

Тогда follower способен ошибочно остаться flat до конца master episode.

### Что изменить

Причина target=0 должна быть известна явно.

Sticky marker разрешён только для:

`PROPORTIONAL_REDUCTION_DUST`.

Не sticky:

* risk cap temporarily zero;
* stop move;
* flip transition;
* stale state;
* temporary risk constraint;
* manual reconciliation;
* safety flatten.

### Acceptance

Сценарий:

`non-dust zero → condition recovers → follower снова может increase`
обязан работать в том же master episode.

---

## PM-B6 — Existing position without Stop — LIVE-COMPLETE

Статус: `[x]` prod PM-B6 naked-flatten invariant; live green на REAL Aster
Testnet в том же 386s прогоне — Protection-cancel/naked legs (manual follower
Stop cancellation → recovery/flatten по usable master Stop); каждый live test
заканчивается flat/zero-open-orders, второй App не запускался.

### Проблема

Для нового entry отсутствие usable master Stop корректно блокирует entry.

Но существующая follower position может оказаться без Stop, если:

* master protection отсутствует/невалидна;
* follower protection была вручную удалена или исчезла.

Для risk-on-stop модели это naked exposure.

### Что изменить

Добавить invariant:

> Если follower держит ненулевую позицию, а у неё нет ни usable master Stop для восстановления, ни существующего follower Stop, позиция должна быть safety-flattened.

Не создавать искусственный Stop без source-of-truth уровня.

### Acceptance

Manual cancellation follower Stop + master without usable Stop → follower закрывается, а не остаётся naked.

---

## PM-B7 — Leverage / liquidation safety — LIVE-COMPLETE

Статус: `[x]` prod operator cap + liquidation invariant + fresh-entry leverage
gate (`LIQUIDATION_MMR_CUSHION=0.01`); live green на REAL Aster Testnet в том
же 386s прогоне (liquidation-hold leg). Key finds: S1 open→flatten livelock на
~200x найден и закрыт entry gate; liquidation-violation flatten ACTION доказан
unshapeable в cross-only multi-asset реальности (theorem sketch: нужен risk
>~100.5% против RiskPolicy cap 100%; isolated отвергнут с -4168 на copyc1 И
tg-8264892202, probed live) → тест утверждает честный hold contract + live
evaluation evidence (branch-verdict logs с реальными inputs); TEMP probe
добавлен и удалён, ноль TEMP sites осталось. Каждый live test заканчивается
flat/zero-open-orders, второй App не запускался.

### Проблема

Новый `LeverageManager` значительно улучшил leverage provisioning, поэтому старый finding про отсутствие bracket control больше не актуален.

Но production control-plane стремится подготовить максимально допустимое биржей leverage.

При этом:

* margin type сохраняется;
* leverage влияет на liquidation;
* risk model основана на Stop;
* реальный `liquidationPrice` уже доступен через `positionRisk`.

Позиция может ликвидироваться раньше, чем отработает предполагаемый Stop-risk.

### Что изменить

Не строить собственную сложную liquidation engine.

Минимальная mainnet policy:

* отдельный operator/runtime leverage safety cap;
* exchange max является верхней технической границей, а не автоматическим target;
* после исполнения новой/increased exposure читать authoritative `positionRisk`;
* сравнивать actual liquidation price с Stop;
* если liquidation safety invariant нарушен — reduce либо flatten.

Пример invariant:

LONG:

`liquidationPrice < stopPrice - safety_margin`

SHORT:

`liquidationPrice > stopPrice + safety_margin`

Точный margin должен быть конфигурируемым и простым.

### Acceptance

Позиция не считается безопасно converged только потому, что Stop существует; liquidation должна находиться дальше safety boundary.

---

## PM-B8 — Production follower operating envelope — LIVE-COMPLETE

Статус: `[x]` LIVE-COMPLETE. E1 prod DONE и проверен live (metrics.py p50/p95/p99 + snapshot,
symbol-aware snapshot scope, max_followers=10 cap, health convergence gate,
ONE_WAY-only v1 mainnet boot refusal [снят в Package G], priceProtect удалён);
E2 live: LIMIT L1/L3 green, L2 honest skip (биржа дважды full-fill вместо partial),
isolation 3/3 LIVE-COMPLETE за 633s (F1 429-transport, F2 −2015 + degraded,
F3 control-plane-denial); Package F hybrid load 3 REAL + 10 SHADOW 1 passed
за 3:39:15 (loadgate8). Launch cap v1 = 10 (замер, не мнение).
Этот пункт является launch blocker только если первый релиз сразу заявляет десятки followers.

### Проблема

Production-default follower snapshot сейчас использует all-symbol `openOrders`.

Это дорого по Aster request weight.

С увеличением followers система не обязательно упадёт — limiter её затормозит — но reconciliation latency может стать неприемлемой.

Load campaign этот production profile сейчас не тестирует, потому что включает symbol-scoped optimization.

### Что изменить

Normal production reconciliation сделать symbol-aware.

Использовать:

* whitelist;
* реально удерживаемые symbols;
* symbols с bot-owned orders;
* active master episodes.

Не делать all-symbol `openOrders(weight=40)` каждому follower на каждом sweep без необходимости.

Добавить метрики:

* request weight/min;
* reconciliation duration;
* follower snapshot latency;
* queue age;
* pending generations;
* limiter wait time;
* p50/p95/p99.

### Launch cap

Не обещать 100 followers до доказательства.

Первый production cap должен соответствовать измеренному envelope.

Допустим:

* сначала 10;
* затем 30;
* затем 100.

Изменение cap не требует изменения архитектуры.

---

# SHOULD FIX BEFORE MAINNET

## Health/degraded convergence

`degraded` нельзя очищать просто потому, что:

* текущий sweep не бросил exception;
* нет in-flight commands.

Нужно учитывать:

* FAILED commands;
* unresolved follower divergence;
* master dirty state;
* missing protection;
* reconnect recovery state;
* unresolved UNKNOWN;
* leverage/liquidation safety.

`degraded → healthy` только после доказанного authoritative convergence.

Статус E1: `[x]` prod DONE un-run-live — health convergence gate вместо
clear-on-clean-sweep (учитывает FAILED/divergence/dirty/protection/recovery/
UNKNOWN/liquidation).

---

## HEDGE config update

`ACCOUNT_CONFIG_UPDATE` leverage должен обновлять все соответствующие legs symbol, а не первую найденную position.

Position-mode switch должен вызывать authoritative rebuild, а не только log.

Если HEDGE не запускается в первой mainnet версии, можно вместо полноценного hardening сделать fail-closed product restriction:

`MAINNET v1 = ONE_WAY only`.

Это допустимый и более простой вариант.

Статус: `[x]` [superseded: Package G закрыл HEDGE 7/7 live (`test_pre_mainnet_hedge_live.py`), прод-баг #27 устранён, ONE_WAY-only boot refusal снят из `src/copytradeaster/app.py`, вердикт v1 = ONE_WAY + HEDGE].
Ранее: был выбран owner-allowed вариант `MAINNET v1 = ONE_WAY only` (full-HEDGE matrix dropped).

---

## Unsupported order semantics

Не реализовывать сейчас:

* trailing stop;
* OCO;
* OTOCO;
* multiple TP ladder;
* arbitrary exotic order types.

Но система должна явно их detect.

При unsupported master protection:

* не делать вид, что семантика полностью скопирована;
* не увеличивать risk;
* notifier/degraded должен показать причину.

Статус: out of scope для v1 (намеренно удалено из обязательных pre-mainnet задач, см. «Что намеренно удалено из старого плана»; система детектирует unsupported семантику и переходит в degraded без увеличения риска).

---

## Exchange filter validation

Проверять exchange filters на реальную новую mutation:

* quantity;
* stepSize;
* minQty;
* minNotional;
* relevant order type limits.

Не требовать, чтобы уже существующий residual position обязательно удовлетворял minNotional нового entry.

Статус: out of scope для v1 (намеренно удалено из обязательных pre-mainnet задач; базовые фильтры валидируются в safe-execution, blanket minNotional остатка не требуется).

---

## `priceProtect`

Выбрать один вариант:

* либо реально прокинуть `priceProtect` до HTTP;
* либо убрать поле из internal contract, если политика проекта всегда использует exchange default `FALSE`.

Не оставлять planner contract, который wire layer игнорирует.

Не является blocker сам по себе.

Статус E1: `[x]` выбран второй вариант — поле удалено из planner contract
(wire его никогда не нёс); 2 offline pins удалены.

---

# ОБЯЗАТЕЛЬНАЯ REAL ASTER TESTNET QUALIFICATION

КАЖДЫЙ ПУНКТ НИЖЕ ДОЛЖЕН ПРОЙТИ НА РЕАЛЬНОЙ TESTNET-СЕТИ С
РЕАЛЬНЫМИ TESTNET-КОШЕЛЬКАМИ. UNIT/FAKE/MOCK НЕ ЗАСЧИТЫВАЮТСЯ КАК
PRE-PRODUCTION ACCEPTANCE. Package F повторяет весь уже пройденный live-
набор единым финальным campaign, а не служит местом для отложенных
непроверенных сценариев.

Обозначения этого раздела: `[x]` — прошёл живой Testnet;
`[~]` — только часть live-сценария; `[ ]` — не прошёл live-проверку.

- [x] MARKET_OPEN accepted + response lost.
- [x] MARKET_CLOSE accepted + response lost (A1: real `allOrders` 7.7s-class, ровно 1 send).
- [x] Resolution endpoint временно недоступен (A2: all-reads-down UNKNOWN hold, zero retry).
- [x] FILLED order отсутствует в current-order query, найден через real `allOrders`.
- [x] Partial execution/outcome recovery (A3: real `positionRisk` delta, только residual).
- [x] SIGKILL в SENDING/UNKNOWN window (A5: boot CONFIRM без resend).
- [x] Proven-not-executed retry ровно один раз с новым COID (A4 на copyc3, 67 rows; main/copyc1 BTCUSDT `allOrders` saturated 1000/1000 — отсутствие там недоказуемо by design).

Главный invariant:

**не более одной economic mutation на один logical command.**

## Snapshot / WS

- [x] TRADE находится в queue во время real REST rebuild (S1 rebuild-holds-queue 100s green, финальное дерево).
- [x] Queue overflow на frames из real WS (финальный live campaign `test_pre_mainnet_master_live.py`, green).
- [x] Delayed stale TRADE из real WS (S2 stale-cutoff 44s green, финальное дерево).
- [x] Forced real listenKey reconnect (W1 reconnect-gap 83s green; missed-fill variant закрыт).
- [x] Reconnect resnapshot временно падает (W2 failed-resnapshot 68s green, финальное дерево).
- [x] После reconnect follower qty/Stop не дублировались и cleanup flat (REAL-06 App/WS/reconnect 134s green, run DB `data/pm-b-live-20260905T002000Z.sqlite3`; final-state matrix закрыта).

## Startup

- [x] Startup reconciliation искусственно замедлен (P1 startup-transient 45s green, финальное дерево).
- [x] Master реально меняется на Testnet между initial snapshot и WS ready (P1 + REAL-06 134s green).
- [x] REAL followers не открывают stale entry (P1 + REAL-06, flat/zero-open-orders audit).

## Network profile

- [x] MAINNET-profile production composition без order writes (live green).
- [x] Mixed-combo mode failure закрыт fail-closed (live green).
- [x] In-flight block перед READY (live green; итого 3/3 за 12s на REAL Aster Testnet).

## Dust

- [x] Proportional reduction → zero → sticky (Dust-1, master 0.008 shaping, green в 386s run).
- [x] Risk cap → zero → recovery → re-entry/increase (Dust-2, green в 386s run).
- [x] Flip zero не sticky (Dust-3, SHORT-leg fix, green в 386s run).

## Protection

- [x] Manual follower Stop cancellation (Protection-cancel leg, green в 386s run).
- [x] Master still has Stop → protection восстанавливается (green в 386s run).
- [x] Master no longer has usable Stop → position flatten (naked leg, green в 386s run).
- [x] Actual Stop trigger (green в 386s run, flat/zero-open-orders audit).
- [x] Actual TP trigger (green в 386s run, flat/zero-open-orders audit).
## LIMIT

- [x] L1 LIMIT entry (green, `test_pre_mainnet_limit_live.py`).
- [ ] L2 residual row — OPEN-SKIP: биржа дважды исполнила full, нужен real partial для residual (честный skip, не green).
- [x] L3 LIMIT reduction + final target convergence (green, `test_pre_mainnet_limit_live.py`).

Follower не обязан зеркалировать pending LIMIT master order до fill.

## HEDGE — Package G (owner decision 2026-09-05: проверить всё до прода)

Исходный owner-plan допускал `MAINNET v1 = ONE_WAY only` вместо полноценного
hardening; по ходу кампании матрица была помечена DROPPED без явного
подтверждения owner'а. Решение 2026-09-05: HEDGE возвращается в скоуп как
Package G (после закрытия F, до mainnet-HEDGE). E1 prod содержит ONE_WAY-only
v1 mainnet boot refusal — он остаётся, пока матрица ниже не зелёная; снятие
refusal только после green всей матрицы + flat-аудита.

- [x] LONG + SHORT одновременно (Package G, live green 2026-09-05, `test_hedge_long_and_short_together`, economic sends >= 2).
- [x] Разные fills на ногах (Package G, live green, `test_hedge_different_fills` — асимметрия LONG/SHORT без неттинга).
- [x] Stop/TP каждой leg (Package G, live green, `test_hedge_stop_per_leg_replace` — стопы на корректных сторонах + replace SHORT-стопа при нетронутом LONG).
- [x] Leverage config update на одной ноге (Package G, live green, `test_hedge_leverage_update_keeps_legs` — смена плеча мастера не дёргает ноги; плечом фолловера владеет движок).
- [x] Partial close одной leg (Package G, live green, `test_hedge_partial_close_one_leg` — LONG ужата вдвое, SHORT нетронут).
- [x] Flip/close без влияния на вторую (Package G, live green, `test_hedge_close_one_leg_keeps_other` — LONG flat + стопы сняты, SHORT цел).

HEDGE live-matrix CLOSED 2026-09-05 (`tests/testnet/test_pre_mainnet_hedge_live.py`,
7 тестов: G0 pre-check + G1–G6). ONE_WAY-only v1 mainnet boot refusal СНЯТ
(`app.py`: удалены `_MainnetHedgeViolation`, `_require_one_way_v1_config` и
mainnet-ветки step 5; везде warn-and-fix). Найден и пофикшен прод-баг: биржа
отдала `liquidationPrice=25.5M` на 80k LONG-ноге → PM-B7 сносил здоровую ногу
по кругу (фикс: liq с невозможной стороны от входа = unknown,
`Coordinator._liquidation_violated`). Финальный signed audit: FLAT-ALL.

## Failure isolation — LIVE-COMPLETE (3/3, один прогон 633s, финальное дерево)

Несколько REAL followers (`test_pre_mainnet_isolation_live.py`): F1 429-transport
(seam исправлен: FollowerRuntime.client, не master_client), F2 permission −2015
+ degraded-доказательство (прод-фикс: degraded на sweep-фейлы), F3 переформулирован
в control-plane-denial (премиса 1x-margin мертва — движок сам ставит плечо;
fresh-cycle heal после ENTRY_DEVIATION-диагноза; diagnostic timeouts).
Найдены и починены прод-баги: boundary-DoS от одного битого фолловера
(tolerance по reachable), silent sweep failures, WS-brownout drift
(mark dirty + EVERY_SWEEPS 10→4).

- [x] Один REAL follower получает injected 429/network error.
- [x] Один REAL follower получает permission error.
- [x] Control-plane denial вместо insufficient margin (см. выше).
- [x] Остальные REAL followers продолжают convergence.

## Load

Не требуется 30 реальных кошельков.

Достаточный gate:

- [x] 3 REAL followers (rescope: 3–5 по исходному плану, gate прошёл на 3; FULL GREEN 1.0/loadgate8 3:39:15).
- [x] 10 SHADOW slots (rescope со 100: замер load12 — ~103×~7 ≈ 721+ weight/sweep
  против бюджета 40/s → свипы 17–37s против 5s freshness window →
  stale→dirty→3-pass-boundary congestion collapse; потолок ≈ 40×5/7 ≈ 28
  абсолютный → v1 cap 10 с запасом; mainnet-бюджеты шире — cap может расти
  по измеренному mainnet-envelope без изменения кода; FULL GREEN 1.0/loadgate8 3:39:15).
- [x] Resumability: `CT_LOAD_PHASES` (a,restart,b,sigterm,c,sigkill,d,audit) +
  `CT_LOAD_SCALE` (дефолт 1.0 = 30/60/80/100) — FULL GREEN 1.0/loadgate8 3:39:15.
- [x] Production-equivalent request profile (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] Минимум 100 cycles (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] Measured request-weight/latency (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] No unresolved journal rows (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] No duplicate mutations (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] Final flat/order-free audit (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] Restart (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] SIGTERM (FULL GREEN 1.0/loadgate8 3:39:15).
- [x] SIGKILL (FULL GREEN 1.0/loadgate8 3:39:15).

---

# OPTIONAL / POST-MAINNET

Не задерживают первый запуск:

* trailing stop;
* OCO/OTOCO;
* multiple TP;
* 30+ REAL-wallet load campaign;
* PostgreSQL;
* Redis;
* adaptive distributed scheduler;
* полноценный собственный liquidation calculator;
* multi-process architecture;
* automatic complex margin-mode management.

---

# Порядок реализации

Работу вести короткими независимыми пакетами.

## Package A — Economic idempotency

PM-B1.

Сначала полностью закрыть UNKNOWN.

Не смешивать с остальными изменениями.

После implementation:

* unit/Fake/integration — только supplemental regression;
* ОБЯЗАТЕЛЬНО вся UNKNOWN-matrix на реальном Aster Testnet;
* реальные signed fault-injection и restart/SIGKILL.

---

## Package B — Master recovery correctness

PM-B2 + PM-B3.

Закрыть одной моделью:

* dirty state;
* snapshot generation;
* queue invalidation/coalescing;
* reconnect;
* startup entry gate.

После implementation обязательно тестировать race conditions, а не только обычный reconnect.
ВСЯ Snapshot/WS + Startup matrix должна пройти на реальном Testnet;
offline race-tests package не закрывают.

---

## Package C — Mainnet profile

PM-B4.

После него должен существовать технически валидный MAINNET composition, но реальные trade writes ещё не включать.
Все exchange-facing profile/signing/onboarding paths пройти на REAL Testnet;
MAINNET composition проверить без order writes.

---

## Package D — Risk invariants

PM-B5 + PM-B6 + PM-B7.

* correct zero reason;
* no naked existing exposure;
* leverage cap;
* liquidation-vs-stop.

Вся Dust/Protection/HEDGE матрица — обязательно REAL Testnet.

---

## Package E — Production scale

PM-B8 + health convergence.

Сделать production snapshot profile и определить реальный launch cap.
Acceptance: 3–5 REAL Testnet followers + 10 SHADOW slots (rescope-замер: 100 вне rate-limit envelope, потолок ≈28 → v1 cap 10 с запасом); чистый
SHADOW-only load не засчитывается.

---

## Package F — Final signed qualification

Никакого нового feature development.

Только:

* повторный полный REAL Testnet campaign всех сценариев выше;
* restart/SIGKILL;
* App E2E;
* multi-follower failure isolation;
* load;
* evidence report.


---

## Package G — HEDGE live qualification

7/7 live (`tests/testnet/test_pre_mainnet_hedge_live.py`): G0 pre-check + G1 LONG+SHORT + G2 asymmetry + G3 stop replace + G4 leverage + G5 partial + G6 close.
Прод-баг #27 (мусорный liquidationPrice=25.5M → wrong-side guard в `Coordinator._liquidation_violated`).
ONE_WAY-only boot refusal снят из `src/copytradeaster/app.py`. Финальный audit FLAT-ALL.
---

# Final Mainnet Gate

Проект получает статус:

## MAINNET READY

только если одновременно выполнено:

* UNKNOWN не может duplicate economic order;
* authoritative master recovery не double-counts events;
* stale startup entry невозможен;
* mainnet network/signing profile полностью согласован;
* zero-target semantics исправлена;
* position не остаётся naked без Stop;
* leverage/liquidation invariant существует;
* degraded отражает реальную convergence;
* заявленный follower count проходит load envelope;
* final signed Testnet campaign зелёный;
* restart/SIGKILL recovery зелёный;
* после кампании нет unresolved commands;
* exchange audit показывает ожидаемые positions/orders;
* no BLOCKER findings remain.

Первый production rollout делать ограниченным:

1. минимальный капитал;
2. 1 master;
3. небольшой follower cap;
4. ограниченный symbol whitelist;
5. ONE_WAY или HEDGE (superseded: Package G прошёл полную live-квалификацию 7/7, refusal снят; для v1 доступны оба режима);
6. observability по UNKNOWN, degraded, queue age, reconciliation latency и request weight;
7. постепенное увеличение follower cap только после production evidence.

---

# Что намеренно удалено из старого плана

Не являются обязательными pre-mainnet задачами:

* зеркалирование pending LIMIT intents;
* 30 отдельных REAL testnet followers;
* обязательная искусственная реальная liquidation master;
* PostgreSQL;
* сложная event-sourcing архитектура;
* trailing/OCO/OTOCO/multi-TP;
* blanket minNotional validation остатка позиции;
* большая отдельная abstraction только ради `priceProtect`.

Эти задачи дают слишком мало дополнительной безопасности относительно сложности и не должны превращать Pre-Mainnet Hardening в бесконечный enterprise-проект.

---

# Текущий статус

**PRE-PRODUCTION ACCEPTANCE — ТОЛЬКО REAL ASTER TESTNET.
FAKE/OFFLINE НЕ ЗАСЧИТЫВАЮТСЯ.**

Текущее состояние:

**Кампания A–G полностью закрыта на REAL Aster Testnet. Isolation 3/3 закрыт за 633s,
LIMIT 2/3 (L1/L3 green + L2 honest skip), Package F hybrid load (3 REAL + 10 SHADOW
rescope-замер + restart/SIGTERM/SIGKILL) 1 passed за 3:39:15 (loadgate8), Package G
HEDGE live matrix 7/7 закрыт с фиксом прод-бага #27 и снятием ONE_WAY-only boot refusal.
Offline unit: 1068 passed, ruff clean, flat audit везде. Вердикт v1 (ONE_WAY + HEDGE): GO.
Финальное решение по выходу в mainnet остаётся за owner'ом.**
