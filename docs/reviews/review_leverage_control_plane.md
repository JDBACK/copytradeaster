# Ревью leverage control-plane (2026-08-25)

Дословные тексты внешнего ревью цепочки `86a51ec → 6f01917 → 54279d8 → 69fa148`.
Никогда не редактировать и не перефразировать.

---

## Проход 1: четыре находки к `6f01917`

Проверил **сам код в репозитории**, а не отчёт coding-модели. Ветка действительно сейчас на `6f01917`, поверх основного leverage-коммита `86a51ec`.

В целом реализация получилась существенно лучше исходной. P0 dependency `SET_LEVERAGE → MARKET_OPEN`, сохранение leverage у flat-symbol, отсутствие leverage REST на обычном hot path, onboarding/resume wiring и реальные signed live-тесты — всё это действительно есть в коде. Live-набор не фиктивный: там реально проверяются `-4028`, worker provisioning, блокировка входа и реальный downshift.

Но **я бы пока не принимал этот кусок окончательно**. Нашёл 4 проблемы, причём две довольно серьёзные.

* **P0/P1 — неправильная обработка UNKNOWN у `POST /leverage`.** `AsterClient` специально превращает timeout/transport в `-1007/-1006`, то есть результат записи может быть неизвестен. Но `LeverageManager._prepare_locked()` ловит вообще **любой Exception** после первого POST и сразу делает `GET bracket → второй POST`. Это неправильно. Например, POST `100x` реально сработал, ответ потерялся, а follower bracket говорит `200x` — менеджер после timeout может отправить второй POST уже на `200x`. Fallback допустим только после **детерминированного reject** вроде `-4028`. Для `-1006/-1007/503/504` сначала надо читать `positionRisk` и определять фактическое состояние, без слепой второй записи.

  Здесь же есть crash/restart проблема. Executor перед SET пишет `SENDING`, но у `SET_LEVERAGE` нет `clientOrderId`. При падении процесса в этот момент boot-resolver умеет восстанавливать неизвестные **ордера через `query_order(coid)`**, а для команды без coid оставляет её unresolved. App затем намеренно отказывается переходить в READY. То есть crash ровно во время SET может оставить движок в состоянии, которое само после рестарта не восстановится. Для `SET_LEVERAGE` boot resolver должен отдельно делать `positionRisk(symbol)` и сравнивать фактическое leverage с payload.

* **P1 — account-specific fallback сейчас логически сломан.** Это самый интересный баг. Допустим global candidate BTC = `200`, но конкретному follower разрешено только `100`. Код делает POST 200 → получает reject → bracket follower = 100 → POST 100 → записывает `ConfirmedLeverage(leverage=100, catalog_revision=current_revision)` и считает аккаунт ready. Это правильно. Но следующий обычный `_snapshot()` получает authoritative `positionRisk = 100` и вызывает `observe()`. А `observe()` считает подтверждённым поколение только когда observed leverage **равен глобальному candidate**, то есть 200. Значение 100 получает revision `-1`, и follower снова становится `not ready`. Получается цикл: `prepare 100 → ready → snapshot → unready → prepare 100 → ...`.

  Причём offline-тест вроде бы проверяет follower с максимумом 100, но только **сразу после prepare**, без последующего `observe()`. А live S1 использует искусственный candidate `999`, после чего fallback возвращает обычный `200`; то есть account-specific отличие `200 global / 100 follower` там фактически не проверено. Исправление небольшое: если текущий `ConfirmedLeverage` относится к текущей revision и authoritative observed value совпадает с **`current.leverage`**, подтверждение надо сохранять, даже если оно ниже global candidate. При смене revision оно естественно инвалидируется.

* **P1 — обещанное восстановление после рестарта "без записи" фактически сейчас не работает.** В `App.start()` сначала выполняется startup reconciliation, где follower snapshot вызывает `observe()`, а **candidate catalog ещё пуст**. Поэтому наблюдённые 200x записываются с revision `-1`. Только потом вызывается `refresh_catalog()`, появляется новая revision, после чего worker видит follower как `not ready` и снова делает POST leverage. А unit-тест restart reconstruction делает операции в обратном порядке: сначала `refresh_catalog()`, потом `observe()`, поэтому проходит. Для заявленной архитектуры лучше просто переставить boot-порядок: `refresh_catalog()` **до** startup follower reconciliation. Если catalog недоступен — входы всё равно останутся fail-closed.

