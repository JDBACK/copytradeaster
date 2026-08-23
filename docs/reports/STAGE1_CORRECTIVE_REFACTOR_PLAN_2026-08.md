# Corrective-план доработки Stage 1

Дата подготовки: 2026-08-23
Статус: **выполнен полностью; Stage 1 = CLOSED**
Ветка-аудит: `codex/stage1-core`, baseline `18224d5`
Область: торговое ядро AsterDex futures testnet для одного master и сначала
10–30, максимум около 100 followers.

## 1. Цель и решение по статусу

Цель этого pass — закрыть конкретные финансовые и эксплуатационные инварианты,
найденные независимым аудитом после предыдущего acceptance review, и получить
новый воспроизводимый набор доказательств на testnet.

Gate выполнен 2026-08-23: Stage 1 помечен `CLOSED`, переход к Stage 2 разрешён.
Архитектура осталась modular monolith; mainnet и реальные средства в ходе
corrective pass не использовались и этим verdict не разрешены.

Актуальные счётчики suite и фактический статус прогонов по-прежнему ведутся
только в [ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md).
Этот документ задаёт будущий scope и критерии, но не дублирует живые счётчики.

## 2. Ограничения и non-goals

Проект остаётся небольшим modular monolith:

- один Python/asyncio process;
- SQLite + WAL;
- один master;
- 10–30 followers на первом рабочем запуске;
- до 100 followers после отдельного scale-gate;
- REST reconciliation для followers, один user-data WebSocket мастера.

В этот corrective pass **не входят**:

- Redis, PostgreSQL, Kafka, RabbitMQ, Celery, Kubernetes и микросервисы;
- Telegram/onboarding Stage 2;
- Builder/Aster Code Stage 3;
- зеркалирование неисполненных лимитных входов мастера;
- follower WebSocket pool;
- автоматизация mainnet-развёртывания.

## 3. Подтверждённые проблемы baseline

### CR-1 — P0: защита не учитывает размер позиции

`OrderDTO` получает с биржи `origQty`, но `ProtectiveOrder` его не хранит.
Planner сравнивает Stop/TP только по trigger level. При неизменном уровне и
изменившейся позиции он оставляет старый conditional order без замены.

Подтверждённое воспроизведение baseline:

```text
position 0.02 + Stop qty 0.02
target position 0.05, Stop level тот же
planner -> только MARKET_OPEN 0.03
Stop trigger -> закрыто 0.02, остаток позиции 0.03
```

Следствие: добавленная часть позиции остаётся без полного SL/TP. После
сокращения HEDGE-позиции старый fixed-quantity conditional может быть больше
остатка и быть отклонён/истечь при trigger.

### CR-2 — P1: `emergency-close-all` не является устойчивой аварийной операцией

Baseline не включает глобальный pause в emergency-flow. Если master остаётся в
позиции, следующий reconcile может открыть follower повторно.

Кроме того, повторное emergency-закрытие одинаковой позиции получает тот же
`command_key` (`trade_id=None`, `revision=0`, payload тот же). Исторический
`CONFIRMED` проглатывает новую операцию.

Подтверждённое воспроизведение baseline:

```text
первый emergency: 0.05 -> 0, отправлен один order
позиция снова открыта: 0.05
второй emergency: Executor вернул CONFIRMED, новый order не отправлен
позиция осталась 0.05
```

### CR-3 — P1: boot допускает READY с неразрешённым финансовым исходом

Если `query_order` во время `resolve_boot_unknowns()` временно падает, строка
остаётся `SENDING/SUBMITTED/UNKNOWN`. `App.start()` не проверяет остаток
in-flight rows и продолжает startup reconciliation. `SENDING/UNKNOWN` затем
может быть отправлен повторно с новым client order id.

### CR-4 — P2: `ready` и `degraded` не отражают живое состояние процесса

`ready=true` сбрасывается только при graceful stop. После kill/crash процесс
может отсутствовать, а CLI продолжает показывать READY. `degraded` хранит
последнюю историческую ошибку без lifecycle/acknowledge.

### CR-5 — тестовые doubles закрепляют часть неверной семантики

`FakeAster` и `ShadowExchange` недостаточно строго моделируют:

- fixed quantity conditional после изменения позиции;
- `reduceOnly` и HEDGE close semantics при qty больше остатка;
- `closePosition=true`;
- trigger и expiration конкурирующих защит.

Из-за этого load/e2e может быть зелёным при неверной полноте защиты.

## 4. Инварианты после рефакторинга

Новый gate принимается только при выполнении всех инвариантов.

| ID | Инвариант |
|---|---|
| INV-01 | Каждая non-zero follower leg имеет ровно один bot-owned Stop, закрывающий **весь фактический остаток** позиции |
| INV-02 | При наличии target TP ровно один bot-owned TP также закрывает весь фактический остаток |
| INV-03 | Изменение qty при прежнем trigger level не считается converged, если тип защиты использует fixed quantity |
| INV-04 | Замена защиты всегда `PLACE new CONFIRMED -> CANCEL old`; ошибка PLACE не удаляет старую защиту |
| INV-05 | После успешного MARKET fill выполняется authoritative post-fill snapshot и защита сверяется с фактическим fill, а не только с плановым qty |
| INV-06 | Если новая/увеличенная позиция не получила полный Stop, движок немедленно сокращает непокрытую часть либо закрывает новую ногу |
| INV-07 | Planner/Coordinator управляют только bot-owned conditional orders и не отменяют ручные чужие ордера |
| INV-08 | Emergency сначала запрещает entries, затем приводит всех followers к flat и подтверждает это REST snapshot |
| INV-09 | Две разные emergency-операции никогда не делят один execution generation, даже при одинаковом payload |
| INV-10 | Пока существует неразрешённый `SENDING/SUBMITTED/UNKNOWN`, приложение не публикует READY и не отправляет новые mutation commands |
| INV-11 | `status ready=true` означает живой процесс со свежим heartbeat, а не старое значение в SQLite |
| INV-12 | После повторного reconcile при `actual == target` не создаётся новых команд |
| INV-13 | Ошибка одного follower не останавливает остальных и не разрешает fail-open entry |
| INV-14 | После cleanup все testnet accounts flat, open orders = 0, in-flight journal = 0 |

