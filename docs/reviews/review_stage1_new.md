Я повторно прошёл ветку `codex/stage1-core`: исходный `review_stage1.md`, архитектуру и цели проекта, `REFACTOR_STAGE1_REPORT.md`, `ENGINE_TEST_STATUS`, три новых коммита, итоговые версии `Coordinator / Planner / Executor / Projector / Storage / App` и новые acceptance/testnet-тесты.

## Вердикт

**Stage 1 пока не принимаю. Но теперь он действительно близок к приёмке.**

Это уже совсем другая ситуация, чем в первом ревью. Большая часть моих прошлых замечаний исправлена **по существу**, а не замаскирована тестами. Persisted basis, recovery журнала, pause, post-fill cap, per-leg trade attribution, fail-closed routing, one-tick replace, TP removal, out-of-order fills — это хорошие реальные изменения. Новый acceptance suite тоже стал значительно полезнее.

Но при чтении **итогового кода**, а не отчёта, я нашёл несколько новых/оставшихся дыр именно в финансовых инвариантах. Тут речь не про «давайте сделаем enterprise». Я бы не просил Redis, микросервисы, очереди, PostgreSQL и т. п. Архитектура для одного разработчика + LLM и 30–100 пользователей по-прежнему выбрана правильно: modular monolith + SQLite + asyncio. Именно так и надо оставлять.

### Что теперь реально исправлено

| Область прошлого ревью                     | Сейчас                             |
| ------------------------------------------ | ---------------------------------- |
| Persisted basis / restart partial close    | 🟢 Исправлено                      |
| Два одинаковых legitimate close            | 🟢 Исправлено                      |
| SENDING / SUBMITTED / UNKNOWN recovery     | 🟢 Сильно улучшено                 |
| recovered `order_links`                    | 🟢                                 |
| httpx timeout → UNKNOWN                    | 🟢                                 |
| persist-before-send nonce в основном пути  | 🟢                                 |
| CLI pause                                  | 🟢                                 |
| max entry deviation подключён              | 🟢                                 |
| post-fill cap                              | 🟢                                 |
| one-tick stop                              | 🟢                                 |
| TP removal                                 | 🟢                                 |
| orphan protection cleanup                  | 🟢                                 |
| per-action trade episode                   | 🟢                                 |
| out-of-order TRADE / same-ms fill          | 🟢                                 |
| entry math по `L`, reduction не re-average | 🟢                                 |
| SET_LEVERAGE на новой ноге                 | 🟢                                 |
| unknown account → master                   | 🟢 Исправлен критический fail-open |
| WS ingest отделён от follower work         | 🟢                                 |
| двойной `openOrders`                       | 🟢 Убран                           |

То есть модель после прошлого ревью проделала **очень приличный рефакторинг**. Я не вижу причины всё переписывать или менять архитектуру.

---

# Что блокирует приёмку

## 🔴 1. Самый опасный новый баг: replace защиты может удалить старый Stop после неуспешной установки нового

Planner правильно строит замену:

> PLACE new → CANCEL old

и тем самым хочет гарантировать, что follower ни на секунду не останется без защиты.

Но `Executor.submit()` просто проходит **все actions по priority подряд**:

```text
PLACE_STOP -> FAILED
CANCEL_ORDER -> всё равно выполняется
```

Ни `FAILED`, ни `SUBMITTED` предыдущей команды не останавливают зависимую следующую команду.

Сценарий абсолютно практический:

```text
Follower имеет SL 64 000
Master переносит SL → 64 500

PLACE_STOP 64 500
    ↓
биржа отвечает -2019 / -4014 / другая ошибка
    ↓
Executor получает FAILED
    ↓
но продолжает план
    ↓
CANCEL old 64 000
    ↓
Follower остаётся БЕЗ STOP
```

Вот это для copy-trading я считаю **P0**.

### Что требуется

Не надо строить DAG execution engine.

Достаточно простого правила:

**если PLACE нового protective ордера не CONFIRMED — зависимый CANCEL старого protective не исполнять.**

И тест:

```text
existing stop A
target stop B
PLACE B -> FAILED
assert A остался NEW
assert cancel(A) не был вызван
```

То же разумно применить к критическим последовательностям внутри плана.

---

## 🔴 2. Новая idempotency всё ещё ломает настоящий state reconciliation — теперь в обратную сторону

Это более тонкая проблема.

Сейчас revision повышается, только если изменился **desired TargetPosition**. Если target тот же — revision остаётся старой.

Executor же исторический `CONFIRMED` command считает окончательно выполненным и второй раз не отправляет.

Пример:

```text
target stop = 64 000

движок:
PLACE_STOP 64 000 -> CONFIRMED

потом Stop follower исчез:
- пользователь отменил руками
- exchange его expired/cancelled
- состояние разошлось по другой причине

reconcile:

actual stop = None
target stop = 64 000

Planner правильно говорит:
PLACE_STOP 64 000

НО target не изменился
→ revision старая
→ payload тот же
→ command_key тот же
→ Executor находит старый CONFIRMED
→ ничего не отправляет
```