* **P1/P2 — `_leverage_task` на shutdown на самом деле не управляется корректно.** В `_stop_impl()` сначала есть правильный `task_names` с `_leverage_task`, но чуть ниже переменная повторно присваивается уже **без `_leverage_task`**. В результате leverage worker не cancel/await'ится и не обнуляется перед закрытием HTTP/DB. Тест это не заметил: он вызывает `stop()` на App, у которого leverage worker вообще не был запущен, поэтому `_leverage_task` и так `None`. Тут фикс буквально на несколько строк плюс нормальный lifecycle-test с реально живой task.

Остальная ключевая механика выглядит хорошо. Planner действительно берёт `symbol_leverages`, создаёт SET только при реальном mismatch и привязывает MARKET_OPEN/защиту к generic prerequisite. Executor действительно блокирует dependents при FAILED prerequisite, а MARKET_CLOSE остаётся независимым. Snapshot действительно сохраняет leverage flat-строк и fail-closed обрабатывает противоречивые LONG/SHORT значения. И coordinator действительно не делает bracket/SET на обычном пути открытия — readiness читается из RAM.

Отдельно: заявленные **656 passed / 2 skipped** и **20 signed passed / 1 skipped** действительно записаны в текущем status-doc и соответствуют последнему коммиту. Я их сам на его testnet-кошельках не перезапускал, поэтому воспринимаю это как зафиксированный результат его прогонов, а не как мой независимый запуск.

---

## Проход 2: остаточный P1 к `54279d8`

Проверил `54279d8` в репо. Ветка действительно указывает на этот коммит.

И да: **по пункту 3 coding-модель права, а моё прошлое замечание было ошибочным.** В `observe()` уже существовал early-continue:

* `current.leverage == observed`
* `current.catalog_revision == candidate.revision`

Поэтому сценарий `global=200 → follower fallback=100 → observe(100)` действительно сохраняет readiness. Новый регрессионный тест именно это и фиксирует, включая инвалидирование после смены revision.

### Что действительно исправлено

UNKNOWN у первого `POST /leverage` теперь обработан правильно. `-1000/-1006/-1007`, 503/504 и прочие неопределённые ошибки сначала ведут к `positionRisk`; совпадение подтверждает операцию, невозможность прочитать состояние оставляет систему fail-closed. Blind fallback после timeout убран.

Boot recovery для `SET_LEVERAGE` действительно добавлен отдельно от order recovery: команда без `clientOrderId` теперь разрешается через `positionRisk`.

Boot order теперь правильный:

`refresh_catalog → startup reconciliation → worker → READY`

то есть restart reconstruction действительно может восстановить readiness из уже существующего `positionRisk`, не делая лишний POST.

Shutdown тоже исправлен: `_leverage_task` больше не выпадает из `task_names`, и появился тест с реально запущенной task, проверяющий её завершение до закрытия ресурсов.

Но я нашёл **один реальный остаточный баг в новом boot recovery**.

### Остался один P1

В `_resolve_set_leverage_boot()` есть:

```python
values = { ... }
observed = values.pop() if len(values) == 1 else None
...
if observed == target:
    CONFIRMED
else:
    FAILED
```

То есть если `positionRisk` вернул:

* пустой ответ;
* два противоречивых значения, например LONG=20 / SHORT=5,

то `observed=None`, после чего команда становится **FAILED** и boot может продолжиться.

Это противоречит их же заявленному контракту:

> нечитаемо → остаётся in-flight, boot fail-closed

И особенно забавно, что новый тест называется:

`test_boot_conflicting_position_rows_keep_set_unresolved`

но фактически проверяет обратное:

```python
assert report.failed_not_found == 1
assert ...["status"] == "FAILED"
```

То есть **название теста говорит UNRESOLVED, assertions требуют FAILED**.

