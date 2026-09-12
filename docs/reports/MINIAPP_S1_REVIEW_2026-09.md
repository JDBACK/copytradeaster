# Независимое ревью S1: шаги 12–22

Дата: 2026-09-06. Проверенный HEAD: `f77a3fd`, база: `4f1a86a`.
Scope: `c326894`, `9702ee8`, `4680331`, `f77a3fd`. Шаги 1–11 использованы
как контекст вызовов; повторная приёмка всего ядра не проводилась.
Требования: [UX-план](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md), §7, §10.9,
§11, §14. Масштаб оценки: 10–30 followers, максимум 100, один процесс/SQLite.

**Вердикт: REQUEST CHANGES. S1/G1 закрывать нельзя.** Значительная часть
backend написана и покрыта проверками, однако остались ошибки пользовательских
фактов и незавершённые интеграции. Это не только отсутствие live-прогона.
Предыдущая формулировка «всё исправлено, дальше только live» не подтверждается.

## Что проверено и что сохраняем

Проверены capture→projection→events, application/HTTP contracts, accounting
call sites, production wiring уведомлений, recovery/retention, изменения
миграций и новый live harness. Повторены полная offline suite и ruff check /
format. Результаты и счётчики — только в
[ENGINE_TEST_STATUS](../ENGINE_TEST_STATUS_2026-08.md).

Дополнительные локальные воспроизведения использовали существующие helpers,
настоящие repositories/service и временную SQLite; биржа, рабочая БД и Telegram
не затрагивались. Для recovery заменён только конечный emitter на recorder.
Эти probes не добавлялись в постоянную suite и не являются live evidence.

Удачные решения: отдельная history read model, Decimal и строковые money/IDs,
ownership account/environment, атомарность episode+events, revision CAS,
идемпотентный event insert, изоляция history от исполнения, snapshot singleflight.
Ремонт ghost-v33 проходит имеющиеся regression tests; они сравнивают имена
таблиц/индексов и работоспособность fills, а не все свойства DDL и всех старых БД.
Сохраняем один процесс, SQLite и существующие seams. Переписывать торговое ядро
или вводить брокер очередей ради исправлений не требуется.

## Обязательные исправления

### R1 — P1: детали возвращают исполнения других эпизодов

`application/service.py:2913–2927`: `trade_events` выбирает все fills символа
между opened_at и closed_at. `episode.allocations` не участвуют в отборе.
При HEDGE в детали LONG попадают SHORT fills; при partial history start=0
захватывает ещё более широкий диапазон. Для flip возвращается весь fill,
хотя конкретному эпизоду принадлежит только его доля.

Воспроизведение: у эпизода allocations `[1, 2]`, между ними добавлен
SHORT fill `99` того же символа. Service вернул `['1', '99', '2']`.
Это смешение сделок одного аккаунта, не утечка между пользователями.
Дополнительно `episodes.get` разрешает alias, а запрос событий продолжает
использовать исходный ref; после merge две ссылки могут показывать разные ленты.

Исправить: получать fills по allocation IDs с account/environment/symbol,
возвращать role и allocated quantity отдельно от полного exchange fill;
разрешать canonical ref и связанные alias при чтении событий. Добавить
отдельную bounded пагинацию fills, не повторять весь массив на каждой странице
events. Приёмка: пересекающиеся HEDGE legs, shared flip и alias дают только
относящиеся к эпизоду факты и правильные доли.

### R2 — P1: moving window дублирует эпизоды; открытый старт не интегрирован

`history/capture.py:262–290`: `prepare_window` сдвигает start вслед за
семидневным frontier. Placeholder identity содержит `seed:{window.start}:leg`.
Если исходный entry выпал из окна, ранее опубликованный closed episode
появляется ещё раз под новым ref. `_save` не удаляет/не объединяет прежнюю
публикацию; production calls `merge_ref` отсутствуют.

Воспроизведение через `publish_reconciled(require_settled=True)`:
BUY 2 в t=10, SELL 2 с gross=20 в t=60, flat ending.
Публикации `[0,100]`, затем `[50,100]` дают **два closed episode с gross=20
каждый**. Укороченное окно моделирует тот же переход entry за rolling start.
Если позиция остаётся открытой и началась до окна, settled-gate вообще
отказывает в публикации. Это не реализованный сценарий open-start/late backfill.