## 5. Work package 1 — полная семантика Stop/TP

### 5.1 Модель и snapshot

Изменить:

- `domain/models.py::ProtectiveOrder` — добавить `orig_qty: Decimal`;
- при необходимости добавить явный признак `bot_owned` или вычислять ownership
  централизованной функцией;
- `coordinator.py::_snapshot()` — переносить `OrderDTO.orig_qty` в доменную
  модель;
- `master/projector.py::_pick_protective()` — также сохранять quantity, чтобы
  rebuild не терял контракт биржи;
- `FakeOrder`, `_ShadowOrder` и DTO builders — поддержать ту же модель.

Ownership для Stage 1:

```text
bot-owned = clientOrderId создан нашим executor (`ct...`)
```

`order_links` остаётся доказательной связью эпизода, но snapshot не должен
делать по одному SQL-запросу на каждый open order. Ручные follower conditionals
не участвуют в bot convergence и никогда автоматически не отменяются.

### 5.2 Целевая семантика market-protection

Предпочтительный follower contract:

- `STOP_MARKET` / `TAKE_PROFIT_MARKET` — `closePosition=true`, без `quantity`
  и без `reduceOnly`;
- `positionSide` передаётся только для HEDGE;
- `STOP` / `TAKE_PROFIT` — fixed `quantity=abs(actual follower position)`, так
  как limit-flavor не поддерживает `closePosition=true`.

Перед закреплением этого решения обязателен небольшой signed contract test на
testnet для ONE_WAY и HEDGE. Если Aster testnet отвергает `closePosition` в
каком-либо режиме, fallback — единая quantity-aware модель для всех четырёх
типов. В любом варианте INV-01/INV-02 обязательны.

### 5.3 Planner convergence

Для fixed-quantity protection сравнивать:

- order type;
- trigger level по tick grid;
- side;
- positionSide/close mode;
- workingType;
- `orig_qty` против `abs(actual position qty)` по step grid.

Несовпадение любого обязательного поля означает replacement. Даже если цена
та же, planner строит:

```text
PLACE new full-size protection
  -> только после CONFIRMED
CANCEL old protection
```

При `closePosition=true` qty mismatch отсутствует по определению, но остальные
поля продолжают участвовать в convergence.

### 5.4 Post-fill safety

После `MARKET_OPEN`/`MARKET_CLOSE`:

1. дождаться окончания mutation generation;
2. выполнить authoritative follower `positionRisk + openOrders`;
3. построить protection target от фактической позиции;
4. заменить защиту при любом расхождении;
5. повторить snapshot и подтвердить INV-01/INV-02.

Если после нового entry/increase полный Stop не подтверждён:

- initial entry закрывается полностью;
- increase сокращается как минимум до последнего подтверждённо защищённого qty;
- ошибка фиксируется в journal/degraded;
- TP failure не закрывает позицию, если полный Stop остаётся подтверждённым;
  TP ретраится reconciliation.

Для этого не нужен универсальный DAG executor. Достаточно отдельного
координаторного safety-step вокруг уже существующих per-account locks и
post-fill recompute.

## 6. Work package 2 — надёжный emergency-flow

### 6.1 Последовательность

`emergency-close-all` выполняет:

```text
persist emergency_active=true
persist allow_new_entries=false
authoritative snapshot всех followers
cancel все bot-owned Stop/TP, включая extras/orphans
close полный фактический остаток каждой leg
authoritative verification snapshot
повторить bounded pass при остаточном drift
оставить allow_new_entries=false после успеха
```

Ошибка одного follower не прекращает cleanup остальных, но итоговый exit code
ненулевой, пока хотя бы один follower non-flat либо имеет bot-owned orders.

### 6.2 Новая execution generation

Добавить `operation_id` в доменный контекст `ExecutionAction`/`ExecutionPlan`
либо эквивалентный persisted emergency epoch. Он участвует в `command_key`, но
не отправляется в Aster payload.

Требования:

- одна emergency invocation переиспользует один operation id для retry;
- следующая invocation получает новый id;
- restart после частично выполненного emergency может продолжить существующую
  operation либо безопасно начать новую после authoritative snapshot;
- одинаковый qty во втором эпизоде не переиспользует исторический CONFIRMED.

### 6.3 Конкурентность с работающим engine

Coordinator проверяет `emergency_active` вместе с pause flag и не выпускает
новые entries/increases. Уже начатая до pause mutation может завершиться, поэтому
emergency verification делает не менее двух последовательных flat snapshots
или bounded повтор до устойчивого flat.

Resume — отдельная явная команда после проверки оператором. Emergency никогда
не включает entries автоматически.