**Follower может навсегда остаться без Stop, хотя actual != target.**

И это касается не только SL.

Например follower вручную закрыл позицию, master всё ещё в ней. Planner может захотеть восстановить прежний target той же командой, а историческая idempotency способна её проглотить.

Это фундаментально противоречит архитектурному принципу проекта:

> actual state + target state → снова привести actual к target.

### Как чинить

Не надо отменять revision design — он хороший.

Нужно различать:

**retry уже выполняемой операции**

и

**новую reconciliation operation потому, что authoritative actual state снова разошёлся с тем же target.**

То есть после подтверждённой предыдущей попытки, если свежий exchange snapshot снова требует того же action, должна возникнуть новая execution generation/revision.

Обязательный тест:

```text
PLACE_STOP 64000 -> confirmed
удалить Stop из FakeAster
reconcile с неизменным master target
assert новый PLACE_STOP действительно отправлен
```

Это второй **P0**.

---

## 🔴 3. HEDGE REST recovery фактически снова сломан из-за банального duplicate function

Это прям конкретный regression в файле.

В `master/projector.py` сначала появилась правильная новая `_pick_protective()`:

```python
o.reduce_only
or o.close_position
or _closes_leg(o)
```

Именно это чинит HEDGE-ордера вашего manual script, которые имеют `positionSide=LONG/SHORT`, но могут не иметь `reduceOnly/closePosition`.

А **ниже файла `_pick_protective()` объявлена второй раз** со старой реализацией:

```python
o.reduce_only or o.close_position
```

В Python второе определение просто перезаписывает первое.

То есть пункт P12 в отчёте:

> HEDGE-защита в rebuild исправлена

**фактически не исправлен**.

Почему 232 теста зелёные — понятно: REST rebuild unit test использует `BOTH` и `reduce_only=True`, поэтому этот случай проходит.

Исправление элементарное:

* удалить старое второе определение;
* добавить rebuild test для `LONG + SELL STOP + reduceOnly=False + closePosition=False`.

Это маленький фикс, но для текущего HEDGE live-режима обязательный.

---

## 🔴/🟠 4. Stop-in-profit исправлен логически, но `mark_price` в реальном WS state может быть старым

Само изменение правильное:

```python
long: stop < current mark
short: stop > current mark
```

вместо проверки относительно historical entry.

Но `MasterProjector` не поддерживает постоянно актуальный mark price. После fill значение может остаться около цены открытия до очередного REST `rebuild_full`. `ACCOUNT_UPDATE` специально informational и mark там не обновляет.

А App делает forced master resnapshot только раз в 10 sweep:

```text
15 sec default × 10 = примерно до 150 секунд
```

Новый acceptance test эту проблему случайно скрывает:

```python
projector.state.positions[key].mark_price = 66500
```

То есть **сам вручную делает mark актуальным**, а потом проверяет stop 66200.

Реальный сценарий:

```text
entry 65 000
рынок ушёл 67 000
projector.mark всё ещё ~65 000
master переносит stop → 66 000
```

и код опять может посчитать Stop «неправильным».

Для маленького проекта тут тоже не требуется отдельный market-data subsystem. Достаточно иметь один действительно свежий mark для affected symbol перед sanity/risk decision либо перестроить validation так, чтобы не полагаться на давно сохранённый mark.

---

## 🟠 5. Liquidation / ADL / autoclose из прошлого acceptance gate по-прежнему не закрыты

Это вообще потерялось при переносе прошлого ревью в таблицу «17 пунктов».

В исходном `review_stage1.md` я отдельно писал про liquidation/ADL и в acceptance gate прямо был:

> `liquidation/autoclose path`.

Сейчас Projector всё ещё меняет позицию только на:

```python
execution_type == "TRADE"
```

а `MARGIN_CALL` лишь ставит `degraded`.

Периодический REST rebuild через какое-то время это вылечит, но для сценария:

> мастер принудительно ликвидирован / auto-close → follower продолжает держать позицию

150 секунд — плохой recovery.

Не обязательно моделировать каждую экзотику Aster. Минимальный вариант Stage 1:

**MARGIN_CALL / неизвестный position-changing execution → немедленный authoritative master resnapshot + reconciliation.**

---

# Ещё три вещи, но я не превращал бы их в новый многонедельный этап

### Nonce

`NonceStore` теперь действительно commit делает до return — это правильное исправление прошлого P0.

Но allocation состоит из отдельных:

```text
SELECT last_nonce
...
UPDATE
COMMIT
```

и не является атомарным. Две concurrent coroutine/process с одним signer теоретически могут прочитать одинаковый `last_nonce` и получить одинаковый candidate.

Тест сейчас только последовательный + restart, concurrency не проверяет.