Исправить: связать incremental capture с сохранённой границей/seed и identity
уже известного эпизода; не терять его начало при clamp. Подтверждённые corrections
обновляют/объединяют проекцию и refs атомарно, без второй карточки/уведомления.
Partial open history должна быть явно доступна с ограничениями качества.
Приёмка: passage через 7 дней, старт с открытой позицией, её закрытие и поздний
entry/backfill сохраняют один trade_ref либо доказанный canonical alias.

### R3 — P1: события зависят от частоты capture и содержат неверные числа

`history/event_builder.py:64–169`: diff конечного episode теряет промежуточные
переходы, если increase/partial/close попали в один импорт. OPENED получает
итоговый entry_qty/average_entry с timestamp первого входа; несколько REDUCED
получают одинаковый итоговый gross и остаток вместо состояния своего перехода.
First-wins навсегда закрепляет эти значения.

Воспроизведение BUY 2@100 → BUY 1@130 → SELL 1 (gross 10) → SELL 2
(gross 60): только OPENED+CLOSED; OPENED в t=1 уже содержит 3@110.
Две partial reductions в одном capture дают каждой `realized_gross=70`,
`remaining_qty=1`, хотя первая должна нести собственный gross=10 и остаток=2.
Это обычный polling/batching сценарий, а не редкая нагрузочная крайность.

Исправить: строить события последовательной свёрткой новых allocations и
состояния после каждого смыслового перехода; при агрегации частичных fills
одного order явно определить границы события. Dedup опирать на устойчивые
факты, не на размер polling batch. Historical catch-up подавляет push по
возрасту самого события, а не только по свежести конца импортированного окна
(`capture.py` сейчас делает notify_eligible общим для всего окна).
Приёмка: один batch и четыре последовательных capture дают одинаковую
семантическую историю; старое событие, найденное у свежего frontier, не пушится.

### R4 — P1: protection смешивает HEDGE legs

`history/protection.py`: ProtectionFact/BaselineSlot не несут position_side;
state ключуется только `stop`/`take_profit`. `storage/history.py` выбирает
baseline `PARTITION BY kind`, также без leg. Два независимых STOP на одном
symbol трактуются как замена единственного уровня.

Воспроизведение: LONG STOP 90 и SHORT STOP 110 дают SET 90, затем CHANGED
90→110. На деле установлены две независимые защиты. Нельзя показывать это
как перенос стопа и затем корректно привязать к follower trade.

Исправить: проводить доказанную сторону из journal/order facts и ключевать
уровень `(symbol, position_side, slot)`. При неизвестной стороне не утверждать
замену. Проверить placement→replace→cancel на обеих legs и построение baseline
после того, как mutable observation уже обновлена отменой; placement history
нельзя восстанавливать только из последней строки состояния ордера.

### R5 — P1: costs/качество результата не доходят до API

`application/trade_views.py:69–110`, `application/models.py:586–602`:
`net_pnl_usdt=None` безусловно, result_state всегда `gross_only`, DTO не несёт
known commission/funding/builder и конкретных причин недоступности net.
Поиск `account_episode/account_episodes` в `src/` находит только определения
и single-view wrapper; production service/projection их не вызывают.

`null` для недоказанного net — правильное поведение, но само по себе не
выполняет §10.9.4 и §11.1. Известные costs и объяснение пробела тоже нужны.
Live-тест вручную вызывает accountant отдельно от API и скрывает этот разрыв.

Исправить: провести owned fills/cashflows и проверенную completeness/attribution
в application read model, вернуть известные компоненты, issues и result_state.
Net выставлять только при достаточном proof; gross_only с конкретным gap
допустим. Перед включением batch accountant либо ограничить вход одним symbol,
либо ключевать fills/allocations по `(symbol, trade_id)` — сейчас key только id.
Приёмка: HTTP отдаёт совпадающие с accountant known costs и понятные gaps;
полный proof даёт net, неполный — null без выдуманного нуля.

### R6 — P1: recovery теряет хвост после 1000 eligible events

`notifications/router.py:208–267`: лимит 1000 относится к суммарному числу
emitted events по всем accounts, а не размеру страницы. После возврата
`application/engine.py:215–217` безусловно переносит общий floor на now.
Хвост с прежним recorded_at больше не попадёт в следующий recovery.