## 7. Work package 3 — boot recovery fail-closed

### 7.1 Новый результат resolver

`resolve_boot_unknowns()` возвращает структурированный отчёт:

```text
confirmed
failed_not_found
unresolved_query_error
unresolved_without_coid
```

Алгоритм для каждого in-flight command:

1. bounded query retry с небольшим backoff;
2. found — классифицировать реальный exchange status, записать link;
3. authoritative not-found — `FAILED`, reconciliation создаст новую generation;
4. query недоступен — оставить unresolved и блокировать boot;
5. для MARKET дополнительно разрешается authoritative position check, но только
   если эффект команды можно однозначно доказать по сохранённому pre-state.

### 7.2 READY gate

`App.start()` после resolver повторно читает `CommandRepo.in_flight()`.

Если список не пуст:

- `ready=false`;
- `degraded=boot-unresolved:<count>`;
- follower mutations и startup reconcile не запускаются;
- процесс либо завершает start с понятной ошибкой, либо остаётся в явном
  `RECOVERY_REQUIRED` без торговых writes.

Executor отдельно запрещает resubmit существующего `SENDING/UNKNOWN`, пока не
получен успешный resolution result.

### 7.3 Очистка baseline-БД

Перед новым acceptance run старая in-flight строка в рабочей БД разрешается
через штатный новый resolver. Историю FAILED не удалять: она остаётся журналом
фактов. Ручное SQL-редактирование статусов не считается доказательством.

## 8. Work package 4 — runtime status и startup gap

### 8.1 Heartbeat/lease

В `app_state` достаточно хранить:

```text
runtime_id
started_at
heartbeat_at
ready
degraded_active
degraded_reason
```

Heartbeat обновляется существующим periodic loop без отдельной инфраструктуры.
CLI считает runtime живым, только если `heartbeat_at` свежее согласованного
порога, например `max(2 * reconcile_interval, 30 секунд)`.

На старте первым write устанавливается `ready=false`; после stale heartbeat
CLI показывает `ready=false/stale`, даже если процесс завершился через kill -9.

Исторические ошибки уже остаются в `execution_commands`. Активный degraded
можно очищать после успешного full reconcile при нулевом in-flight и нулевых
ошибках followers либо отдельной `clear-degraded` после операторской проверки.

### 8.2 Закрытие startup gap

Текущий первый REST snapshot выполняется до подключения user stream. После
первого успешного WebSocket handshake, но до READY, выполнить ещё один master
REST snapshot + follower reconcile. Это закрывает окно изменения master между
первым snapshot и подпиской.

### 8.3 Migration hardening

При snapshot открытого legacy episode с `copy_trades.direction=0` записать
фактический sign позиции. Это устраняет бессрочный legacy default после
миграции #13.

## 9. Work package 5 — конфигурация и операционные guards

Добавить Pydantic validation:

- `position_mode in {ONE_WAY,HEDGE}`;
- `working_type in {MARK_PRICE,CONTRACT_PRICE}`;
- leverage, интервалы и rate limits строго положительны;
- risk percent и entry deviation находятся в согласованном безопасном диапазоне;
- whitelist непустой, symbols нормализованы в uppercase;
- Stage 1 по умолчанию отказывается стартовать на URL без `testnet`, если нет
  отдельного явного `CT_ALLOW_MAINNET=true` будущего этапа.

Исправить `dry_run` contract:

- read-only REST snapshots продолжают выполняться;
- только mutation endpoints не отправляются;
- либо `dry_run` официально заменяется SHADOW mode и удаляется из runtime App.

Обновить `.env.example` параметрами SHADOW/snapshot profile и пояснить, что для
10–30 followers production рекомендуется symbol-scoped snapshot по whitelist.

## 10. Work package 6 — строгие FakeAster/ShadowExchange

Тестовые adapters должны моделировать хотя бы те exchange rules, на которых
держатся финансовые assertions:

- ONE_WAY `reduceOnly` не может увеличить/перевернуть позицию;
- HEDGE close действует только на указанную leg;
- fixed conditional выполняет не больше своего `orig_qty`;
- oversized HEDGE close получает exchange-like reject/expire, а не молча flip;
- `closePosition=true` закрывает весь текущий остаток на момент trigger;
- conditional без позиции не создаёт фиктивную противоположную позицию;
- при flat остальные reduce-only/close-position conditionals expire;
- partial MARKET fills можно включать детерминированным test hook;
- query visibility delay и transport unknown настраиваются отдельно.

Shadow load не считается exchange contract evidence; он подтверждает fan-out,
journal, SQLite и отсутствие блокировок.

## 11. Обязательный offline test matrix

### 11.1 Domain/planner unit

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| PROT-U01 | ONE_WAY grow, Stop level тот же, fixed qty старый | PLACE full-size Stop + dependent CANCEL old |
| PROT-U02 | ONE_WAY partial reduce, Stop level тот же | защита приведена к остатку либо остаётся closePosition |
| PROT-U03 | HEDGE LONG reduce | новый Stop закрывает ровно LONG remainder |
| PROT-U04 | HEDGE SHORT reduce | новый Stop закрывает ровно SHORT remainder |
| PROT-U05 | TP при grow/reduce | тот же full-size contract, что у Stop |
| PROT-U06 | Stop price тот же, type/workingType отличается | replacement обязателен |
| PROT-U07 | manual conditional + bot conditional | manual order не отменяется и не считается bot convergence |
| PROT-U08 | closePosition market protection | payload без quantity/reduceOnly, с корректным positionSide |
| PROT-U09 | limit-flavor protection | payload содержит grid-aligned price и полный actual qty |
| PROT-U10 | actual == target по всем полям | пустой plan |