Это надо исправить. Однозначный `5 != target 20` → действительно `FAILED`. Но `{5, 20}` или `{}` → это не доказанное расхождение, а **неопределённое состояние** → прежний `SENDING/UNKNOWN`, report `unresolved`, boot не должен дать READY.

Исправить:

* exactly one positive leverage == target → CONFIRMED;
* exactly one positive leverage != target → FAILED;
* zero или >1 distinct values → оставить исходный SENDING/UNKNOWN, safe last_error `boot-query-unresolved:ambiguous-leverage`, вернуть unresolved;
* желательно любые неожиданные ошибки parsing/read также трактовать как unresolved, а не позволять им вылететь из boot resolver.

Добавить два теста: conflicting rows → unresolved/in-flight; empty rows → unresolved/in-flight. После этого прогнать offline gate. Signed testnet повторять специально для этого фикса не обязательно.

Есть ещё **неблокирующее P2**: если timeout случится уже на втором, fallback `POST account_max`, `_prepare_at_account_maximum()` не делает read-back, а просто fail-closed. Это безопасно — вход не откроется, worker потом повторит идемпотентную установку, — поэтому я бы сейчас архитектуру ради этого не тормозил.

Итого: **почти принимаю.** Основные проблемы из прошлого ревью исправлены, по пункту 3 я был неправ. Остался один очень небольшой, но принципиальный фикс boot-инварианта `ambiguous ≠ FAILED`. После него я бы leverage control-plane уже закрывал.

---

## Проход 3: финальный acceptance к `69fa148`

Да. **Теперь принимаю leverage control-plane целиком.** `codex/stage-2-telegram` действительно стоит на `69fa148`, родитель — `54279d8`.

Проверил именно финальный production-код. Матрица boot recovery теперь правильная:

* одно однозначное значение `== target` → `CONFIRMED`;
* одно однозначное значение `!= target` → `FAILED`;
* `{}` или несколько различных leverage → сохраняется исходный `SENDING/UNKNOWN`, ставится `boot-query-unresolved:ambiguous-leverage`, возвращается `unresolved`.

То есть главный финансовый инвариант теперь соблюдён: **отсутствие доказательства ≠ доказательство неисполнения**.

Тесты тоже исправлены содержательно, а не косметически. Conflicting `{20,5}` теперь требует `unresolved_query_error == 1`, наличие команды в `in_flight` и сохранение `SENDING`. Отдельно добавлен пустой `positionRisk=[]` с тем же fail-closed поведением.

Сам коммит очень узкий — меняет только `executor.py` и соответствующие unit-тесты. Никакого побочного архитектурного churn нет.

По предыдущему спору тоже окончательно фиксирую: **coding-модель была права насчёт account-specific readiness**, моё замечание про `200 global / 100 follower` было ложноположительным. Early-continue уже обеспечивал нужный контракт; хорошо, что вместо ненужного изменения туда просто добавили regression test.

### Итог

**Acceptance: да. Можно закрывать баги #25/#26 и весь leverage control-plane.**

Цепочка:

`86a51ec → 6f01917 → 54279d8 → 69fa148`

в итоге закрывает:

**P0:** неуспешный SET не пропускает risk-increasing actions.
**P1:** flat leverage не теряется.
**Control-plane:** hot path без leverage REST.
**UNKNOWN:** без blind retry.
**Crash recovery:** SET восстанавливается через authoritative state.
**Ambiguous recovery:** остаётся fail-closed.
**Restart:** readiness реконструируется из exchange state.
**Lifecycle:** worker корректно управляется App.
**Account-specific max:** поддерживается.
**Live Aster:** основной контракт уже прошёл signed testnet.

Заявленные `666 passed / 2 skipped, coverage 87%, ruff clean` присутствуют в коммите; сам прогон я независимо не запускал. Для `69fa148` повторять 20 signed testnet тестов действительно не вижу смысла: изменение исключительно в boot-разрешении неоднозначного локального состояния и не меняет сетевой trading path.

И P2 с timeout второго fallback POST я тоже **не стал бы сейчас трогать**: он fail-closed, поэтому это скорее возможная оптимизация восстановления, а не correctness/safety defect.

**С моей стороны этот этап принят. Дальше копать leverage уже будет скорее вредным overengineering — можно двигаться дальше по проекту.**