Воспроизведение настоящим `recover_notification_outbox` с временной SQLite,
1001 eligible событиями и recorder вместо внешней отправки: первый boot
передаёт 1000, второй — 0; суммарно по-прежнему 1000. При 100 followers это
порядка десяти событий на каждого, вполне в масштабе проекта.

Исправить: сохранять durable progress по seq/account либо исчерпывать все
bounded pages перед продвижением floor. На достигнутом лимите нельзя объявлять
окно завершённым. Приёмка: 1001+ eligible events и несколько accounts проходят
через рестарт без пропусков; повтор не удваивает outbox.

### R7 — P2: кнопка сделки фактически не dormant

`application/engine.py:115` уже передаёт miniapp_public_url в worker;
`notifications/worker.py:31–54,253` строит markup по action/ref/URL, без gate
готовности trade route. Recovery уже пишет `action=open_trade`. При обычном
настроенном URL после restart кнопка появляется, хотя frontend route ещё нет.
Локальный вызов `trade_keyboard(ref, 'open_trade', URL)` возвращает markup.

Исправить: отдельный явный capability flag по умолчанию False до S3 либо
не передавать URL именно в этот keyboard path. Mini App menu URL сохраняется.
Приёмка: production worker с настроенным Mini App URL остаётся text-only
до активации trade route; structured action/ref при этом сохраняются.

### R8 — P1: live harness не готов ни к PASS, ни к доказательству G1

`tests/testnet/test_history_acceptance_live.py:243–246`: `list_closed` возвращает
`(items, next_cursor)`, следовательно `len(closed)==1` всегда False (длина tuple=2),
а `closed[0].episode` обращается к list. Следующий `gross > 0` также неверен:
реальная короткая сделка может быть убыточной; нужна сверка с биржей, не знак.

Другие фактические ограничения harness:

- «Не трогает prod-БД» неверно: fixture открывает `settings.db_path` и передаёт
  `NonceStore(creds_db)`, который пишет nonce. Temporary только history DB.
- Cleanup выбирает направление по `rows[0]`, а residual суммирует все legs;
  при первой нулевой SHORT и ненулевой LONG может купить вместо закрытия.
  В one-way уменьшение не защищено reduceOnly. Cleanup нужен отдельно по
  фактической ненулевой leg, с проверкой flat и учётом UNKNOWN outcome.
- Ордера отправляются напрямую AsterClient, а не follower Executor/journal.
  Поэтому §10.9.1 не проверяется. Runtime для service — тестовый adapter.
- «Рестарт» создаёт новые repository объекты на том же открытом соединении;
  процесс/DB не перезапускаются, capture повторно не публикует episodes.
- Нет live SL/UNKNOWN/open-start/backfill и authenticated HTTP acceptance.

Исправить harness offline до подписанного запуска. Текущий файл можно сохранить
как ограниченный exchange-read/projection smoke с честным названием scope;
для G1 дополнить компактным production-path сценарием. Не ослаблять asserts
ради зелёного результата. В этом review live не запускался.

### R9 — P2: feed cursor не реализует обещанную пользовательскую пагинацию

`storage/events.py:169–200`, `application/service.py:2956–2964`: первая страница
возвращает самые старые записи (`seq ASC`); high_water вычисляется после чтения
и не принимается следующими запросами. Это не snapshot-bound feed §11.2.
При большом backfill пользователь сначала увидит начало истории, а не последние
изменения. Между страницами новые строки попадают в существующую выборку.

Seq как технический ingestion cursor подходит для recovery; само его наличие
не устраняет необходимость отдельного порядка пользовательской ленты.
Оставить решение §11.2: high-water в курсоре и `(occurred_at, seq)` keyset,
новые/late строки выше high-water видны после обновления. Это обычный SQLite
query, отдельная инфраструктура не нужна. Closed pages также нуждаются в
явной invalidation/revision policy при исправлении границ эпизода (R2).

## Остаток S1 и решения по замечаниям исполнителя

- **Fills без лимита — исправить вместе с R1.** Проблема уже в принадлежности,
  а длительный эпизод одного аккаунта может быть большим и при 10 followers.