### 11.2 Executor/coordinator integration

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| PROT-I01 | 0.02 + Stop 0.02 -> target 0.05 -> trigger | follower становится ровно flat |
| PROT-I02 | partial fill MARKET_OPEN | post-fill snapshot ставит защиту по фактическому, не плановому qty |
| PROT-I03 | new Stop FAILED при старом Stop | старый Stop остаётся NEW, cancel не вызван |
| PROT-I04 | initial Stop FAILED после entry | entry компенсирующе закрыт, follower flat |
| PROT-I05 | grow Stop FAILED | непокрытая добавка закрыта, исходный защищённый qty не превышен |
| PROT-I06 | TP FAILED, Stop подтверждён | позиция остаётся со Stop, TP ретраится |
| PROT-I07 | duplicate fixed-size protections | остаётся один актуальный full-size order |
| PROT-I08 | restart между PLACE new и CANCEL old | следующий reconcile конвергирует без naked window |

### 11.3 Emergency/recovery/App

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| EMG-I01 | emergency при работающем engine и открытом master | pause установлен до close, re-entry нет |
| EMG-I02 | reopen того же qty -> второй emergency | новый exchange order, новый operation id, итог flat |
| EMG-I03 | один follower close FAILED | остальные закрыты, exit non-zero, повтор добивает остаток |
| EMG-I04 | extras/orphans без позиции | все bot-owned conditionals отменены |
| BOOT-I01 | SENDING found на exchange | CONFIRMED + link, duplicate send = 0 |
| BOOT-I02 | SENDING authoritative not-found | FAILED, затем новая reconciliation generation |
| BOOT-I03 | query_order временно недоступен | boot не READY, place_order = 0 |
| BOOT-I04 | resolver восстановился после retry | только после resolution разрешён reconcile |
| APP-I01 | kill без graceful stop | heartbeat stale, CLI показывает ready=false/stale |
| APP-I02 | master изменился между snapshot и WS ready | второй pre-READY snapshot видит изменение |
| APP-I03 | legacy direction=0 | snapshot backfill записывает фактический sign |

### 11.4 Регресс существующих инвариантов

Повторно обязательны существующие сценарии:

- dust zero-target и отсутствие re-entry;
- plain flip и dust-flat -> flip;
- FAILED dependent cancel convergence;
- unchanged target + manual drift создаёт новую generation;
- out-of-order TRADE и same-ms fills;
- MARGIN_CALL/autoclose critical rebuild;
- pause разрешает closes, но запрещает entries/increases;
- nonce concurrency;
- multi-symbol/per-leg trade attribution;
- повторный reconcile converged state создаёт ноль команд.

## 12. Обязательные реальные signed testnet tests

Все сценарии используют только `fapi.asterdex-testnet.com`, минимальные faucet
позиции и `try/finally` cleanup. Сценарий не может быть помечен passed, если
обязательная фаза была пропущена условным `if`.

### REAL-00 — preflight и safety boundary

Перед каждым signed run:

1. проверить, что REST/WS URL содержат `testnet`;
2. signed GET `positionRisk/openOrders/position mode` для master и REAL followers;
3. cleanup прежних bot-owned orders и позиций;
4. подтвердить flat/no-orders;
5. записать исходные position modes для восстановления в `finally`;
6. запретить запуск, если найден неизвестный account mapping или общий user
   address master/follower.

### REAL-01 — прямой contract `closePosition`

Отдельно для ONE_WAY и HEDGE:

- открыть минимальную позицию;
- поставить `STOP_MARKET closePosition=true` без quantity/reduceOnly;
- query/openOrders подтверждают `closePosition=true` и правильную leg;
- отменить Stop и закрыть позицию;
- повторить для `TAKE_PROFIT_MARKET`.

Если любой режим не принимает contract, зафиксировать error code и применить
quantity-aware fallback до продолжения core E2E.

### REAL-02 — ONE_WAY protection-size E2E через ядро

Фазы:

1. master LONG + Stop -> follower entry + full Stop;
2. вручную сократить follower ниже risk cap;
3. master увеличить LONG при прежнем Stop -> core доращивает follower;
4. проверить, что защита закрывает весь фактический follower qty;
5. master partial reduce -> follower reduce;
6. повторно проверить full-remainder protection;
7. поставить/перенести TP и проверить ту же семантику;
8. один раз реально дождаться trigger Stop или TP;
9. follower и master flat, повторный reconcile = 0 новых команд.

Trigger-фаза обязательна. Если рынок не коснулся валидного близкого уровня за
операционный timeout, run фиксируется `NOT COMPLETED`, а не passed.

### REAL-03 — HEDGE LONG и SHORT protection-size E2E

Для каждой leg отдельно:

- entry;
- Stop/TP;
- grow при прежнем уровне;
- partial reduce;
- protection query после каждой size transition;
- trigger/close;
- убедиться, что противоположная leg не менялась;
- финал flat/no-orders.

Минимум одна из HEDGE legs должна использовать limit-flavor `STOP` или
`TAKE_PROFIT`, чтобы подтвердить fixed-quantity replacement на бирже.

### REAL-04 — emergency два раза

