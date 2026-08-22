Я прошёл `AGENTS.md`, все документы в `docs`, Stage 1 archive, live-test status, API research, архитектуру, основной runtime-код, storage/nonce, projector, risk, planner/coordinator/executor/reconciler, CLI, FakeAster, unit/integration/replay/testnet tests ветки `codex/stage1-core`.

## Вердикт

**Нет — в текущем состоянии Stage 1 я бы не принимал.**

При этом фундамент проекта **хороший и переделывать всё с нуля не нужно**. Выбранная архитектура — modular monolith + target state + reconciliation + durable journal — для этого проекта правильная. В коде хорошие границы `risk → planner → executor`, `Decimal`, encrypted credentials, общий rate limiter, follower isolation, place-new-before-cancel-old и нормальный test layout. Это действительно уже похожий на торговый engine код, а не прототип.

Проблема в другом: **несколько фундаментальных инвариантов, которые сама архитектура объявляет обязательными, фактически не выполняются**. Некоторые способны оставить follower с неправильным размером, не закрыть позицию, повторно отправить финансовую команду или не скопировать защиту.

Поэтому мой статус сейчас:

| Область                               | Оценка                                  |
| ------------------------------------- | --------------------------------------- |
| Общая архитектура                     | 🟢 хорошая основа                       |
| Domain / Risk / Planner decomposition | 🟢                                      |
| Aster V3 adapter                      | 🟢/🟡                                   |
| Master state projection               | 🔴                                      |
| Target-state reconciliation           | 🔴                                      |
| Durable execution / crash recovery    | 🔴                                      |
| Risk invariant                        | 🔴                                      |
| SL/TP copy semantics                  | 🔴                                      |
| Pause / emergency safety              | 🔴                                      |
| One-way mode                          | 🔴                                      |
| HEDGE basic happy path                | 🟢/🟡                                   |
| Unit/integration tests                | 🟢 по объёму, 🟡 по качеству assertions |
| Signed acceptance coverage            | 🔴 неполная                             |
| 30–100 followers architecture         | 🔴 сейчас не выдерживает                |

---

# Что критично исправить

## 🔴 P0. Reconciliation сейчас не является по-настоящему state-based

Это, пожалуй, главный архитектурный блокер.

Для существующей позиции follower целевой размер вычисляется через:

```python
prev_abs = self._master_abs.get(...)
...
target_abs = scaled_reduce(current_abs, reference, master_abs, ...)
```

Но `_master_abs` — просто RAM текущего процесса. Причём он обновляется **до исполнения follower plan**.

Отсюда два очень неприятных сценария.

**Restart с пропущенным partial close.**

Мастер был `1 BTC`, follower соответствовал ему. Пока бот выключен, мастер уменьшил позицию до `0.5 BTC`.

После restart:

* REST snapshot видит master = `0.5`;
* follower всё ещё имеет старый размер;
* `_master_abs` пуст;
* reference становится текущим `0.5`;
* `scaled_reduce(current, 0.5, 0.5)` возвращает `current`.

**Никакого сокращения не будет.**

То есть именно тот сценарий, ради которого строился state-reconciliation, восстановить состояние не умеет.

Ещё хуже при runtime failure: `_master_abs` обновляется перед `executor.submit()`. Если follower partial-close не выполнился, следующий reconcile уже может забыть старый master size и перестать пытаться уменьшить follower.

Это противоречит самой архитектуре, где REST snapshot + journal должны лечить потерянные события/restart.

### Что нужно

У target engine должен быть **persisted reference/baseline trade state**, а не предыдущий master qty из памяти.

Например у trade episode хранить как минимум:

`master_initial_qty / follower_initial_qty / current master revision`

либо эквивалентный persisted sizing basis.

Тогда:

> current exchange state + persisted trade state → desired follower state

и restart действительно становится детерминированным.

---

## 🔴 P0. Idempotency key может блокировать совершенно легитимную следующую команду

Сейчас:

```python
command_key =
sha256(account | trade_id | kind | canonical payload)
```

Это выглядит логично, но **payload не является identity бизнес-операции**.

Пример:

master:

`1.00 → 0.75 → 0.50`