- **Settled-placeholder и clamp — не NIT**, см. R2. Нельзя признавать
  open-start/backfill реализованными, пока runtime их отвергает/дублирует.
- **Seq-сортировка — не принимать молча вместо §11.2**, см. R9. В исходном
  плане late-backfill уже исключается high-water, аргумент исполнителя этого
  механизма не учитывает.
- **Retention без TTL canonical facts разумен**, но бюджет S1.2 не завершён.
  Замер старой схемы не включает итоговые episodes/events и их индексы. Нужен
  один численный прогноз 30/90/365 дней при 100 accounts, DB/WAL/backup и порог
  свободного места. Достаточно существующего benchmark и таблицы допущений.
  Prune 1000 строк только на boot не делает outbox «только in-flight» в steady
  state: процесс может работать месяцами. Нужна периодическая bounded очистка
  либо явно документированное обслуживание с рассчитанным запасом.
- **Задержка worker требует проверки для нашего масштаба.** При одном symbol
  на account: income+repair+fills+capture = 4 jobs; один job/5 s даёт минимум
  200/600/2000 s на круг для 10/30/100 accounts без учёта I/O. Dirty hint
  снимается уже на fills job. Это расчёт кода, не live benchmark; проверить
  bulk master fill→publication и возраст ленты. Исправление может ограничиться
  приоритетом fills→capture и пропуском no-op jobs внутри bounded tick.
- **Backend system events:** риск/pause/resume есть; writers connectivity и
  entry-skipped пока отсутствуют. Зафиксировать источник и записать их до
  миграции consumer; готовый formatter не означает готовое событие. Передача
  qty unit в notification presenter также пока отсутствует (default None).
- **Maps:** новые tests из этой кампании не отражены в `tests/AGENTS.md`;
  обновить при фиксации корректирующих тестов. Общие счётчики держать только
  в ENGINE_TEST_STATUS.

## Порядок исправления и умеренная приёмка

1. R1–R5: корректные собственные факты, стабильные episodes, costs contract.
2. R6–R7, R9: recovery, отключённая до S3 кнопка и пользовательская пагинация.
3. R8: исправить harness; закончить capacity/retention и проверить worker lag.
4. На каждый root cause — компактная регрессия из примеров выше; повторить
   обычную offline suite и lint. Нужен тест поведения, не тест наличия поля.
5. Один выделенный follower: через production executor/journal провести
   open→increase→partial→close, настоящий restart, сверить API и stored payload
   уведомления с биржей. SL/open-start/backfill проверять короткими сценариями;
   UNKNOWN безопаснее вызвать управляемым сбоем transport после реального
   testnet send и проверить query-before-retry, не ждать случайного 503.
   В конце доказать flat. Fake-матрицы покрывают комбинации, signed run —
   связь реальных фактов с production path; клик по будущему UI остаётся G3.

После этого обновить §10.9 evidence и вынести новый verdict G1. До исправлений
статус S1 — IN PROGRESS / REQUEST CHANGES. S2/S3 не принимаются этим review.

## Дополнение: конкретная архитектура remediation

По запросу владельца добавлен §23
[основного плана](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md): C1–C9 с файлами,
контрактами, storage/transaction changes и порядком PR. Исполнителю использовать
review как реестр дефектов, §23 — как основу реализации, а execution journal —
как запись фактически завершённого. Само дополнение плана findings не закрывает.

## Повторное ревью C1–C9 — 2026-09-07

Проверенный HEAD: `9a0dd98`; diff от предыдущего `f77a3fd`.
**Новый verdict: REQUEST CHANGES. Закрытие G1 в `8a7e714` не подтверждено.**
Live PASS из шага 26 сохраняется как evidence конкретного сценария, а не
доказательство всех рабочих путей. Внешние проверки не оспариваются как факт
запуска: дополнительные probes выявили сценарии, которых нет в их assertions.

Повторены полная offline suite, ruff check/format; счётчики и время — в
[ENGINE_TEST_STATUS](../ENGINE_TEST_STATUS_2026-08.md). Изучены изменения
capture/replay/projector, allocation API, событий, money loader, recovery,
feed, worker/benchmark и live harness. Signed вызовов в этом review не было.
Воспроизведения ниже — production classes на временной SQLite, fake exchange
edge и recorder вместо внешней отправки. Ни рабочая БД, ни биржа не изменялись.