1. запустить настоящий engine и открыть master/follower позицию;
2. выполнить `emergency-close-all` без предварительного ручного pause;
3. подтвердить persisted pause, flat и отсутствие bot-owned orders;
4. выдержать не менее двух reconcile intervals — re-entry отсутствует;
5. явно resume, открыть тот же symbol и максимально близкий прежнему qty;
6. повторить emergency;
7. журнал содержит две разные operation generations и два реальных close;
8. финал flat/no-orders, pause остаётся активным.

### REAL-05 — boot recovery found/not-found

- `found`: создать testnet conditional с известным coid, зафиксировать
  соответствующую SENDING/SUBMITTED строку, запустить resolver; результат
  CONFIRMED + order_link, повторной установки нет;
- `not-found`: создать journal row для заведомо отсутствующего coid; resolver
  переводит её в FAILED, затем reconciliation создаёт новую generation только
  при реальном actual/target drift;
- query-error fail-closed проверяется hermetic transport fault test, потому что
  намеренно ломать testnet API недетерминированно.

### REAL-06 — настоящий App/WS lifecycle

- `App.start()` доходит до READY только после stream handshake и второго
  authoritative snapshot;
- master action проходит через реальный user-data WS, а не синтетический
  `projector.apply_raw`;
- принудительно закрыть socket/listenKey, дождаться reconnect + snapshot;
- позиции/защита не дублируются;
- graceful stop даёт `ready=false`;
- отдельный kill-run подтверждает stale-heartbeat detection.

### REAL-07 — critical authoritative recovery без реальной ликвидации

Реальную ликвидацию намеренно не вызываем. Вместо этого:

1. открыть master/follower на testnet;
2. напрямую закрыть master на бирже;
3. подать captured/synthetic critical `CALCULATED/autoclose` signal;
4. callback обязан сделать реальный REST rebuild;
5. follower закрывается по authoritative master flat;
6. journal и REST audit подтверждают отсутствие остатка.

### REAL-08 — финальный audit

После всего signed набора:

- master и все REAL followers возвращены в исходные position modes;
- non-zero positions = 0;
- open orders = 0;
- in-flight execution commands = 0;
- sticky zero targets = 0;
- `ready=false`, если engine остановлен;
- cleanup errors = 0.

## 13. Load и soak после рефакторинга

### LOAD-01 — быстрый 30-way hermetic

- 30 SHADOW followers;
- не только open/close, а цикл
  `open -> protection -> grow same level -> partial reduce -> protection check -> close`;
- не менее 100 циклов;
- все команды terminal CONFIRMED;
- DB locks, UNKNOWN, BLOCKED и ingest drops отсутствуют;
- после каждого цикла позиции и orders пусты.

### LOAD-02 — max-scale 100 SHADOW

- 100 SHADOW followers;
- минимум 20 расширенных циклов;
- production Coordinator/Planner/Executor/SQLite path;
- snapshot profile соответствует whitelist;
- отдельно записать p50/p95/max generation latency, SQLite write latency и
  размер очереди;
- порог принятия задаётся после первого измерения, но никакой цикл не должен
  нарушать финансовые инварианты или оставлять non-terminal journal rows.

Это подтверждает внутренний scale до заявленного максимума, но не подписанные
запросы 100 реальных Aster accounts.

### LOAD-03 — hybrid testnet 30 slots

- 3 имеющихся REAL followers + 27 SHADOW;
- минимум 20 расширенных циклов protection-resize;
- REAL accounts проходят signed REST/order path;
- SHADOW не имеют credentials и `shadow_signed_requests=0`;
- итоговый JSON + SQLite artifact;
- независимый signed REST audit после cleanup.

Требовать 30 REAL wallets для Stage 1 не нужно: для небольшого проекта это
дорогое и слабополезное усложнение. Exchange contract доказывают REAL-01..08,
fan-out — LOAD-01..03.

### SOAK-01 — двухчасовой testnet runtime

На живом `App`:

- master WS подключён непрерывно;
- несколько open/add/reduce/stop-move/tp-move/close циклов;
- один принудительный reconnect;
- один graceful restart;
- один kill/recovery run;
- периодические snapshots не создают duplicate protection;
- в конце no positions/no orders/no in-flight.

## 14. Стек после рефакторинга

Runtime stack **не расширяется**:

| Область | Стек после corrective pass |
|---|---|
| Runtime | Python 3.12, asyncio, один процесс |
| REST | httpx async |
| WebSocket | websockets, один master user-data stream |
| Подпись | eth-account, EIP-712, persisted SQLite nonce |
| Валидация | Pydantic v2 / pydantic-settings |
| Storage | SQLite WAL + aiosqlite, ordered migrations |
| Credentials | AES-GCM через cryptography, master key вне БД |
| Core | Projector -> Coordinator -> Risk -> Planner -> Journal -> Executor -> Reconciler |
| Followers | REST snapshot/reconciliation; без follower WS pool |
| Load | ShadowExchange только как non-certifying fan-out adapter |

Новые обязательные runtime-механизмы, но не новые сервисы:

- quantity/closePosition-aware protection state;
- emergency operation generation;
- fail-closed boot recovery report;
- SQLite heartbeat/lease;
- второй authoritative snapshot после WS handshake;
- testnet/mainnet startup guard.

Test/verification stack:

| Контур | Инструменты |
|---|---|
| Unit/integration | pytest, pytest-asyncio |
| HTTP contracts | respx + Aster testnet signed REST |
| Coverage | pytest-cov, gate не ниже текущего 87% |
| Static | ruff check, ruff format --check |
| Packaging | uv build (sdist + wheel) |
| Storage audit | sqlite3 read-only queries |
| WS proof | реальный Aster testnet user stream + replay fixtures |
| Load | FakeAster, строгий ShadowExchange, hybrid runner |
| Evidence | JSON run report, run SQLite, journal queries, final REST audit |

Новые Python dependencies для corrective pass не требуются.

## 15. Финальный набор команд подтверждения

Конкретные имена новых тестовых файлов могут уточняться при реализации, но
финальный gate должен сводиться к одному воспроизводимому набору.

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q -m "not testnet and not load" \
  --cov=src/copytradeaster --cov-report=term-missing --cov-fail-under=87
uv build
git diff --check
```

Signed testnet:

```bash
CT_TESTNET=1 .venv/bin/pytest -q tests/testnet -m testnet -vv
```

Load:

```bash
CT_LOAD=1 .venv/bin/pytest -q tests/integration/test_shadow_scale.py -vv
CT_TESTNET=1 CT_LOAD=1 .venv/bin/python scripts/hybrid_load_test.py \
  --followers 30 --cycles 20 --profile corrective