Follower может дважды подряд потребовать:

`MARKET_CLOSE 0.025 BTC`

Обе команды имеют одинаковые:

* account;
* trade_id;
* kind;
* payload.

Вторая получает тот же `command_key`. Executor увидит старый `CONFIRMED` и **не отправит второй close**.

Ещё более опасный вариант — повторение одного и того же SL:

`64500 → 64000 → 64500`

Второе размещение `64500` может совпасть с первым command key.

И есть особенно плохой случай с закрытием сделки: master trade episode закрывается в `MasterProjector` **до** follower reconciliation. `_stamp_trade_id()` ищет только active trade, поэтому follower close получает `trade_id=None`.

Две разные торговые сессии:

`close BTC 0.019`

могут получить один и тот же key.

Это означает: **вторая реальная сделка потенциально не закроется потому, что похожая команда уже успешно выполнялась когда-то раньше.**

То же касается `emergency-close`, где `trade_id` тоже может быть `None`.

### Что нужно

Idempotency должен идентифицировать **конкретный desired-state transition/revision**, а не просто payload.

Например:

`account + trade_episode + target_revision + action_kind + payload`

При этом retry одного transition должен сохранять один logical command ID.

---

## 🔴 P0. Durable journal не закрывает реальные crash windows

Документ проектирует:

`PENDING → SENDING → SUBMITTED → CONFIRMED / UNKNOWN`

и требует recovery.

Но boot восстанавливает **только `UNKNOWN`**. `SENDING` и `SUBMITTED` вообще не рассматриваются.

Классический crash:

> journal=SENDING → exchange принял ордер → процесс умер до SUBMITTED

После restart этот command не разрешается через exchange query.

Потом reconciliation потенциально создаёт resend.

Есть и конкретный баг timeout-ветки:

```python
except TimeoutError:
    resolved = ...
    if resolved:
        return resolved
```

Если order не найден, retry-логика находится уже в `except AsterError`, а timeout execution проваливается дальше к `_confirm(..., order)`, хотя `order` не присвоен.

Кроме того, реальный `AsterClient` использует `httpx`; его transport timeouts не преобразуются в built-in `TimeoutError`, который ловит Executor.

И ещё:

UNKNOWN → `query_order()` found → command ставится `CONFIRMED`, **но `_record_link()` не вызывается**. Значит recovered STOP/TP не попадёт в `order_links`, что ломает follower-own-stop/TP guard.

`SUBMITTED` тоже опасен: если conditional после submit не найден в `openOrders`, он остаётся `SUBMITTED`; последующие submit видят `SUBMITTED` как терминальный success и больше не пытаются восстановить защиту.

### До приёмки

Recovery должен детерминированно разрешать как минимум:

`SENDING`, `SUBMITTED`, `UNKNOWN`

через exchange state.

---

# 🔴 P0. Nonce нарушает собственный persist-before-send contract

Архитектура специально требует:

> nonce сначала сохранить, **потом** отправлять signed request.

И в repository действительно есть правильный по идее `NonceStore`, который делает commit до return.

Но production App его **не использует**.

`AsterClient` получает `SqliteCredentials` как `NonceSource`.

А там:

```python
self._nonces[...] = candidate
loop.create_task(self._persist_nonce(...))
return candidate
```

То есть HTTP уже может уйти, пока nonce ещё не записан.

Crash в этом окне → restart может повторить nonce.

Причём тест специально делает:

```python
await asyncio.sleep(0.05)  # persistence task
```

и лишь потом проверяет БД. Он подтверждает не persist-before-send, а как раз asynchronous persistence.

Для финансового signed API это блокер.

Дополнительно manual master script и основной процесс могут работать отдельными процессами с одним signer — тогда нужен ещё и atomic cross-process nonce allocation, а не только in-memory counter.

---

# 🔴 P0. `pause` сейчас декоративный

CLI:

`pause` → пишет `app_state.allow_new_entries=false`.

Но Coordinator при принятии решения смотрит:

```python
policy.allow_new_entries
```

и **никогда не читает глобальный `app_state.allow_new_entries`**.

То есть:

> CLI: `paused`

а новые сделки могут продолжить открываться.