### Вердикт по исходным R1–R9

| Finding | Результат повторной проверки |
|---|---|
| R1 | JOIN по allocations, разделение долей и fills cursor исправлены. Полная приёмка details остаётся открытой: N4 обрезает events после 200 |
| R2 | Не закрыт: N1 first-run, N2 continuation и N3 lost dirty range — рабочие пути, не только backfill edge cases |
| R3 | Pure transitions исправляют исходный single-batch gross/qty defect; сквозная история не принята из-за N2/N3, которые меняют identity и теряют накопленный контекст |
| R4 | Исходное смешение HEDGE stop slots исправлено leg-keyed моделью и regression tests; этот узкий дефект принимается. Live STOP-приёмка отдельно ограничена N6 |
| R5 | Money loader подключён к service/DTO, known costs/issues доступны, keys включают symbol. Исходный безусловный gross-only stub исправлен; достоверность полного episode остаётся зависимой от N2/N3 |
| R6 | Durable implementation есть, но default production branch продолжает вызывать старый recovery с потерей хвоста: N5. R6 не закрыт |
| R7 | `trade_links_enabled=False` действительно выключает markup независимо от URL; исходный дефект закрыт |
| R8 | Исправлены tuple unpack, знак gross, journal execution и reopen DB. Общий G1 harness не принят: N6, отсутствует scheduled/intermediate capture, нет собственного SL fill |
| R9 | Исходный feed дефект исправлен: newest-first, v1/high-water и поздние строки за пределами среза. Closed-page revision/filter policy из §23 остаётся отдельным остатком, не выполнена заменой feed cursor |

### N1 — P1: свежий scheduled capture требует историю от эпохи

`history/capture.py:318–323`: при отсутствии origin/checkpoint выбран `start=0`.
`HistoryWorker` предварительно импортирует последние семь дней, а
`HistorySynchronizer._next_window` при наличии coverage продолжает от frontier
с overlap. Он не заполняет промежуток от эпохи до начала первой загрузки.
`publish_projection` честно отвергает такой replay через require_coverage;
на следующем проходе отсутствующее покрытие не появляется.

Probe: заполнено complete окно `[1787395200000,1788000000000]`, origin отсутствует.
`prepare_window` вернул `Span(start=0,end=1788000000000)`.
`run_scheduled` с flat snapshots и обычным synchronizer завершился
`HistoryReconciliationError: fill coverage has a gap`. Эпизоды не публикуются.
Live/benchmark обходят это, вручную передавая корректный start напрямую capture.

Исправление: первый replay start выбирать из принятого непрерывного coverage
**этого symbol**, содержащего frontier, либо из явно сохранённого initial import
intent. Не из account-wide min и не 0. Snapshot proof определяет partial seed
именно этой границы. Первая неудачная попытка не должна навсегда закреплять 0
в legacy capture_state. Regression: fresh account → real worker discovery /
sync / run_scheduled → принятый origin без ручного window.

### N2 — P1: continuation открытой позиции создаёт новые refs и обнуляет basis

`history/projection.py:202–207` повторно передаёт уже публичный seed ref в
`reserve_ref` как anchor; repository выдаёт другой UUID. Даже без этой смены
`PositionSeed` содержит только qty/ref, а projector начинает `_empty(...)` с
partial_history, без старых entry/exit/gross/allocations. `previous` используется
лишь для events diff, не для продолжения денежного/позиционного аккумулятора.

Probe через `HistoryCapture.publish_reconciled` с правильными boundary proofs:

| Публикация | Факты / ending | Фактический результат |
|---|---|---|
| `[0,100]` | BUY 2@100 в t=10 / qty=2 | ref A, opened_at=10, entry_qty=2, complete, allocations=[10] |
| `[101,200]` | BUY 1@130 в t=150 / qty=3 | ref B, opened_at=null, entry_qty=1, partial_history, allocations=[150] |
| `[201,300]` | SELL 3@140, gross=70 в t=250 / flat | ref C, opened_at=null, entry_qty=0, exit_qty=3, partial_history, allocations=[250] |

В БД **три episode rows** вместо одного; прежние open rows не закрыты.
Это обычное открытие → увеличение → закрытие между polling cycles.