```

Если параметры `--profile/--followers/--cycles` ещё не поддерживаются runner,
они добавляются в рамках work package тестовой инфраструктуры.

После каждого signed/load run выполняется отдельный read-only audit, который
не использует assertions самого runner как единственный источник истины.

## 16. Evidence package для нового acceptance review

В review передаются:

1. diff по CR-1..CR-5 без несвязанных изменений;
2. обновлённый `ENGINE_TEST_STATUS_2026-08.md` с актуальными счётчиками;
3. новый run journal в `docs/reports/` с датами всех неуспешных и успешных
   попыток;
4. raw JSON и run SQLite в `data/` локально;
5. агрегаты journal statuses до cleanup;
6. независимый REST audit после cleanup;
7. доказательство двух разных emergency operation ids;
8. доказательство `query_error -> not READY -> zero new orders`;
9. proof protection size/closePosition после grow и reduce в ONE_WAY/HEDGE;
10. список известных ограничений без объявления их закрытыми.

Raw artifacts остаются git-ignored из-за размера/локальных данных. В отчёте
фиксируются checksum, имя файла и проверяемые агрегаты, чтобы результаты нельзя
было спутать между runs.

## 17. Порядок реализации

Выполнять последовательно:

1. строгая модель protection + Fake/Shadow semantics;
2. planner/coordinator protection convergence и post-fill compensation;
3. offline protection regressions;
4. emergency operation generation и pause/verification;
5. boot fail-closed;
6. heartbeat/startup gap/config guards;
7. полный offline gate;
8. REAL-01 contract decision (`closePosition` либо quantity fallback);
9. REAL-02..08 signed tests;
10. LOAD-01..03 и SOAK-01;
11. финальный audit/documentation;
12. новый независимый acceptance verdict.

Не смешивать corrective pass с функционалом Stage 2.

## 18. Definition of Done Stage 1

Stage 1 снова получает `CLOSED` только если одновременно:

- CR-1..CR-5 закрыты кодом и регрессами;
- INV-01..INV-14 доказаны;
- offline/static/build gates зелёные;
- REAL-00..08 завершены без пропущенных обязательных фаз;
- LOAD-01..03 и SOAK-01 завершены;
- final REST audit flat/no-orders;
- in-flight journal = 0;
- остановленный engine отображается как not ready;
- Builder явно остаётся Stage 3 и не маскируется skip-ом Stage 1;
- отдельный reviewer подтверждает отсутствие новых P0/P1.

После этого можно зафиксировать Stage 1 и перейти к Stage 2 без дополнительной
архитектурной перестройки.

## 19. Журнал выполнения

Журнал содержит только фактически завершённые действия и проверяемые команды.
Текущие suite counters по-прежнему обновляются исключительно в
`ENGINE_TEST_STATUS_2026-08.md` после завершения значимого рабочего пакета.

### 2026-08-23 — старт corrective pass, WP1 в работе

- повторно сверены baseline `18224d5`, рабочее дерево, corrective scope и
  реализация `domain/planner/coordinator/executor/FakeAster/ShadowExchange`;
- подтверждено, что follower snapshot терял `origQty`, а planner считал
  protection converged только по trigger level;
- `ProtectiveOrder` расширен фактическим `orig_qty`; REST/WS master projection
  и follower snapshot теперь переносят это поле;
- follower snapshot начал централизованно отделять bot-owned (`ct...`)
  conditionals от ручных ордеров; ручные conditionals не выбираются как Stop/TP
  и не попадают в automatic cancel convergence;
- planner переведён на `closePosition=true` для market Stop/TP и на
  quantity-aware convergence для limit-flavor `STOP/TAKE_PROFIT`;
- сравнение protection теперь включает type, side, trigger grid, workingType,
  close mode и fixed quantity;
- добавлены первые regression assertions для grow/reduce при прежнем уровне,
  mismatch workingType и HEDGE closePosition payload.

Промежуточное доказательство:

```text
.venv/bin/pytest -q tests/unit/trading/test_planner_transitions.py
результат первого прогона после изменения: 30 passed, 1 failed
причина remaining failure: старый тест создавал LONG TP с неверной BUY side;
строгая новая проверка side корректно потребовала replacement.
```

Этот failure не скрыт и не принят как регресс: fixture исправлен на реальную
SELL close-side; повторный прогон будет записан после завершения WP1.

### 2026-08-23 — WP1–WP6 реализованы

- market `STOP_MARKET`/`TAKE_PROFIT_MARKET` переведены на подтверждённый
  testnet-контракт `closePosition=true` без `quantity/reduceOnly`; для
  fixed-quantity `STOP/TAKE_PROFIT` planner сравнивает `origQty` с фактическим
  остатком ноги и выполняет `PLACE new -> CANCEL old`;
- signed contract показал особенность Aster: ответ/query нормализует
  `reduceOnly=true`, хотя корректный request его не содержит. Matcher поэтому
  проверяет request-contract и `closePosition`, но не принимает response-only
  нормализацию за mismatch;
- после MARKET fill coordinator синхронно делает authoritative snapshot и
  повторную convergence защиты; если новый entry/increase не получил Stop
  после немедленной повторной попытки, небезопасная нога закрывается полностью
  и runtime переводится в degraded;
- ручные follower conditionals исключены из bot convergence по зарезервированному
  префиксу `ct`; extras/orphans самого движка продолжают очищаться;
- emergency сначала сохраняет `emergency_active=true` и pause, получает новый
  UUID `operation_id`, выполняет до пяти bounded passes и требует два
  последовательных flat snapshots. Повторный emergency той же qty создаёт
  отдельную persisted generation;
- boot resolver возвращает структурированный отчёт, делает bounded query retry
  и оставляет query-error unresolved; `App.start()` fail-closed блокирует READY
  и любые follower mutations при остаточном in-flight;
- добавлены runtime UUID/heartbeat, stale READY interpretation, lifecycle
  активного degraded, второй authoritative snapshot внутри awaited WS
  connected-callback, legacy direction backfill и config/mainnet guards;
- FakeAster и ShadowExchange приведены к ONE_WAY/HEDGE reduce semantics,
  `closePosition`, fixed conditional quantity/expiration и детерминированному
  partial fill hook. Reject больше не оставляет фиктивный NEW order.

Промежуточные hermetic доказательства:

```text
planner targeted: 31 passed
strict Shadow adapter targeted: 5 passed
offline gate до последней test-isolation правки: 280 passed, 1 skipped,
11 deselected; coverage 88.21% >= 87%
```

### 2026-08-23 — REAL-01..08, включая обнаруженные testnet-особенности

- REAL-01 `closePosition` прошёл отдельно для ONE_WAY и HEDGE; query вернул
  `closePosition=true`, `origQty=0`, правильную leg и описанную выше
  response-нормализацию `reduceOnly=true`;
- REAL-02 полный ONE_WAY core E2E прошёл с обязательной trigger-фазой;
- REAL-03 fixed `STOP` на HEDGE LONG и SHORT доказал reduce/grow replacement
  при неизменном уровне;
- REAL-04 два emergency подряд прошли: две UUID operation generations, две
  реальные close-команды, после каждой позиции flat, pause остался активен;
- REAL-05 resolver нашёл реальный resting order, а заведомо отсутствующий coid
  перевёл в FAILED без повторной отправки;
- REAL-06/07 настоящий `App` достиг READY после WS-gated resnapshot, получил
  реальный master event, пережил закрытие listenKey/reconnect без дублей и
  выполнил critical authoritative recovery через реальный REST;
- первый полный объединённый signed run дал `14 passed / 1 failed / 1 skipped`:
  HEDGE SHORT не создал MARKET_OPEN. Journal доказал отсутствие exchange send.
  Причина была в тестовом harness: синтетический `MasterState` не открывал
  новый production `copy_trade` episode и мог наследовать persisted
  intentional-flat/own-protective latch из общей signed БД;
- REAL-03 исправлен не очисткой истории, а использованием штатного lifecycle
  `close active -> open new episode -> close test episode`. Связка
  ONE_WAY→HEDGE после исправления: `2 passed in 231.18s`;
- окончательный последовательный signed gate: `15 passed, 1 skipped in
  510.48s`. Единственный skip — Builder Stage 3.

Все signed сценарии использовали только
`https://fapi.asterdex-testnet.com`; mainnet и реальные средства не
использовались.

### 2026-08-23 — LOAD-01..03 и финальный offline/static/build milestone

- LOAD-01: 30 SHADOW followers × 100 расширенных fixed-protection циклов;
- LOAD-02: 100 SHADOW followers × 20 расширенных циклов production
  Coordinator/Planner/Executor/SQLite path;
- первый LOAD-03 (`hybrid-load-20260823T115350Z`) остановлен как FAILED:
  строгий Shadow HEDGE adapter ошибочно отвергал законный grow; результат не
  принят, adapter исправлен и покрыт unit-регрессией;
- короткий повтор LOAD-03 (`hybrid-load-20260823T115717Z`) подтвердил один
  чистый цикл: 210 CONFIRMED, все 31 account flat/no-orders;
- промежуточный 20-cycle run со Stop на расстоянии 500 был остановлен на
  цикле 16 после реального движения рынка к trigger; cleanup выполнен, run не
  принят как load evidence;
- финальный LOAD-03 с non-triggering расстоянием 3000:
  `hybrid-load-20260823T120948Z`, 20/20 циклов, 3 REAL + 27 SHADOW,
  3642/3642 CONFIRMED, non-terminal/UNKNOWN/retry/ingest-drop = 0,
  p50 35.118 s, p95 39.253 s, max 39.918 s; до и после cleanup все 31 account
  flat/no-orders, cleanup errors = 0;