Почему тест зелёный? Потому что `test_pause_blocks...` не вызывает CLI pause — он напрямую переписывает `RiskPolicy.allow_new_entries=False`.

А CLI-тест проверяет только текст `paused/resumed`, а не поведение торгового engine.

Для safety switch это безусловный blocker.

---

# 🔴 P0. `max_entry_deviation` и post-fill risk protection фактически не подключены

API research правильно требует:

> до входа проверить max deviation; после follower fill пересчитать реальный risk и при превышении уменьшить позицию.

Функции есть:

* `deviation_exceeded()`;
* `post_fill_cap()`.

И они протестированы отдельно.

Но Coordinator:

* не вызывает `deviation_exceeded`;
* новый follower entry рассчитывает от **master historical entry_price**, а не от ожидаемой текущей follower entry;
* после confirmation не делает post-fill reconciliation;
* `App._on_confirmed()` только печатает сообщение.

Более того, даже periodic sweep не гарантирует post-fill reduction: если master size и stop не изменились, branch C фактически сохраняет текущий follower qty вместо применения `cap`.

Это очень важно именно для копитрейда.

Master 65k, follower может исполниться 65.4k. На бумаге `risk=1%`, а реально уже больше.

Сейчас configured:

`max_entry_deviation_pct=1%`

может существовать в БД и выглядеть рабочим, но торговый путь его не использует.

---

# 🔴 P0. Legitimate stop в прибыли считается «invalid»

В Coordinator есть:

```python
long and stop >= master_entry -> NO_STOP
short and stop <= master_entry -> NO_STOP
```

Для новой сделки это может иметь смысл как sanity check.

Для **существующей** сделки — нет.

Например:

* long entry = 65,000
* market = 67,000
* master переносит SL = 66,000

Это абсолютно нормальный protective stop, фиксирующий прибыль.

Ваш Coordinator назовёт его `NO_STOP` и **не перенесёт follower stop**.

Это напрямую ломает одну из главных функций проекта — перенос SL вслед за мастером.

Проверять stop нужно относительно trigger/current-market semantics, а не старой entry price.

---

# 🔴 P1. Один tick движения SL намеренно игнорируется

Planner считает уровни одинаковыми, если:

```python
abs(a - b) <= tick
```

Но биржевой уровень уже лежит на tick grid. Поэтому изменение:

`64500.0 → 64500.1`

при tick `0.1` — это **реальное минимально возможное движение цены**, а не floating noise.

Тест даже специально закрепляет это неправильное поведение:

`test_stop_within_tick_noop`.

Для копирования Stop нужен equality после нормализации к tick, а не `<= tick`.

---

# 🔴 P0/P1. Follower SL/TP state reconciliation неполный

Здесь сразу несколько связанных дефектов.

Follower snapshot распознаёт только:

```text
STOP_MARKET
TAKE_PROFIT_MARKET
```

но **не `STOP` / `TAKE_PROFIT`**.

При этом Stage 1 отдельно заявляет исправленный support limit-flavour protective orders, и test проверяет только отправку `STOP`, а не следующий snapshot/reconcile.

Результат:

> STOP успешно поставили → следующий reconciliation его не видит → считает stop missing → может поставить ещё один.

Это очень хорошо объясняет записанное в live status:

> возможны duplicate stops during fast convergence.

Но причина глубже, чем просто fast convergence.

Ещё Planner при:

```python
wanted_level is None
```

просто делает `continue`.

То есть:

> master TP был → master TP отменён

target теперь `tp=None`, но follower TP **не отменяется**.

Это уже нарушение target-state semantics.

И если actual position = 0, Planner возвращает `[]` **до отмены protective orders**. Поэтому orphan conditionals на flat follower также не являются converged state.

---

# 🔴 P1. HEDGE recovery защит тоже подозрительно сломан

REST master snapshot считает protective только ордер:

```python
reduce_only or close_position
```

Но ваш же manual HEDGE script ставит protection как:

* конкретный `positionSide`;
* quantity;
* без `reduceOnly`;
* без `closePosition`.

То есть после reconnect такой HEDGE stop может исчезнуть из reconstructed `MasterState`.