Для вашего проекта решение маленькое: atomic SQLite allocation/transaction. Никакой Redis здесь не нужен.

### ONE_WAY

Это уже не вопрос архитектуры — нужен просто живой прогон.

`.env.example` по умолчанию стоит:

`CT_POSITION_MODE=ONE_WAY`,

а отчёт прямо признаёт, что полноценная ONE_WAY live campaign не проводилась.

Причём `ENGINE_TEST_STATUS` сам пишет:

> до приёмки Stage 1 остаётся builder-e2e и ONE_WAY live-кампания.

Я бы **ONE_WAY прогнал обязательно**. Это не overengineering, это один основной режим продукта.

И ещё остался маленький старый mismatch: `entry_quantity()` margin check использует `policy.max_leverage`, хотя Coordinator реально может выставить `min(master_leverage, policy.max_leverage)`. Master 2x + policy 20x → sizing проверяется как будто доступно 20x, а потом реально выставляется 2x.

### 30 пользователей

После рефакторинга стало существенно лучше: один `openOrders`, WS больше не ждёт follower REST. Это правильные два фикса.

Но snapshot всё равно приблизительно:

```text
balance        5
openOrders    40
positionRisk   5
----------------
≈50 weight / follower
```

и каждый master delta запускает snapshot **всех followers**. Reconciler тоже делает full sweep.

Для 30:

```text
один event ≈ 1500 weight
open + следом Stop ≈ 3000
```

при вашем configured 2400/min.

Rate limiter спасёт от нарушения лимита, но ценой задержек.

Поэтому:

**для 3–10 тестовых пользователей — нормально.
Для уверенного запуска сразу на 30 — symbol-scoped/dirty snapshot я бы всё-таки сделал.**

Но это **не блокер именно архитектуры Stage 1** и точно не причина городить новую инфраструктуру.

---

## Что с Builder

Здесь документы проекта сами друг другу немного противоречат.

Stage-1 archive / DEV plan включают Builder signed e2e в criterion, а новая архитектура логически относит настоящий Builder onboarding к Stage 3.

С учётом реальной цели проекта **я бы Builder сейчас НЕ держал блокером торгового ядра Stage 1**.

Просто поправил бы документацию:

> Stage 1 trading engine accepted без Builder; Builder acceptance → Stage 3.

Это как раз разумное упрощение под одного разработчика.

---

# Оценка после рефакторинга

| Область                    |   Было |                                Сейчас |
| -------------------------- | -----: | ------------------------------------: |
| Архитектура                | 8.5/10 |                            **8.5/10** |
| Core implementation        | 7–8/10 |                            **8.5/10** |
| Risk engine / sizing       |  ~7/10 |                            **8.5/10** |
| Planner                    |  ~7/10 |                              **9/10** |
| Durable execution/recovery |   4/10 |                            **7.5/10** |
| Master projector           |  ~5/10 |                            **7.5/10** |
| Test breadth               |   8/10 |                              **9/10** |
| Adversarial test quality   | 5.5/10 |                              **8/10** |
| HEDGE happy path           |   8/10 |     **8.5/10**, но rebuild regression |
| ONE_WAY confidence         | низкая | **7/10 code, live proof отсутствует** |
| Готовность 3–10 тестеров   |      — |            **высокая после P0 fixes** |
| Готовность 30              |      — |        **нужна оптимизация snapshot** |
| Готовность Stage 1 сегодня |      ❌ |            **❌, но уже очень близко** |

Я отдельно попытался проверить CI/status head-коммита. GitHub App не даёт доступ к combined status (`403`), а workflow runs на `e165d62` отсутствуют, поэтому **232 passed / 8 signed passed я могу подтвердить как содержимое отчёта и тестового кода, но не как независимо просмотренный CI run**.

## Что я бы дал модели как последний Stage-1 fix gate

Не ещё 17 пунктов. **Шесть конкретных вещей:**

1. Executor: `PLACE new protection` не CONFIRMED → **не CANCEL old**.
2. Idempotency: unchanged target + fresh actual drift должен **создавать новую reconciliation command**, а не упираться в старый CONFIRMED.
3. Удалить вторую `_pick_protective`; добавить настоящий HEDGE REST-rebuild test без reduceOnly.
4. Сделать stop/deviation decision на действительно свежем market price.
5. MARGIN_CALL / liquidation/autoclose → immediate REST resnapshot + reconcile.
6. Прогнать полноценную signed **ONE_WAY** кампанию; заодно исправить margin check на фактически выбранном leverage.

И отдельно, уже как подготовку к первым ~30 пользователям: symbol-scoped follower snapshot.

**После этих шести я, скорее всего, Stage 1 приму.** Новой архитектурной итерации здесь не нужно. Сам рефакторинг `7fa813e + 3b48e51 + e165d62` двигался в правильную сторону; проблема сейчас уже не «ядро спроектировано неправильно», а несколько конкретных edge cases вокруг safety и reconciliation.