- checksum финального LOAD-03:
  JSON `6c0f8fe461d159377215d454500266256d5a410197b1fc27f8a6b42234cebd9e`,
  SQLite `fc011bb8f30a380e9ca895e852cdc19d448c4269db1ac6862c6cc939db2ac62a`;
- финальный offline command после рефакторинга:
  `282 passed, 2 skipped, 16 deselected`, coverage `88.23% >= 87%`;
- `ruff check`, `ruff format --check`, `git diff --check` — clean;
- `uv build` успешно создал sdist и wheel. Сборщик выдал неблокирующее
  packaging-warning об отсутствии README; сам артефакт собран успешно.

Следующий незавершённый обязательный пункт: SOAK-01 и завершающий независимый
REST/SQLite audit. До них Stage 1 остаётся reopened.

### 2026-08-23 — SOAK-01 runner и smoke orchestration

- добавлен testnet-only `scripts/testnet_soak.py`; он не меняет runtime stack и
  использует production `App`, signed REST/WS и отдельную cloned run-БД;
- профиль: редкие расширенные циклы
  `open -> reduce -> grow -> stop move -> TP set/move -> close`, forced
  listenKey reconnect, graceful restart и child App с реальным `SIGKILL` между
  подтверждёнными flat-состояниями;
- первая 240-second smoke-попытка прошла оба торговых цикла/reconnect/restart,
  но остановилась на `child App did not become READY`. Foreground reproduction
  доказал причину в test orchestration: complex list env был передан как CSV,
  тогда как `pydantic-settings` декодирует его как JSON до validator. Run не
  принят; независимый REST preflight после ошибки подтвердил все четыре REAL
  account flat/no-orders;
- env исправлен на JSON `['BTCUSDT']`, failure path теперь гарантированно
  завершает child и поднимает cleanup App;
- повторный smoke (`testnet-soak-20260823T130803Z`): 2/2 цикла,
  73/73 CONFIRMED, in-flight 0, unknown/retry/ingest-drop 0, cleanup errors 0,
  до и после cleanup все четыре REAL account flat/no-orders;
- SIGKILL proof smoke: child READY с runtime id
  `526b62e571be4e889ea342053849d5f8`, return code `-9`, persisted READY остался
  true, heartbeat age достиг 33.820 s и был классифицирован как stale; новый
  App затем восстановился до READY;
- smoke имеет `accepted_two_hour_duration=false` и не засчитывается как
  SOAK-01. Следующий run запускается с `--duration 7200 --cycles 6`.

### 2026-08-23 — SOAK-01, финальный audit и acceptance verdict

- принятый run: `data/testnet-soak-20260823T131314Z.{json,sqlite3}`;
- фактическая длительность 7203.589 s при требовании 7200 s;
- 6/6 signed циклов
  `open -> reduce -> grow -> stop move -> TP set/move -> close`;
- cycle 1 встретил один transport outcome-unknown и выполнил один безопасный
  retry после query/position probes; duplicate effect и остатка не возникло;
  остальные пять циклов — retry/unknown/recovery 0;
- forced listenKey reconnect на 1946.529 s, graceful restart на 3249.411 s;
- child App достиг READY, получил runtime id
  `acb83b7e1b9a44f1a16d0c911836fc82`, был завершён реальным SIGKILL
  (`return_code=-9`), heartbeat стал stale через 33.709 s, затем новый App
  прошёл startup recovery и следующий signed cycle;
- 24 периодических независимых flat audits; journal до cleanup:
  218 CONFIRMED, другие statuses 0, in-flight 0, sticky zero 0,
  ingest drops 0;
- до и после cleanup `main` и `copyc1`–`copyc3` flat/no-orders,
  cleanup errors 0; остановленный run имеет `ready=false`, degraded false;
- независимый от runner signed REST preflight по рабочей БД повторно подтвердил
  все четыре account flat/no-orders и HEDGE mode; рабочая БД также имеет
  in-flight 0, sticky zero 0, `ready=false`;
- checksum SOAK-01:
  JSON `a6fbb57a1f06abb51843d5af30a35a8565c82b2e66da86be9cd08a4f5c59793d`,
  SQLite `42dfdd4b23bc5234cf503ba38d0c6f67b623f2ad9f6faca17d5aeac6eb5ad1aa`;
- добавлены узкие hermetic proofs INV-06 grow-failure fail-safe и APP-I02
  `rebuild -> reconcile -> stream-ready`; оба targeted запуска прошли;
- окончательный offline/static/package gate:
  `284 passed, 2 skipped, 16 deselected`, coverage 88.41% >= 87%, ruff/format/
  diff-check clean, sdist+wheel собраны без warning после добавления README.

Независимый финальный code review не нашёл новых P0/P1 в testnet scope.
Неблокирующие ограничения для маленького проекта: запускать ровно один App
process и проверять единый position mode preflight-ом. Добавлять Redis,
распределённый lock или отдельный orchestrator для одного трейдера не нужно.

Итог: CR-1..CR-5, INV-01..INV-14, REAL-00..08, LOAD-01..03 и SOAK-01
закрыты доказательствами. **Stage 1 = CLOSED; push разрешён для testnet core,
можно переходить к Stage 2.** Builder остаётся Stage 3, mainnet не разрешён.