Исправление: различить unresolved anchor и resolved public_ref, не reserve
последний повторно. Продолжать полный сохранённый Episode accumulator
(basis/qty/gross/allocations/source/quality), либо replay от начала текущего
открытого эпизода с доказанным seed. Выбрать один путь, не оставлять оба.
Для минимальной правки допустим второй вариант; он не требует нового движка.
Regression: этот цикл и partial→close в четырёх scheduled publications сохраняют
один ref, все allocations, entry_qty=3, average_entry=110 и точный cumulative gross.

### N3 — P1: dirty interval теряется между import job и capture/restart

Worker использует ImportDelta только для RAM hint и не передаёт диапазон в
следующий capture. Capture делает ещё один sync, получает уже пустой delta и
выбирает replay после latest cutoff. Durable `facts_revision` есть, но boundary
не проверяет его как основание invalidation; `coverage_revision` при continuation
копируется из прежней boundary. Поэтому новый round-trip позади cutoff может
вообще не попасть в episodes, хотя оба fills сохранены.

Probe: принято flat окно `[0,100]`. Отдельный import job сохраняет поздние
BUY 1 в t=40 и SELL 1 в t=60; `changed_from_ms=40`. Следующий import/capture
видит их как existing и продолжает после t=100. Ending остаётся flat, поэтому
qty reconciliation проходит: **два durable fills, ноль episodes**.
Сценарий воспроизводим и после crash между import и publication.

Исправление: в import transaction хранить earliest unpublished dirty time
(минимум до успешной публикации) вместе с facts revision. Capture читает его
из storage, а не только из своего последнего запроса. Сбрасывать dirty только
атомарно с публикацией при совпавшей consumed revision. Обновлять revision
boundary, не копировать старую. Gap→complete также требует корректного dirty
интервала: `_new_coverage_start` сейчас смотрит только starts уже merged intervals
и может пропустить заполнение внутреннего gap/расширение прежнего интервала.
Regression: import job → crash/reopen → capture для позднего flat round-trip
сохраняет один episode; повторный импорт его не удваивает.

### N4 — P2: details pagination молча заканчивается на 200 events

`application/service.py:3113`: `_family_events` всегда читает
`after_seq=0, limit=200`, затем фильтрует в Python по cursor клиента.
После первых 200 новые строки недостижимы, каков бы ни был next cursor.
Probe: 201 событие одного ref, страницы по 50 → `[50,50,50,50]`, всего 200,
`next_events_cursor=None`.

Исправление: передавать текущий after_seq в repository, читать `limit+1` на
family ref, слить по seq и обрезать до page limit. Либо один SQL IN по owned
family с общим keyset/limit. Regression: 201+ events, в том числе через alias,
полностью читаются без повторов и преждевременного конца.

### N5 — P1: выключенный новый delivery включает старый неисправный путь

`application/engine.py:229–230`: при `enable_user_event_delivery=False`
вызывается `_recover_user_events` с прежним лимитом 1000, затем timestamp floor
продвигается. Это default production setting. Сам флаг поэтому не выключает
user events, а переключает их на старый механизм. Новые C6 tests проверяют
включённый durable branch, исходный R6 всё ещё воспроизводится по умолчанию.

Probe реальным `recover_notification_outbox` без override capability,
1001 eligible events и recorder вместо внешней отправки: первый boot передал
1000, второй 0; одно событие потеряно. Включённый durable implementation можно
сохранить — исправить нужно routing/rollout.

Исправление по §23.7: False выполняет только legacy **command** recovery,
не любой user-event recovery и не продвижение его progress. True использует
только durable consumer с отдельным rollout floor. Удалить/закрыть production
call sites старого user-event timestamp sweep. Regression через именно default
Stage2 composition: False не enqueue user events, True продолжает >1000 через
несколько budgets без пропусков.

### N6 — P1 для acceptance: STOP cancel не относится к размещённому ордеру

`tests/testnet/test_history_acceptance_live.py:323,372`: STOP размещается через
Executor с реально сгенерированным coid `ct<command_key>`, но CANCEL получает
`origClientOrderId=stop_coid`, то есть исходный `s16-...-stop`. Executor не
переписывает target отмены. `-2011` подтверждается как already absent, поэтому
`cancel_results.status == CONFIRMED` может пройти без отмены нужного STOP.

