# Pre-Mainnet Hardening Plan

Проект: `JDBACK/copytradeaster`
Исходный audit baseline: `3c38db34f42d7a61c334367edbc434e6fbb97c8a`
Текущий рабочий срез: ветка `codex/live-copy-tests-hardening`, checkpoint `918bf46`
Подробное текущее состояние: [PRE_MAINNET_HARDENING_PROGRESS_2026-09.md](reports/PRE_MAINNET_HARDENING_PROGRESS_2026-09.md)
Отчёт для следующей сессии: [PRE_MAINNET_HARDENING_HANDOFF_2026-09.md](reports/PRE_MAINNET_HARDENING_HANDOFF_2026-09.md)

Статус на 2026-09-04: **Package A сделан; Package B — WIP / NOT GREEN; Packages C–F не начинались.**

Обозначения: `[x]` — сделано и подтверждено; `[~]` — частично/WIP; `[ ]` — осталось.

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

## PM-B1 — UNKNOWN economic outcome / duplicate execution — DONE 2026-09-04

Статус: `[x]` реализовано в Executor, journal/recovery API, Builder fallback и boot
recovery; unit/integration/signed Testnet evidence записаны в подробном журнале.

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

## PM-B2 — Master snapshot / WS queue recovery boundary — WIP / NOT GREEN

Статус: `[~]` красная race-репродукция выполнена; в ветке есть частичная реализация
generation/dirty, concurrent WS receive, overflow/disconnect recovery. После production-
изменений focused tests ещё не доведены до зелёного состояния.

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

---

## PM-B3 — Startup stale-entry gate — WIP / NOT GREEN

Статус: `[~]` process-local entry admission и close-only startup boundary добавлены
частично; acceptance-тесты и полный post-change gate ещё не подтверждены.

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

## PM-B4 — Mainnet NetworkProfile — TODO

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

## PM-B5 — `zero_target` correctness — TODO

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

## PM-B6 — Existing position without Stop — TODO

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

## PM-B7 — Leverage / liquidation safety — TODO

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

## PM-B8 — Production follower operating envelope — TODO

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

---

## HEDGE config update

`ACCOUNT_CONFIG_UPDATE` leverage должен обновлять все соответствующие legs symbol, а не первую найденную position.

Position-mode switch должен вызывать authoritative rebuild, а не только log.

Если HEDGE не запускается в первой mainnet версии, можно вместо полноценного hardening сделать fail-closed product restriction:

`MAINNET v1 = ONE_WAY only`.

Это допустимый и более простой вариант.

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

---

## Exchange filter validation

Проверять exchange filters на реальную новую mutation:

* quantity;
* stepSize;
* minQty;
* minNotional;
* relevant order type limits.

Не требовать, чтобы уже существующий residual position обязательно удовлетворял minNotional нового entry.

---

## `priceProtect`

Выбрать один вариант:

* либо реально прокинуть `priceProtect` до HTTP;
* либо убрать поле из internal contract, если политика проекта всегда использует exchange default `FALSE`.

Не оставлять planner contract, который wire layer игнорирует.

Не является blocker сам по себе.

---

# TESTNET PROOF

После BLOCKERS нужен отдельный final signed campaign.

Не использовать FakeAster как доказательство exchange semantics там, где важны реальные Aster responses.

## UNKNOWN

* MARKET_OPEN accepted + response lost;
* MARKET_CLOSE accepted + response lost;
* resolution endpoint временно недоступен;
* FILLED order отсутствует в current-order query;
* partial execution;
* SIGKILL в SENDING/UNKNOWN window.

Главный invariant:

**не более одной economic mutation на один logical command.**

## Snapshot / WS

* TRADE находится в queue во время REST rebuild;
* queue overflow;
* delayed stale TRADE;
* reconnect с пропущенным fill;
* reconnect resnapshot временно падает;
* после восстановления final state совпадает с Aster REST.

## Startup

* startup reconciliation искусственно замедлен;
* master меняется между initial snapshot и WS ready;
* follower не открывает stale entry.

## Dust

* proportional reduction → zero → sticky;
* risk cap → zero → recovery → re-entry/increase;
* flip zero не sticky.

## Protection

* manual follower Stop cancellation;
* master still has Stop → protection восстанавливается;
* master no longer has usable Stop → position flatten;
* actual Stop trigger;
* actual TP trigger.

## LIMIT

* LIMIT entry multiple partial fills;
* cancellation остатка;
* LIMIT reduction multiple partial fills;
* final target convergence;
* no duplicate copy episode.

Follower не обязан зеркалировать pending LIMIT master order до fill.

## HEDGE

Только если HEDGE входит в first-mainnet scope:

* LONG + SHORT одновременно;
* разные fills;
* Stop/TP каждой leg;
* leverage config update;
* partial close одной leg;
* flip/close без влияния на вторую.

Иначе HEDGE явно отключается на v1.

## Failure isolation

Несколько REAL followers:

* один получает 429/network error;
* один permission error;
* один insufficient margin;
* остальные followers продолжают convergence.

## Load

Не требуется 30 реальных кошельков.

Достаточный gate:

* 3–5 REAL followers;
* 100 SHADOW slots;
* production-equivalent request profile;
* минимум 100 cycles;
* measured request-weight/latency;
* no unresolved journal rows;
* no duplicate mutations;
* final flat/order-free audit;
* restart;
* SIGTERM;
* SIGKILL.

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

* unit;
* Fake negative tests;
* integration;
* signed testnet fault-injection;
* restart/SIGKILL.

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

---

## Package C — Mainnet profile

PM-B4.

После него должен существовать технически валидный MAINNET composition, но реальные trade writes ещё не включать.

---

## Package D — Risk invariants

PM-B5 + PM-B6 + PM-B7.

* correct zero reason;
* no naked existing exposure;
* leverage cap;
* liquidation-vs-stop.

---

## Package E — Production scale

PM-B8 + health convergence.

Сделать production snapshot profile и определить реальный launch cap.

---

## Package F — Final signed qualification

Никакого нового feature development.

Только:

* testnet campaign;
* restart/SIGKILL;
* App E2E;
* multi-follower failure isolation;
* load;
* evidence report.

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
5. желательно ONE_WAY-only, если HEDGE не прошёл отдельную qualification;
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

**Implementation разрешена.**

Следующая задача:

**Package A — Economic idempotency / UNKNOWN outcome hardening.**

До закрытия Package A переходить к реальному Mainnet нельзя.