Streaming projector его видит, REST rebuild — потенциально уже нет.

Для recovery source-of-truth это серьёзный разрыв.

---

# 🔴 P0/P1. Master projector может терять корректные fills

Документ API специально предупреждает про duplicate/out-of-order и требует reconciliation.

Но projector делает:

```python
if E < last_event_time:
    ignore
```

глобально для всех типов событий.

ACCOUNT_UPDATE при этом позиции не меняет.

Представим:

```text
ACCOUNT_UPDATE E=2001 пришёл первым
ORDER_TRADE_UPDATE TRADE E=2000 пришёл потом
```

TRADE будет выброшен как старый.

Master position останется неправильной до `rebuild_full()`.

А periodic reconciliation **не делает master REST rebuild**, она использует тот же `projector.state`.

То есть ошибка может жить до следующего WebSocket reconnect.

Ещё dedup TRADE:

```text
event_type : E : order_id : x
```

не содержит `trade id t`.

Два partial fill одного order в одном `E` могут конфликтовать.

Тесты не ищут такую проблему; наоборот `test_out_of_order_older_E_ignored` закрепляет простое отбрасывание старого `E` как желаемое поведение.

---

# 🔴 P1. Projected entry price математически неверна при partial fills/reductions

Projector обновляет entry:

```python
(abs(old_qty) * old_entry + last_qty * ap) /
(abs(old_qty) + last_qty)
```

Но `ap` — cumulative average order price, а не last fill price.

При reduction remaining position вообще обычно не должна re-average entry по цене выхода.

Получается projected entry постепенно расходится с exchange state.

А эта цена дальше участвует в follower sizing и stop sanity.

Для state-based engine правильнее либо:

* использовать `L` для incremental fill math;
* корректно различать increase/reduce/flip;
* либо после significant fill брать authoritative `positionRisk`.

---

# 🔴 P1. Liquidation/ADL path не выглядит закрытым

Position mutator срабатывает только при:

```python
execution_type == "TRADE"
```

Conditional update — только `NEW/CANCELED`.

В вашем API contract присутствуют и другие execution types, включая `CALCULATED`, а также special autoclose/ADL cases. Плюс `MARGIN_CALL` только пишет `degraded`, позицию не меняет.

Так как ACCOUNT_UPDATE informational и periodic master REST snapshot отсутствует, этот путь я **до production acceptance обязательно вынес бы в отдельный signed test**. Сейчас нет достаточного доказательства, что liquidation master гарантированно приведёт followers к flat.

---

# 🔴 P1. Trade episode identity ломается на нескольких позициях и recovery

`ExecutionPlan` содержит **один** `trade_id`.

Coordinator берёт первый active trade среди targets:

```python
for tp in targets:
    ...
    trade_id = first active
    break
```

Если одновременно BTC + SOL или HEDGE LONG + SHORT, все actions одного follower-plan могут записаться под episode **первого** target.

Это ломает `order_links` и, следовательно, защиту:

> follower own STOP/TP filled → не re-enter same master episode.

Manual campaign BTC+SOL не проверяет корректность link ownership.

Кроме того, `rebuild_full()` восстанавливает MasterState, но **не синхронизирует `copy_trades` lifecycle**.

Если master закрыл сделку, пока бот был offline, старый `copy_trade` может остаться OPEN.

Это опять означает: restart state восстанавливается не полностью.

---

# 🔴 P1. Default ONE_WAY почти не покрыт и содержит реальную дыру

`.env.example` по умолчанию:

`CT_POSITION_MODE=ONE_WAY`.

Но live campaign была в **HEDGE**.

В ONE_WAY Coordinator/Projector специально отбрасывают flat `BOTH` position rows. Поэтому до первого входа engine теряет текущий leverage.

При первом WS fill `MasterProjector` создаёт позицию с:

```python
leverage=0
```

А Planner специально имеет тест:

> `test_new_position_no_set_leverage_action`

То есть initial one-way entry не устанавливает target leverage.

В следующем reconcile реальная leverage follower может уже конфликтовать с projected `0`.

Плюс `entry_quantity()` margin check использует `policy.max_leverage`, тогда как Coordinator target leverage делает:

```python
min(master_leverage, policy.max_leverage)
```

Если master 2x, policy max 20x, sizing может пройти margin-check как будто 20x, а затем реально пытаться работать с 2x.

**ONE_WAY нужен отдельный полноценный acceptance cycle.**

---

# 🔴 P0. `_client_for()` fail-open на master account

Это маленькая строка с очень неприятным blast radius:

```python
if follower not found:
    return self.master_client
```

В финансовом коде неизвестный `account_id` должен давать:

> hard error / fail closed.

Никогда:

> «не нашли follower → попробуем master».

Такой fallback я бы убрал до любого дальнейшего тестнета.

---

# Масштаб 30–100 followers

Тут архитектура пока тоже не готова.

Follower snapshot выполняет:

```text
balance             weight 5
openOrders(no symbol) 40
positionRisk        5
openOrders(no symbol) 40
```

≈ **90 weight на follower за один recompute**.

API contract в проекте действительно фиксирует `openOrders` без symbol = weight 40.

Получается:

| followers | один полный recompute |
| --------: | --------------------: |
|         4 |           ~360 weight |
|        10 |                  ~900 |
|        30 |                 ~2700 |
|       100 |                 ~9000 |

При configured limit `2400/min`.

То есть **один master event при 30 followers уже превышает минутный request-weight budget**.

Причём `changed_symbols` сейчас фактически не помогает: follower делает full REST snapshot.

А UserStream вызывает callback через `await` прямо внутри receive loop. Пока Coordinator опрашивает и исполняет followers, WS consumer не занимается следующим событием. Для 30–100 accounts это создаёт backpressure именно на master feed.

Research изначально правильно предлагал:

> WS → queue/event inbox → state machine → execution/reconciliation.

Я бы восстановил именно эту границу.

Не нужен Kafka/RabbitMQ. Для вашего масштаба достаточно внутренней bounded asyncio queue + durable event ledger, но **WS ingest нельзя ждать десятки follower REST calls**.

---

# Что с тестами

Здесь есть хорошие новости.

По `ENGINE_TEST_STATUS` записано:

> 220 passed, 90.28% coverage, ruff clean

и большая ручная HEDGE testnet campaign с long/short, SL/TP, restart, limit protection, multi-position и проверкой exchange coid. Это сильнее обычного MVP.

Но я бы **не использовал 220/90% как acceptance argument**, потому что ряд тестов проверяет именно текущие ошибочные предположения.

Например:

| Тест                  | Что происходит                                                                                                             |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| out-of-order          | специально ожидается drop старого `E`                                                                                      |
| one-tick SL           | специально ожидается no-op                                                                                                 |
| new position leverage | специально ожидается отсутствие SET_LEVERAGE                                                                               |
| pause                 | Coordinator тестирует policy pause, CLI тестирует только флаг; связки нет                                                  |
| UNKNOWN               | recovery не проверяет создание OrderLink                                                                                   |
| Timeout               | Fake умеет TimeoutError, но executor journal suite не проверяет реальную problematic path                                  |
| restart               | проверяется вручную вставленный `UNKNOWN`, а не `SENDING/SUBMITTED`, missed partial close или восстановление trade episode |
| reconciliation        | consistent current state проверяется; recovery desired qty после missed master delta — нет                                 |

Это видно непосредственно в тестах.

FakeAster тоже достаточно оптимистичен. Например, его own fill accounting использует похожую упрощённую entry-price формулу и не моделирует многие реальные exchange constraints, поэтому некоторые bugs production-кода повторяются в fake и становятся невидимыми.

---

# А что с «12 signed testnet scenarios»?

Вот тут важное различие.

`ENGINE_TEST_STATUS` действительно описывает **12 успешно проведённых live/manual сценариев**.

Но автоматический файл:

`tests/testnet/test_signed_scenarios.py`

существенно меньше заявленного acceptance plan.

Например основной тест прямо говорит:

> `Full cycle on the master account only; follower copy is covered by e2e.`

То есть это не настоящий signed master→engine→follower acceptance.

В нём нет полного автоматического воспроизведения:

* kill/restart процесса;
* missed master partial close;
* follower post-fill cap;
* max entry deviation;
* two identical reductions;
* TP removal;
* stop beyond breakeven;
* duplicate protection convergence;
* SENDING/SUBMITTED crash;
* ONE_WAY cycle;
* out-of-order permutation;
* same-ms partial fills.

А research определял signed acceptance существенно шире — 27 пунктов, включая actual follower fill, deviation, One-way/Hedge, partial fill, reconnect, backend restart, Strategy, priceProtect и Builder.

Builder также формально не закрыт: status сам пишет, что финальный Builder e2e остался; test при отсутствии main keys просто `skip`, а сами response bodies практически не assert'ятся.

Поэтому на вопрос **«всё ли пройдено?» — нет.**

---

# Что я считаю хорошим и сохранял бы

Не хочу, чтобы из количества найденных проблем создалось впечатление, что Stage 1 неудачный. Я бы **не менял направление архитектуры**:

* modular monolith здесь подходит;
* `risk` без I/O — хорошо;
* pure planner — очень хорошо;
* persistent execution journal нужен;
* exchange DTO отдельно от domain — правильно;
* `Decimal` + запрет float — хороший financial invariant;
* place-new-before-cancel-old — правильная safety идея;
* shared limiter — правильная база;
* follower error isolation — правильная;
* raw master event ledger — обязательно оставить;
* periodic reconciliation — оставить, но сделать его действительно authoritative.

Основная работа — **довести эти идеи до фактических инвариантов**, а не строить новую систему.

Coordinator в 454 строки уже начинает брать на себя слишком много: snapshot, copy policy, sizing transitions, trade episode binding, protective-fill detection, fanout. Я бы не дробил его на 15 классов, но как минимум выделил:

`FollowerSnapshotReader` и `TargetBuilder/CopyPolicy`.

Тогда исчезнет часть текущих перекрёстных состояний вроде `_master_abs`.

---

# Мой acceptance gate для Stage 1

| До merge Stage 1                     | Обязательная проверка                                                                                    |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Persisted target basis               | restart после missed 50% partial close приводит follower ровно к target                                  |
| Correct episode/revision idempotency | два одинаковых partial-close подряд; две одинаковые сделки разных episodes; SL A→B→A                     |
| Complete journal recovery            | crash в PENDING/SENDING/SUBMITTED/UNKNOWN, без duplicate exchange order                                  |
| Correct nonce                        | persist-before-send + crash test; желательно concurrent/process test                                     |
| Real pause                           | `copytradeaster pause` → новые entries реально отсутствуют, closes проходят                              |
| Risk                                 | max deviation реально blocks; bad follower fill вызывает immediate reduce to cap                         |
| Protection convergence               | STOP/STOP_MARKET, TP flavours, TP removal, one-tick move, stop in profit, duplicate/orphan cleanup       |
| Master recovery                      | reordered fills, same-ms partial fills, missed close, liquidation/autoclose path                         |
| Episodes                             | restart while master closed/open; multi-symbol links принадлежат правильным trade IDs                    |
| Modes                                | полноценный signed ONE_WAY и полноценный signed HEDGE                                                    |
| Fail closed                          | unknown account никогда не маршрутизируется в master                                                     |
| Scale invariant                      | убрать второй `openOrders`, symbol-scoped reads/dirty symbols, WS ingestion decoupled from follower work |

После этого я бы **принимал Stage 1** и уже переходил дальше.

### Итог

Если дать простую оценку:

**архитектурное направление: 8.5/10**
**качество реализации happy-path: 8/10**
**tests breadth: 8/10**
**tests correctness/adversarial coverage: 5.5/10**
**crash/recovery correctness: 4/10**
**готовность принять Stage 1 сейчас: ❌ нет**

И самое важное: я вижу не «десятки мелких замечаний», а примерно **8–10 связанных core defects**. После их исправления большая часть остальных проблем автоматически уйдёт. Переписывать Stage 1 заново не требуется.

Главные четыре, которые я бы чинил первыми: **persisted target/recovery → command idempotency/journal → risk post-fill/deviation → protection convergence**. После них — event projector/ONE_WAY/rate-limit pipeline.