В конце harness проверяет flat quantity, но не итоговый `open_orders()==[]`;
финальный MARKET_CLOSE также не является исполнением собственного SL.
Если биржа сама уберёт protection после flat, это не докажет предыдущую отмену.
По коду нельзя подтвердить обещанное live «STOP place+cancel, ордеров нет».

Исправление: взять target coid из placement journal row, после cancel сверить
exchange status/order absence именно этого id. Cleanup включает все созданные
защитные ордера и финальную проверку open_orders. Отдельный небольшой сценарий
SL fill обязателен по §10.9; наличие неисполненного STOP его не заменяет.
Не требуется заново переписывать harness: journal/HTTP/reopen исправления
полезны и сохраняются. Но добавить scheduled capture между lifecycle шагами,
чтобы N1–N3 были проверены реальным маршрутом, а не одной публикацией после flat.

### C8: что именно доказывает benchmark

Новая схема действительно участвует в storage замере; прогноз объёма лучше
прежнего. Однако `publish_per_account_ms_p95=552` — время прямого вызова
capture на предзагруженных facts, margin=1 ms и подставленных часах. Скрипт
не запускает HistoryWorker.run_once/_run, не измеряет очередь 30 accounts и
не включает production 5 s interval/2000 ms margin. Это CPU/SQLite service time,
**не p95 fill-observed→published всей системы**.

Worker всё ещё выполняет один real job за tick; ожидание margin внутри capture
блокирует его serial loop. Request stats считают fills jobs и boundary snapshots,
но не income/repair и повторный fills read внутри capture. Закрытие C8 latency
на основании этих счётчиков/552 ms преждевременно. После N1–N3 измерить именно
worker path для 10/30/100 accounts с fake exchange и production scheduling
policy; небольшого hermetic workload достаточно, новой инфраструктуры не нужно.

### Минимальный следующий проход

1. N1–N3 одним согласованным C1-fix: initial coverage, полноценная continuation,
   durable dirty/consumed revision. Не исправлять только возвращаемый список.
2. N4/N5 — небольшие отдельные исправления cursor и rollout routing.
3. N6 — поправить target отмены/cleanup, добавить scheduled промежуточные
   captures и SL fill; затем один согласованный signed прогон.
4. Повторить regressions, обычную suite и worker timing. Новый verdict G1 после
   evidence этих сценариев. Отдельные принятые fixes R4/R5/R7/R9 не откатывать.

Статусы G1 в roadmap/TL;DR скорректированы на OPEN. Изолированная работа по S2
на существующих API допустима по §14; зависимый новый UI этим review не принят.

## Уточнение приоритетов и передачи реализации — 2026-09-07

После обсуждения с владельцем масштаб 10–30 аккаунтов учтён в gate:
обязательные блокеры — N1–N3. N4 не блокирует G1; N5 понижен из безусловного
P1 до условного recovery follow-up; N6 — дефект evidence harness, а не
установленный дефект рабочего Executor. Потребность в обязательном новом
100-account benchmark/полном signed цикле для corrective G1 снята.

Факты воспроизведений выше не отменены. Их сроки, конкретная архитектура,
достаточные regression tests и условия самостоятельного закрытия реализатором
теперь определяются [FIX_HANDOFF](MINIAPP_S1_FIX_HANDOFF_2026-09.md).
Он имеет приоритет над прежней формулировкой «все N1–N6 блокируют G1».
SL fill и доказанная отмена STOP остаются в end-to-end acceptance до production
rollout новой истории; совместить с G3. Эта запись не закрывает findings:
требуется фактическое выполнение checklist FIX_HANDOFF §6.

## Current disposition — 2026-09-13

Вердикты `REQUEST CHANGES` и `G1 OPEN` выше относятся к независимому ревью и
его handoff-срезу 2026-09-06/07; они сохраняются без переписывания. Findings
N1–N3 закрыты corrective G1, N4–N6 закрыты в последующем S3/G3-проходе.
Текущий execution-evidence и общий статус Mini App находятся в
[`MINIAPP_FUNCTIONAL_PROGRESS_2026-09.md`](MINIAPP_FUNCTIONAL_PROGRESS_2026-09.md)
и [`ENGINE_TEST_STATUS_2026-08.md`](../ENGINE_TEST_STATUS_2026-08.md).
