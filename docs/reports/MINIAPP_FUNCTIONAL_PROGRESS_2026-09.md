# Реализация функционального Mini App — 2026-09-06

Источник требований: [план](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md), §14.
Кампания **CLOSED 2026-09-09**: G1, G2 и G3 приняты; исторические шаги ниже
сохраняют статус на момент записи.

Актуальная фактическая UI-приёмка Mini App в Telegram WebView и Chromium/CDP,
включая frontend-тесты, screenshots и финальный scope G3, собрана в
[центральном UI acceptance index](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).

## Рабочий todo

- [x] S1.1: typed Aster reads, signed read evidence, учётная политика и units.
- [x] S1.2: schema/repository, observations, bounded sync, retention и замер роста.
- [x] S1.3: episodes, partial/flip, cashflows, stable refs, user events.
- [x] S1.4: application/HTTP contracts, ownership, aggregate, settings/scope/freshness.
- [x] S1.5: общий presenter, durable structured notifications.
- [x] G1 corrective acceptance: N1–N3, T1–T6 и §6 offline gate; N4–N6
  закрыты в последующей S3/G3-кампании.
- [x] S2/G2: baseline, session resources, transport/lifecycle, nested settings cache,
  measured performance acceptance; закрыто 2026-09-07, отдельный журнал —
  `MINIAPP_S2_PROGRESS_2026-09.md`.
- [x] S3/G3: Home, Open/Closed/detail, activity/deep links, settings/copy,
  Chromium, Telegram Desktop, iPhone Mirroring и signed testnet; CLOSED в
  согласованном scope.
- [x] После основы: независимый review шкалы и исправление найденных дефектов.
- [x] Финал: Telegram smoke, screenshots и performance acceptance.

## Шаг 1 — read adapter

Добавлены UserTradeDTO/IncomeDTO и AsterClient.user_trades/income_history.
Используются существующие signed transport и weighted limiter. Decimal сохраняет
знак комиссии и income; float/non-finite и повреждённая пачка отвергаются целиком.
Нет предположения, что SELL означает SHORT. IDs не преобразуются в float.
Ошибки валидации не раскрывают исходный payload. Read API ещё не подключён к worker.

Проверен официальный [V3 reference](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md):
userTrades имеет fromId, несовместимый с time bounds; income не документирует
page/fromId. Поэтому для income нужны subdivision временных окон и явный gap,
если один timestamp насыщает limit: молча продвигать cursor нельзя.
Уникальность income tranId ограничена incomeType внутри аккаунта.
Знак commission, Builder accounting, pagination на живой бирже пока НЕ приняты.

В окружении editable package указывает за пределы checkout. Для локальных
проверок использовать `PYTHONPATH=src .venv/bin/pytest ...`; иначе тестируется
другая версия исходников. Результаты проверок — в ENGINE_TEST_STATUS.

## Шаг 2 — signed read evidence и bounded reader

2026-09-06: read-only signed testnet probe существующей истории copyc1, без
запуска App/движка, без order/flatten/mode mutations, nonce в tmp SQLite.
`tests/testnet/test_history_reads_live.py` проверяет непустое окно, fromId,
связь комиссии с income и metadata base/quote/margin assets BTCUSDT.
Первое семидневное чтение достигло лимита. Для проверки семантики взято
минутное populated окно; это не доказательство полноты всей истории.

Наблюдение: userTrades commission положительная, совпадающий COMMISSION income
отрицательный с тем же модулем. Это отличается от отрицательной commission
в документационном примере. Сохраняем raw знак; нормализация не может основываться
только на документации или abs. Одна комиссия окна не связалась с income:
полноту net для всех fills это наблюдение НЕ доказывает. Funding и отдельная
Builder fee пока не доказаны. Следующая учётная проекция обязана отличать
подтверждённые денежные потоки от unknown/unattributed; REALIZED_PNL и COMMISSION
в двух источниках не суммируются дважды.

`history/reader.py`: bounded subdivision включительных окон, без потери границы;
отдельные complete/pending/saturated интервалы. Full page не считается complete;
при насыщении одного timestamp сохраняются известные факты и gap. Ошибка/отмена
пробрасывается владельцу транзакции, checkpoint не продвигается. Worker/repository
ещё не подключены. Продолжение S1.2: атомарное хранение фактов и этих checkpoints,
observations, worker и retention measurement.

## Шаг 3 — durable facts и возобновление импорта

Добавлены append-only migration 33–39: fills, cash flows, order observations,
coverage windows и индексы. `storage/history.py` атомарно записывает факты и
complete/pending/saturated окна. Scope — account + environment; income identity
включает income_type. Decimal канонизируется без округляющего normalize.
Конфликт старого exchange identity с новыми фактами прерывает всю транзакцию.
Наблюдения ордера обновляются на месте и не становятся fills.

`history/sync.py` выполняет один ограниченный шаг, сначала подбирает durable
pending window; HTTP вне write lock. Сетевой сбой сохраняет прежний checkpoint.
Пока это building block: lifecycle worker, account-symbol discovery и executor
hooks ещё не подключены. Episodes/events добавляются следующим слоем перед UI.

Промежуточный замер первоначальной per-row реализации: 1000 fills занимали
134.6 ms транзакции на локальном tmp SQLite. Заменено на batch SQL по 200 строк,
с прежней атомарностью/проверкой конфликтов; итоговый capacity/latency benchmark
ещё предстоит. Canonical facts не очищаются TTL; retention budget пока не принят.

## Шаг 4 — наблюдение ордеров в production executor

`Executor.order_observer` получает account, durable command key, kind и OrderDTO:
обычный ответ, successful cancel, SUBMITTED/UNKNOWN query recovery. Отдельный
optional observer в `resolve_boot_unknowns` охватывает восстановление после crash.
`App` связывает observer с HistoryRepository, изолирует REAL/testnet/mainnet/SHADOW
и пропускает dry-run/master. observed_at здесь — биржевой updateTime, чтобы
запоздавший ответ не затирал более новое наблюдение.

Ошибка observer логируется безопасной категорией, не преобразует торговый
результат в FAILED/UNKNOWN и не инициирует resend. Cancellation не подавляется.
Сбой записи может оставить недостающее observation: восстановление по durable
journal/order links должно быть реализовано periodic history worker; этот шаг
сам по себе не закрывает crash completeness. Worker lifecycle ещё не подключён.

## Шаг 5 — фоновый worker в Stage2Process

HistoryWorker запускается после App.start и останавливается до App.stop/DB close.
Один последовательный запрос за шаг, rotation по streams/account, ограниченный
backoff ошибки, отмена in-flight чтения на shutdown. Отсутствующий account удаляет
его retry state. Источники symbol: whitelist, observations, cashflows и journal.
SHADOW/dry-run не вызывают account history endpoints. Общий Aster limiter сохранён.

После завершения начального окна fallback читает от последней verified границы
с минутным overlap. Pending windows всегда приоритетны; saturated остаются gap.
Это ещё не performance acceptance на 100 accounts: измерение задержки полного
оборота и tuning отдельного history weight budget обязательны перед G1.
Не завершены periodic order-observation repair, расширение discovery по snapshot,
retention/coverage compaction и измерение роста. S1.2 остаётся открытым.

## Шаг 6 — repair, compaction, предварительная ёмкость

History repair обнаруживает пропущенные/незавершённые observations по journal,
читает ордер и записывает его до repair checkpoint. Отсутствие ордера на бирже
не меняет CONFIRMED command; следующий retry не раньше 5 минут. Crash между
observation и checkpoint допускает безопасный повтор. У UNKNOWN/SENDING
по-прежнему единственный владелец recovery — торговый executor.

Проверенные соседние/перекрывающиеся интервалы history_sync_state объединяются
в транзакции; настоящие gaps не заполняются. checked_at объединённого интервала
консервативный (минимальный), не выдаёт старую историю за свежепроверенную.

Предварительная нагрузочная модель (`scripts/benchmark_history.py`): 100 accounts,
2 fills на ордер, 100k fills/100k cashflows/50k observations. Аллокация с индексами:
fills 34,140,160 bytes; cashflows 22,196,224; observations 9,302,016.
DB/backup 65,880,064 bytes (~62.83 MiB); WAL peak snapshot 4,358,992.
Пачка 1000 fills: p50 5.49 ms/max 7.67 ms, локальный tmp SQLite, без trading writer.

Для текущей схемы это ~659 bytes на fill вместе с одним cashflow и половиной
observation. Сценарий 10 эпизодов/account/day × 2 orders × 2 fills × 100 accounts:
4000 fills/day, ~79/237/962 MB за 30/90/365 дней. Высокий сценарий 100 эпизодов:
~0.79/2.37/9.62 GB. Частые изменения защиты, episodes/events, order checks,
остальная БД движка, snapshot WAL и backup добавляются сверху. Начальный
семидневный backfill при этой модели ~18.5 MB или ~185 MB.

Решение по retention: canonical facts без молчаливого TTL; observations обновляются
на месте, coverage compaction включена. Числа служат входом окончательного
бюджета после episodes/events; H1 capacity пока не принят. Перед G1 нужны
финальная схема, профиль protection updates, параллельный trading-writer latency,
полный disk/headroom budget и эксплуатационные пороги/диагностика.

## Шаг 7 — чистая модель эпизода фолловера

`history/models.py` и `history/projector.py`: authoritative PositionSeed,
Episode/FillAllocation, ordered deduped fills, увеличение/частичное закрытие,
zero→position→zero, BOTH flip и независимые HEDGE legs. Flip распределяет qty
между старым и новым эпизодом, realizedPnl учитывается один раз на закрытии.
Source задаётся подтверждённым order mapping; неизвестная связь не становится
external автоматически. Для неизвестного seed qty=null/opened_at=null/partial;
нулевой остаток не выводится из взаимного погашения наблюдавшихся fills.
Известный ненулевой seed сохраняет placeholder ref и partial до flat.

`complete` в этой чистой функции означает известное начало эпизода при переданном
доказанном seed; это НЕ доказательство net, coverage или snapshot reconciliation.
Persistence/snapshot seeding/rebuild/aliases и financial accounting ещё не
подключены. Экраны по этой функции пока не включаются. Следующий шаг S1.3:
сохранение episodes/allocations и устойчивые refs, затем денежные потоки/events.

## Шаг 8 — evidence-based денежные потоки эпизода

`history/accounting.py`: gross из fills, commission из подписанного income,
rebates сохраняют знак; повторный flow id дедуплицируется, конфликт отвергается.
REALIZED_PNL из income не прибавляется второй раз, transfers не относятся к
trade PnL. Funding/отдельный Builder flow входят в эпизод только при переданной
доказанной attribution; non-USDT cost не пересчитывается текущим FX.

AccountingProof по умолчанию не разрешает exact net. Неизвестная Builder policy,
неполный income, partial history, missing/mismatched commission дают явные issues.
Тип BUILDER_FEE используется только синтетическим тестом механизма; наличия такого
типа на Aster тест не доказывает. Production policy остаётся unknown до evidence.
Комиссия flip fill, общего двум эпизодам, пока shared_fill_commission (net=null):
нужна единая batch allocation policy с сохранением суммы, до G1 этот случай не
считать принятым. Storage/aliases и accounting wiring ещё впереди.

## Шаг 9 — persistence эпизодов и стабильные refs

`storage/episodes.py` выделен из растущего history repository как cohesive
подрепозиторий проекции: UUID public refs на устойчивый внутренний anchor,
account/environment ownership, revision CAS, idempotent save. Episode payload
сохраняет Decimal strings и allocations; фильтруемые scope/time поля вынесены
в индексируемые колонки. Это внутренний payload, не публичный HTTP DTO.

Alias merge разрешён только явным вызовом после доказательства общей сделки,
внутри одного account/environment/symbol/leg. Старые ссылки переходят к
канонической, цепочки уплощаются, циклы/чужой scope/обновление alias запрещены.
Повторная reservation старого anchor также возвращает canonical ref.
Не делать автоматический merge только по symbol. Split/backfill reconciliation
policy и атомарное batch publication с events ещё впереди; сохранение отдельных
эпизодов само по себе не закрывает этот gate. Дальше: snapshot seed/coverage
proof, публикация проекции из sync и стабильный list/detail service.

## Шаг 10 — атомарная публикация и reconciliation seam

EpisodeRepository.publish публикует набор эпизодов одной транзакцией; конфликт
последней revision откатывает и предыдущие изменения. `history/reconcile.py`
проверяет непрерывную включительную coverage и signed BOTH/независимые HEDGE
остатки. Unknown remainder не сертифицируется даже нулевым ending snapshot.

`HistoryProjection.publish_window`: durable fills + complete windows → pure
projector → проверка ending quantity → stable reservations → atomic publication.
Есть integration test реального tmp SQLite, повторной публикации и отказа при
snapshot mismatch без повреждения прошлой версии. Seed и ending quantity пока
передаются явными аргументами доверенного caller, не угадываются по fills.

Runtime acquisition согласованных boundary snapshots, сериализация import/project
одного account-symbol, incremental checkpoints и backfill split/merge всё ещё
нужны перед подключением этого seam к worker. Пока runtime не публикует episodes;
G1 не закрыт. Не считать совпадение ending quantity доказательством полноты
income/net или достоверности произвольно выбранного seed.

## Шаг 11 — позиционная boundary evidence

PositionDTO сохраняет nullable updateTime без permissive default. Read-only
signed testnet подтвердил поле у обеих HEDGE-сторон, обе timestamps ненулевые.
`history/snapshot.py` нормализует signed BOTH и HEDGE magnitude, требует полный
mode набор без дубликатов и сравнивает qty/entry/updateTime двух снимков.
Одинаковая qty с новым updateTime не считается неизменной позицией.

Исторический cutoff должен находиться между получением первого ответа и началом
второго запроса с clock margin 2000 ms; изменения позже cutoff отвергаются.
Согласованная boundary + отдельная complete fill coverage позволяют восстановить
начальный qty обратным суммированием. Ненулевой seed остаётся partial entry с
placeholder ref; это не выдуманная историческая цена входа.

Это механизм проверки, ещё не worker capture state machine. Не приняты:
устойчивые incremental checkpoints за пределами семидневного окна, согласование
REST indexing latency, generation barrier и capture/import/recheck wiring.

## Шаг 12 — serial boundary capture seam

`history/capture.py`: `read_boundary` снимает один авторитетный `positionRisk`
snapshot с локальным clock-интервалом; `HistoryCapture` сериализует
capture/import/project одного account/environment/symbol персистентным
`asyncio.Lock`, доказывает `prove_unchanged` с margin 2000 ms, выводит seeds
обратным суммированием из доказанной ending-границы, резервирует стабильные
placeholder refs (`seed:{window.start}:{leg}`, идемпотентно) и публикует через
`HistoryProjection.publish_window`. Неизвестная связь ордеров остаётся
`unknown`, не становится `external`. `capture_and_publish` сначала выполняет
bounded import (durable прогресс сохраняется), затем доказывает live edge;
отложенная публикация — ожидаемое состояние (`HistoryReconciliationError`),
а не повод для worker backoff. `App.episodes` и `build_history_capture`
подключены в композиции; периодическая активация worker, durable capture
checkpoints, user events и shared-fill allocation остаются следующими.

`tests/unit/history/test_capture.py`: 12 checks (happy path + idempotent
republish, changed position, unbracketed cutoff, coverage gap, placeholder
stability, ending mismatch без потери prior версии, concurrent serialization,
ownership isolation, symbol validation, full import→publish wiring, margin
fail-closed с сохранением fills). History targeted: **67 passed**; full
Runtime пока не публикует episodes периодически; G1 не закрыт.

## Шаг 13 — хранилище пользовательских событий

Append-only миграция 45–47 (`user_activity_events` + индексы feed/trade):
внутренний монотонный seq, UNIQUE public event_ref, account/environment
ownership, optional trade_ref без FK-связывания порядка записи, closed kind set
из §7.1 (13 видов: открытия/увеличения/уменьшения/закрытия, защита, риск,
пауза, связность, пропуск входа), occurred_at/recorded_at, canonical JSON facts
без float, UNIQUE(account, environment, dedup_key), notify eligibility. Счётчик
миграций в AGENTS.md обновлён (48, индексы 0..47).

`storage/events.py`: `EventRepository.record` идемпотентен по dedup key —
повтор возвращает первую запись без merge-обновления (события неизменяемы,
требование §11.2 для стабильности курсора). `get`/`list_owned`/`list_by_trade`
всегда фильтруют scope; чужой scope видит пусто. Cursor — строго растущий seq,
поздний backfill дописывается в конец и не сдвигает открытые страницы; limit
bounded 1..200. Факты принимают только str/int/bool/null. Это storage-фундамент:
вывод событий из подтверждённых фактов и привязка trade_ref при публикации —
следующий слой перед S1.5/B8.

`tests/unit/history/test_events.py`: 10 checks (opaque ref/canonical facts,
dedup replay, seq order, ownership isolation, cursor stability + bounds, kind
History targeted: **77 passed**; full offline:
**1192 passed / 2 skipped / 44 deselected**. Ruff changed files clean.

## Шаг 14 — вывод событий из подтверждённых переходов эпизодов

`history/event_builder.py` (pure, без I/O): `episode_events` сравнивает ранее
опубликованные эпизоды со свежей проекцией и выпускает черновики immutable
событий только за genuinely новые биржевые факты — OPENED (первая entry-аллокация,
включая backfill-нутый placeholder; occurred — время входа), INCREASED (рост
открытой позиции, qty/цена/остаток), REDUCED (частичное закрытие, закрытая часть
и остаток; при закрытии в том же раунде не дублируется), CLOSED (реализованный
gross, средние вход/выход, длительность). Повторная проекция даёт ноль черновиков.
Цены только из fills: недостающий fill абрагирует всю пачку, не выпускает событие
без цены. Неизвестная связь остаётся `unknown`. Dedup key стабилен
(`evt:open/close:{ref}`, `evt:exit/entry:{ref}:{trade}:{role}:{qty}`) и совпадает
с семантикой `EventRepository` (first-wins). `notify_eligible` — политика caller
(live capture — eligible, backfill — нет), здесь не выводится. Привязка trade_ref
естественна: черновик всегда несёт ref своего эпизода. Проводка черновиков в
транзакцию публикации и protection/system-события из журнала — следующий слой.

`tests/unit/history/test_event_builder.py`: 12 checks (open facts + key stability,
repeat-silence, increase, partial, open+close одного окна, close-only после ранней
публикации, flip без выдуманной связи, пустой placeholder, unknown direction,
missing fill abort, recorded_at guard, scope guard). History targeted: **88 passed**;
full offline: **1203 passed / 2 skipped / 44 deselected**. Ruff changed files clean.

## Шаг 15 — атомарная проводка событий в транзакцию публикации

Требование §10.6 «fills, проекция и события — одной транзакцией» выполнено для
пары проекция/события (fills фиксируются раньше import-транзакцией; инвариант
«нет опубликованного эпизода без его событий» держится порядком и атомарностью).
`EventDraft` переехал в `storage/events.py`; `insert_many` пишет черновики на
чужом соединении после общей валидации пачки. `episodes.publish(..., events=)`
сохраняет эпизоды и события одной транзакцией: конфликт revision и невалидный
черновик откатывают всё. `HistoryProjection` принимает `EventRepository`,
загружает предыдущие эпизоды, строит черновики через `episode_events` и требует
`recorded_at` при включённой эмиссии. `HistoryCapture`/`App.user_events`/
`build_history_capture` проводят репозиторий и политику сквозно;
`capture_and_publish` штампует `recorded_at` временем after-снапшота без лишнего
тика часов. Без events-репо поведение прежнее (обратная совместимость тестов).

Новые `tests/unit/history/test_episode_events.py`: 6 checks (совместная запись,
откат событий при конфликте revision, dedup при републикации, невалидный черновик
абортит запись эпизода, проекция эмитит OPENED с trade-ссылкой и recorded_at,
History targeted: **97 passed**; full offline:
**1212 passed / 2 skipped / 44 deselected**. Ruff changed files clean.

## Шаг 16 — worker-активация capture с durable checkpoints

Runtime впервые публикует эпизоды периодически. Миграция 48
(`history_capture_state`: cap_start/cap_end per account/environment/symbol,
secret-safe last_error, updated_at); счётчик в AGENTS.md — 49 (0..48).
`HistoryRepository.capture_state/record_capture` — dumb put-семантика, политика
выше. `HistoryCapture.prepare_window` — стабильное окно из durable состояния без
биржевых чтений: нет complete-покрытия / фронтир опубликован / фронтир моложе
margin — пропуск; старт пинится к checkpoint (стабильность placeholder-якорей),
спаны шире 7 дней детерминированно клампятся к newest slice.
`publish_reconciled(..., require_settled=True)` отказывает окнам поверх открытой
позиции (ненулевые seed и ending): steady-state публикует только settled-диапазоны,
позиция не фрагментируется на per-window placeholders. `run_scheduled` связывает
всё: prepare → capture_and_publish → checkpoint; пропуск без чтений — False,
отложенная публикация пишет checkpoint-ошибку (только имя типа, без секретов) и
пробрасывается в worker-backoff; свежие края (5 мин, LIVE_ELIGIBLE_MS) eligible,
backfill — silent. `HistoryWorker`: capture-jobs в ротации (отдельные от fills —
backoff публикации не тормозит импорт), `mark_dirty` приоритизирует symbol
(best-effort RAM-hint, пропуск компенсируется sweep), `App._observe_history_order`
дергает hint через `history_mark_dirty` (set в `build_history_worker`), сбой hint
не ломает торговлю. Discover отдаёт fills+capture jobs на symbol.

Тесты: checkpoint put/validation (storage, +1), prepare ×4 (пусто/свежесть/
published+reuse/clamp), settled ×3 (refuse mid/flat/flat-close-unknown),
run_scheduled ×3 (success+checkpoint+eligible/idempotent-False/skip/deferral+error),
worker ×5 (callback backoff/dirty-priority/prune/no-wiring), App hook ×2.
History targeted: **113 passed**; full offline:
**1230 passed / 2 skipped / 44 deselected**. Ruff changed files clean.

## Шаг 17 — order-link источники, protection-события, shared-fill split

`HistoryRepository.proven_order_sources` возвращает order id → `copy` только
при согласии трёх durable строк: observation, CONFIRMED entry/exit-команда под
тем же command key и order link с непустым master trade id под тем же follower
coid (виды совпадают). Проекция резолвит источники сама, если caller не передал
явно; `HistoryCapture`/`build_history_worker` проводят `CommandRepo`. Частичная
связь остаётся `unknown` — mixed требует доказанного external, отсутствия связи
недостаточно. Новый `history/protection.py`: загрузчик берёт baseline (последние
placements до окна минус отменённые) и факты окна только из CONFIRMED-команд,
цены — из journal payload; чистая свёртка даёт PROTECTION_SET/CHANGED/
UNCONFIRMED в той же транзакции публикации. Пара замены — одно CHANGED, stale
cancel молчит, непривязываемый cancel молчит, trade_ref пуст (journal trade id —
master-side, join к follower ref невозможен). System-события (риск/пауза/
связность/пропуск) из журнала не выводятся: их источник — application-слой,
угадывание из команд было бы выдумкой; это работа S1.5 поверх новой ленты.
`account_episodes`: batch-комиссии shared-fill делятся pro-rata с точным
сохранением подписанной суммы (остаток — последней доле, округлений нет);
`account_episode` — single-view wrapper, частичная аллокация без сиблингов
по-прежнему `shared_fill_commission`.

Тесты: accounting +3 (flip split/неровный split/частичный view), storage +4
(sources/exclusions/baseline+window/by_client_order_id), protection fold +9,
capture +3 (copy/mixed→unknown/protection SET в публикации). History targeted:
**132 passed**; full offline: **1249 passed / 2 skipped / 44 deselected**.
Ruff всего репо clean.

## Шаг 18 — S1.4 контракты истории: list/detail/feed, overview, singleflight

Storage: `EpisodeRepository.list_closed` (неизменяемый курсор
`closed_at:ref`, фильтры symbol/since) и `list_open`; `EventRepository.max_seq`
(high-water); `CommandRepo.in_flight_for` (pending-блок одного аккаунта);
`application/models.py`: TradeListItem/Page, TradeDetailDTO (финальность
`gross_only`, net пуст до S1.6), FillFacts/EventItem/EventsPage,
ActivityFeedPage (items + high-water + pending), AccountOverview (без
суммарного risk_to_stop — запрет §5), `TradeUnavailableError`
(`trade_unavailable`, чужой/missing читаются одинаково), методы в
`ApplicationProtocol`. Новый `application/trade_views.py`: чистые презентеры
эпизод/детали/fill/событие/overview/units — backend считает, frontend
форматирует; unknown direction и units — null, не догадки. Service проводит
репозитории из `runtime.db`, scope environment как testnet-граница,
`trades`/`trade`/`trade_events`/`activity_feed` с ownership и валидацией,
pending-блок из in-flight журнала (symbol/класс/возраст, без payload),
`overview` аддитивно в DashboardDTO, refresh-snapshot singleflight
(shield-join, отмена одного waiter не абортит остальных). Адаптер
`symbol_assets`: baseAsset из кешированного exchange_info, недоступное — null.
HTTP: `trades`/`trade`/`trade/events`/`activity/feed` (стало 25 POST),
строгая типизация тел, старые routes untouched. Frontend не тронут.
Явно отложено: risk preview, resume-контекст, net-атрибуция, TS-зеркала,
удаление старого activity (H5 открыт).

Тесты: trade_views +9, service-контракты +12 (владение/курсоры/freshness/
singleflight/units), HTTP-граница +9 (auth/валидация/сериализация/
trade_unavailable). History targeted: **132 passed**; full offline:
**1279 passed / 2 skipped / 44 deselected**. Ruff всего репо clean.

## Шаг 19 — S1.4 остаток: risk preview и resume-контекст

Risk preview: `RiskPreviewStep/DTO` в `application/models.py` — денежный пример
для каждого разрешённого шага линейки (0,5–5% шагом 0,5%, 10 строк точной
Decimal-математики `available × percent`), `risk_budget_steps` как чистый
презентер в `trade_views.py`, `service.risk_preview` поверх cached snapshot
(без биржевых чтений и side effects: линейка грузит один раз и переключает
готовые строки), freshness-метка, пустые steps при недоступном балансе вместо
нулевого примера. Route `risk/preview` (стало 26 POST). Resume-контекст:
`ResumeContextDTO(master_in_position/snapshot_freshness/permission_ok)` и
аддитивное поле `Confirmation.resume_context` только для RESUME; service
собирает факты cached-only (`master_has_open_positions`, cached snapshot,
cached verified-only permission triple), любая недоступность даёт None
(unknown), а не отказ begin. Старый hardcoded «Мастер сейчас в позиции»
(`miniapp/src/home.ts`) теперь заменяется выбором копии по этим фактам —
сама замена копии в UI остаётся S3. TS-зеркала новых DTO отложены вместе
с остальными; frontend не тронут.

Тесты: service +8 (10 шагов/точность 24.80, empty/unavailable, stale-метка,
чужой user, master True/ошибка→None, denied permission→False, non-resume→None),
презентер +5 (сетка + reject невалидных диапазонов), HTTP +1 (auth/сериализация
preview и resume-контекста, Decimal/enum кодирование). Full offline:
**1293 passed / 2 skipped / 44 deselected**. Ruff + format всего репо clean.

## Шаг 20 — S1.5 общий presenter, structured outbox, recovery из событий

Общий презентер `notifications/trade_cards.py` (pure): `format_user_event`
рендерит все 13 видов stored-событий по примерам §7 — открытие/увеличение/
сокращение/закрытие с объёмами и ценами, защита SET/CHANGED/UNCONFIRMED,
системные RISK/COPY_PAUSED/COPY_RESUMED/CONNECTIVITY/ENTRY_SKIPPED. Нейтральный
заголовок закрытия, знак результата не выводится из BUY/SELL, итог всегда
«до комиссий» до S1.6-атрибуции, missing result — «Результат уточняется»
(не ноль), unknown side/unit опускаются, не угадываются; unknown kind —
ValueError. `event_notification_action` даёт `open_trade` только торговым видам.
Structured outbox: миграции 49–50 (`trade_ref`, `action`, без backfill старых
строк; стало 51, индексы 0..50), `NotificationEvent.trade_ref/action`
(closed set `NOTIFICATION_ACTIONS`, action требует доказанный ref), сквозная
передача router→outbox. Worker: чистый `trade_keyboard` (кнопка «Открыть
сделку» с `?trade_ref=` только при доказанном ref + известном action +
настроенном base URL, иначе None — dormant до S3), `_send` несёт markup только
на Bot API path (custom senders text-only, старые consumers совместимы),
`webapp_base_url` из `CT_MINIAPP_PUBLIC_URL` в engine. Recovery из сохранённых
событий: `router.recover_user_events` (eligible после floor, backfill молчит,
нерендерящиеся строки логируются и пропускаются, severity WARN для потери
защиты/связи), engine вызывает его на старте поверх legacy command recovery
(разные namespaces ключей; только REAL-фолловеры). System-писатели service:
`update_risk`→RISK_CHANGED (новый + предыдущий процент), `pause`→COPY_PAUSED,
`resume`→COPY_RESUMED — записи best-effort поверх ленты (сбой записи логируется
и не валит committed-операцию), факты непустые (требование storage). Push новых
событий и замена live-текстов — S3-активация; menu `render_activity` остаётся
на старом activity до перехода ленты (H5 открыт); ENTRY_SKIPPED/CONNECTIVITY
писатели отложены (нет источника с нужным scope), рендер готов.

Тесты: презентер +7, recovery +3 (eligible-only/backfill-silent/trade-линк/
replay-dedup/chужой scope/invalid floor/unrenderable-skip), keyboard +2 (pure
условия + attach-only-structured), writers +3 (pause/resume/risk в ленте с
формат всего репо clean.

## Шаг 21 — S1.6 offline-приёмка, ремонт миграций, retention (live на ревью)

§10.9.5/6 offline: `tests/unit/application/test_history_acceptance.py` ведёт
синтетическую закрытую сделку реальным путём публикации, затем список,
детали, лента и recovery-уведомление обязаны нести один trade_ref с
одинаковыми qty/ценами/gross; чужой пользователь пуст на всех поверхностях.
§10.9.7: изоляция нотификаций покрыта (`_emit_notification` best-effort,
`test_app_confirmation_isolated_notifier_failure`), crash-completeness —
recovery поверх floor + идемпотентный re-import (acceptance проверяет
неудвоение повторной публикацией).

Ремонт миграций (блокер рестарта, найден в S1.6): 36c55d3 вставил схему
истории в середину списка, поэтому БД, мигрированные до S1 (записан ghost
version 33), пропускали настоящий CREATE TABLE follower_fills и падали на
первом индексе — воспроизведено на копии боевой БД (`no such table`).
Ремонт: entry 34 стал table-ensure `IF NOT EXISTS` (fresh — no-op),
вытесненный индекс времени переехал в entry 51, вся S1-DDL закалена
`IF NOT EXISTS` (на fresh — эквивалентно). Проверено: копия prod-БД
мигрируется (18 applied, remigrate 0), fresh/upgraded схемы конвергируют
полностью. Регрессия: `tests/unit/storage/test_database_upgrade.py`
моделирует ghost-v33 и требует конвергенции.

Retention decision (принято): canonical facts (fills/cashflows/episodes/
events) — без TTL, история пользователя и есть продукт; observations —
обновление на месте, coverage — compaction (как было); outbox — балласт
доставки, а не история: `prune_terminal` удаляет SENT старше 7 дней и
FAILED старше 30 дней, bounded 1000, один раз на старте worker, PENDING/
SENDING никогда; сбой prune не валит старт. Бюджет (замер шага 6): ~659
байт/fill с companions; события ~4–6 на сделку; outbox в steady-state —
только in-flight. Диагностика — существующие `backlog()/capture_state/
coverage` через status/logging, отдельный monitoring stack не требуется.
Счётчик миграций: 52 (0..51).

Live-статус: read-only probe проходит; copyc2-предусловия подтверждены
(HEDGE, flat, 693 USDT). Написан `tests/testnet/test_history_acceptance_
live.py` — полный §10.9-цикл (open→increase→partial→close, импорт реальным
sync+capture путём, сверка gross с биржей, объяснённый net-gap, cross-surface,
рестарт-идемпотентность, failsafe-flatten), но НЕ ЗАПУЩЕН: по указанию
владельца live требует ревью. По пути live-попыток уже исправлены: FK-порядок
(ownership до импорта), запрет future `end_time` (−4181), prod-backed nonces
(−4226 reuse). Открыт вопрос bracket-доказательства capture на исторических
окнах (`cutoff is not bracketed`) — решать в рамках live-ревью, не меняя
инвариант вслепую.

Full offline: **1316 passed / 2 skipped / 45 deselected**. Ruff + format
всего репо clean.

## Шаг 22 — пред-ревью аудит S1 сторонними ревьюерами и фиксы

Шесть параллельных ревьюеров сверили шаги 12–21 с планом (что + как).
Capture-скелет (12) и events/деривация/проводка (13–15): CONFORM, только
NIT (docstring serial-границы уточнён, symbol-mismatch стал явной
ошибкой). Остальные дали 10 must-fix, все подтверждены чтением кода
и исправлены:

- P0 (критичный, шаг 16): `capture_and_publish` публиковал stale-окно
  (`cutoff=window.end`) при свежих снапшотах — bracket невыполним всегда,
  live-публикация невозможна в принципе (наши три live-ошибки — он самый).
  Инвариант `prove_unchanged` не тронут. Фикс в `capture_and_publish`:
  импорт расширяется до `max(window.end, before.response_received)`,
  публикуется `min(complete frontier, after.request − margin)`; быстрый
  импорт ждёт остаток margin один раз (инжектируемый `sleep`) и
  перечитывает `after`; нехватка покрытия — немедленный fail-closed без
  ожидания. Возвращает `(stored, published)`; `run_scheduled` пишет
  checkpoint и eligibility по эффективному концу (заодно чинит
  LIVE_ELIGIBLE для отложенных окон). Тесты: stale-edge регрессия
  (sync получает расширенное окно, слип вызван, конец опубликован),
  wait-then-publish, short-coverage без ожидания, старые ленты без
  изменений. По пути поймано и откачено случайное удаление вызова
  `prove_unchanged` (тесты отказа это доказали).
- S1.4 (4): `_cursor_int` принимает цифровые строки (round-trip «Загрузить
  ещё» чинен, `7x` по-прежнему 400); `risk_preview` с `allow_disabled=True`
  (заглушка вместо отказа); `_cached_permission_ok` включает `whitelist_ok`
  (advisory больше не врёт перед исполняющей проверкой); `executed_qty`
  `str|None` — недоказанный остаток `None`, а не `"0"` (§6.3).
- S1.5 (1): `recover_user_events` листает всю ленту страницами по 200 с
  гардом от непродвигающегося курсора (иначе зрелые аккаунты >200 событий
  молча теряли crash-recovery, нарушение B8). Регрессия 204+1. По пути
  найден и вылечен висяк: стаб, игнорирующий `after_seq`, крутил sweep
  бесконечно — стаб обязан чтить курсор.
- Protection (1, половина репорченного): `protection_canceled_before`
  требует `c.status='CONFIRMED'` как обещает docstring (неподтверждённый
  cancel больше не стирает baseline). Вторая половина репорта отклонена
  после проверки: fold обязан верить CONFIRMED-команде, а не статусу
  наблюдения (stale NEW скрыл бы настоящий UNCONFIRMED) — зафиксировано
  здесь, код не менялся.
- S1.6 offline (2): acceptance ведёт полный цикл open→increase→partial→
  close четырьмя fills и явно фиксирует single-shot batching
  (только OPENED+CLOSED; INCREASED/REDUCED — multi-publish continuation,
  unit-покрыта); FAILED-retention закреплена границами 10d/40d.
- Live-файл (offline-часть): `notify_eligible=True` на capture (иначе
  recovery пуст после P0-фикса), распаковка `(stored, published)`,
  отбор fills по `order_id` вместо qty, assert single-count комиссий
  (COMMISSION income vs fill-комиссии, USDT), foreign spot-check §10.9.6,
  net-assert смягчён под будущую атрибуцию.

Отклонено/отложено с обоснованием: сортировка ленты по seq вместо
`(occurred_at, seq)` §11.2 (иначе backfill вклинивается в открытые
страницы — правится формулировка плана, не код); fills целиком в
`trade_events` (малый объём, NIT); since_ms в курсоре, общий предикат
freshness, settled-placeholder до закрытия, clamp cap_start, отбор fills
Live-часть G1 остаётся: обход журнала команд (§10.9.1), подмена рестарта,
SL/UNKNOWN/open-start/backfill — требуют живого прогона, запрещённого до
ревью; live-файл к нему готов.

Full offline: **1322 passed / 2 skipped / 45 deselected**. Ruff + format
всего репо clean.

## Зафиксированный milestone перед продолжением работы

Состояние после шага 22 (аудит пройден, фиксы влиты, live ждёт ревью):

- S1.1–S1.2 foundation: без изменений, приняты ранее.
- S1.3 core+integration: аудит CONFORM (capture, events, деривация,
  проводка) + P0-фикс live-edge capture (импорт до живого края, cutoff =
  min(frontier, after − margin), одно ожидание margin, checkpoint и
  eligibility по эффективному концу). Cancel-гейт baseline требует
  CONFIRMED. Тесты истории 134 passed.
- S1.4: аудит DIVERGE→исправлено (курсор round-trip, preview отключённым,
  whitelist в advisory, executed_qty None≠"0").
- S1.5: аудит DIVERGE→исправлено (recovery листает всю ленту, гард
  курсора). Кнопка dormant, тексты/замена menu — S3.
- S1.6 offline: acceptance ведёт полный цикл и фиксирует single-shot
  batching; FAILED-retention 10d/40d; live-файл hardened offline
  (order_id-фильтр, single-count комиссий, foreign spot-check).
  Live-прогон и SL/UNKNOWN/open-start/backfill — только с ревью.
- S2/S3 не запускались. G1/G2/G3 не закрыты.

Следующий шаг — ревью настоящего diff, затем запуск live §10.9 и вердикт G1.

## Шаг 23 — независимое ревью реализации S1 (2026-09-06)

Проверены четыре коммита `4f1a86a..f77a3fd`, повторены offline suite и ruff;
локальные probes на временной SQLite подтвердили ошибки, не покрытые suite.
Полный отчёт: [MINIAPP_S1_REVIEW_2026-09.md](MINIAPP_S1_REVIEW_2026-09.md).
Результаты проверок — в ENGINE_TEST_STATUS, без изменения самой suite.

**Актуальный verdict: REQUEST CHANGES; S1/G1 не принят.** Формулировки
предыдущего milestone «аудит CONFORM / всё исправлено / дальше только live»
описывают прежний внутренний проход и не являются текущим acceptance verdict.

Обязательный остаток: отбор fills по allocations/alias, стабильные refs при
rolling window и open-start/backfill, корректные события при batch capture,
HEDGE protection, подключённые known costs/issues, recovery без потери хвоста,
реально dormant trade keyboard, snapshot-bound feed. Live harness содержит
ошибку распаковки list_closed, неверное требование положительного PnL и неполный
cleanup; он пишет nonce в configured DB и не проверяет настоящий restart/journal.
Утверждения «prod-БД не трогает» и «live-файл готов» отзываются этим уточнением.

Дальше: исправления R1–R9 с компактными regression tests, численный итоговый
capacity/retention budget и проверка worker lag, затем исправленный production
testnet сценарий §10.9. Live в этом review не запускался. Основной код не менялся;
обновлены только документация и статусы roadmap. S2/S3 и device smoke шкалы
остаются в прежнем объёме.

## Шаг 24 — архитектурный план исправлений review (2026-09-06)

По запросу владельца в [основной план](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md)
добавлен §23: C1–C9 с точными границами модулей, предлагаемыми методами/DTO,
storage changes, transactional boundaries и порядком PR. Описаны стабильные
replay boundaries/lineage, allocation index и pagination, transitions вместо
diff конечного состояния, HEDGE protection, финансовый read path, отдельный
delivery cursor, выключенные до rollout capabilities, feed high-water,
worker/retention и исправленный live harness.

Это **документирование предстоящих изменений**, код не реализован. R1–R9,
capacity/retention и G1 остаются открытыми. Обычная архитектура single-process /
SQLite и три stages сохранены. Новые tests/миграции появятся при исполнении
соответствующих C-пакетов; в этой правке проверяются документационные ссылки.

## Шаг 25 — исполнение C1–C9, live-прогон и его разбор (2026-09-07)

C1 (коммит `b2fbf73` + миграции `0c1c392`): ImportDelta + facts_revision в
import-транзакции, ProjectionBoundary origin/latest, choose_replay_range,
Span для импорта/реплея (Window остался биржевым ≤7d), partial вместо
require_settled, end-state seeds в границе, alias/split-коррекции атомарно,
тихий rebuild_scope. Батарея `test_replay.py` (R2: rolling, 7d-passage,
late-fill откат с перевыводом seeds, alias/split, silent rebuild).

C2 (`7efe69a`, сабагент): allocation index (миграция 55), fills строго по
аллокациям, resolve_owned_ref, раздельные fills/events курсоры,
history_page_changed. C3 (`fb6acac`, сабагент): transitions при применении
аллокаций, событие на смысловую аллокацию, dedup scope+fill+seq,
HISTORY_CORRECTED (миграция 56 + tuple-runner), per-event age/floor.
C4 (`d107b60` + хук): leg-keyed protection из команд журнала,
ENTRY_SKIPPED через `Coordinator.set_entry_skipped_hook` (вшит в
`build_stage2_composition`), CONNECTIVITY по переходам адаптера.

C5 (`4432590`, сабагент): load_episode_money, (symbol,trade_id) ключи,
proof-driven net с именованными gaps. C6 (`c67e52a`, сабагент):
user_event_delivery_state (миграция 57), high-water sweep одной
транзакцией, rollout floor, trade_links_enabled=False. C7 (`489d0c3`,
сабагент): v1 feed-курсор, high_water snapshot, newest-first, banner.

C8 (`09104a8`): split dirty_import/dirty_projection, fills→capture chaining,
ImportDelta.requests + snapshot-счётчик сквозно, no-op fall-through в тике,
stats(), часовой maintenance tick outbox, бенчмарк полной схемы:
30 аккаунтов × 1000 fills — publish p50 538ms / p95 552ms на аккаунт,
60 snapshot reads; прогноз при 100 акков × 20 fills/день: 30d 0.13GB,
90d 0.39GB, 365d 1.56GB, порог места 1.0GB. Оговорки: синтетика (1ms fills,
без сети, margin 1ms); тик 5s/1-job доминирует в проде, mitigated no-op
пропуском.

C9 (`bf1ee2a`): харнес переписан на Executor/journal (per-step
operation_id — одинаковые payloads делят journal key!), journal-coid
сверка с биржей, distant STOP place+cancel, post-send-timeout
query-before-retry close, REAL рестарт файлом, HTTP по HMAC на всех
поверхностях, per-leg reduceOnly failsafe, exclusivity guards.

Live-прогон `CT_TESTNET=1` (96s): FAIL, и это две настоящие находки.
1. copyc2 делят: чужие fills `ct+32hex` (BUY 01:04:03, SELL 01:04:21) во
время нашего окна + 0.004-раунды за 15–35 мин до старта; наш wait(0.001)
прошёл по чужой позиции. Exclusivity precondition из §23.10 не был
проверен до запуска — больше без него не гонять.
2. Баг харнеса (не ядра): open-a/open-b с одинаковым payload получили один
journal key — open-b стал тихим no-op с чужим CONFIRMED. Исправлено
per-step operation_id + сверка journal-coid↔биржа после каждого submit.
Ядро (dedupe по ключу перехода) работало правильно.

Счёт: offline **1428 passed / 2 skipped / 45 deselected**, ruff + format
чистые, миграции 0..57. G1 остаётся открытым: нужен эксклюзивный прогон
исправленного харнеса.

## Шаг 26 — live §10.9 PASS, G1 evidence собран (2026-09-07)

Прогоны 6–8 доводили харнес и один инвариант ядра: STOP-сверка по journal
key (`e00e564` до этого), пустой replay при подросшем фронтире возвращал
`[]` — сначала чинилось не тем диапазоном (replay-срез вместо
запрошенного окна), затем перекрытие взято от исходного окна capture
(`e00e564`): пустая публикация отчитывается `list_range` запрошенного
окна; офлайн-регрессия `test_recapture_after_frontier_growth_returns_
stored_episode`. Прогон 8: **1 passed за 26.56s** — полный цикл
open→increase→STOP place+cancel→partial→close(UNKNOWN→query-before-retry)
через Executor/journal, импорт, один closed episode, gross == бирже,
комиссии single-count, рестарт пересозданием с тем же ref, HTTP (HMAC) на
всех поверхностях, recovery `open_trade`, foreign пуст. Аккаунт flat,
ордеров нет. Контест второй ветки к моменту прогона остановлен,
exclusivity-гарды прошли.

## Шаг 27 — независимая перепроверка S1 по частям (2026-09-07, без live)

Шесть reviewer-агентов параллельно, только offline (live не трогали —
эксклюзивное окно чужой ветки). Все части — correct, MUST-FIX нет:
- S1.1 capture: 67/67; per-key Lock, margin-ожидание однократное +
  fail-closed, checkpoint только опубликованным end, replay к origin,
  вырожденный replay>cutoff без краша (NOTE: прямой тест ветки
  Span(x,x) отсутствует — покрыт соседний случай e00e564).
- S1.2 store: 40/40 + 20 смежных; first-wins, scope-изоляция, seq-курсоры
  1..200, атомарный publish, allocation-fills, миграция 56 без потерь.
- S1.3 деривация: 72/72; transitions при apply, dedup scope+fill+seq,
  leg-keyed protection из CONFIRMED, net=None без proof, single-count
  комиссий (NOTE: мёртвое pro-rata-поле flip-close transition —
  на события не влияет).
- S1.4 reads: 93 + 441 application/miniapp; allocation-пагинация,
  ownership везде, стабильные курсоры, cached-only preview,
  resume-контекст только RESUME, Decimal без float в деньгах.
- S1.5 notify: 48 + 40 + 10 + 1; durable high-water, кнопка dormant до S3,
  rollout floor, retention только терминальных.
- S1.6 offline: acceptance 4 passed (один trade_ref, чужой пуст),
  ghost-v33 converge, миграции честно append-only (58 entries 0..57),
  suite 1429/2/45 воспроизведён, бенчмарк smoke консервативен.
Сквозная: ruff check/format чистые, doc links OK (48 файлов).
Файлы ревьюера (UX_PLAN/S1_REVIEW/docs/AGENTS) не трогал — их ветка.

## Шаг 28 — повторное ревью G1 (2026-09-07)

Проверены C1–C9 на `9a0dd98`, включая production wiring и последовательные
публикации. Вердикт **REQUEST CHANGES, S1/G1 OPEN**. Выводы шагов 26–27 о
полном закрытии G1 заменены этим verdict; сами записи о проведённых проверках
сохранены как история. Live этим review не запускался, рабочая БД не затрагивалась.

На временных SQLite воспроизведены: fresh scheduled capture с неверным start=0;
три разных ref и потеря entry totals при трёх продолжениях одного эпизода;
потеря отдельно импортированного late roundtrip; обрезание details events после
200; недоставка 1001-го события через default recovery после повторного boot.
В live harness статически подтверждена отмена STOP по пользовательскому label
вместо journal-generated clientOrderId. Также отсутствует SL fill и capture
между изменениями позиции; записанный PASS этих случаев не проверяет.

R4/R5/R7/R9 приняты в пределах исходных findings. R1/R2/R3/R6/R8 требуют
доработки N1–N6. C8 storage forecast полезен, но прямой capture benchmark
не измеряет latency scheduler queue. Offline suite, ruff и форматирование
перепроверены; актуальные счётчики только в ENGINE_TEST_STATUS.

Полный отчёт, точки кода, probes и критерии:
[MINIAPP_S1_REVIEW_2026-09.md](MINIAPP_S1_REVIEW_2026-09.md), повторный проход.
Конкретные архитектурные исправления дополнены в §24
[плана](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md). Roadmap и текущий TL;DR
синхронизированы с OPEN. Исправления runtime-кода в этом review не вносились.

## Шаг 29 — ограниченный corrective handoff реализатору (2026-09-07)

По запросу владельца подготовлен самостоятельный
[FIX_HANDOFF](MINIAPP_S1_FIX_HANDOFF_2026-09.md): почему G1 не принят,
архитектура по файлам, regression matrix T1–T6, команды проверок и шаблон
фактического закрытия. Уточнён масштаб: обязательные для G1 — N1–N3;
остальные находки имеют сроки перед использованием соответствующей функции.
Дополнительный общий review и 100-account campaign не требуются.

Это передача задачи, не реализация. Runtime-код и тесты не изменены;
счётчики suite прежние. После исполнения §6 реализатор сам вносит evidence
§7 и закрывает G1, не запрашивая повторный вердикт автора. Live SL fill/STOP
evidence остаётся до production rollout новой истории, совместить с G3.

## Шаг 30 — corrective N1–N3, G1 CLOSED (2026-09-07)

Дата / ветка / tested commit: 2026-09-07 / `feature/frontend` /
`84cabfb01c2c0febb4dc1327cd565b922392f284` (`84cabfb`).
Scope: только обязательный corrective N1–N3 по
[MINIAPP_S1_FIX_HANDOFF_2026-09.md](MINIAPP_S1_FIX_HANDOFF_2026-09.md),
масштаб 10–30 аккаунтов. Новый общий аудит, переработка движка и 100-account
campaign не выполнялись и не являются этим gate.

Изменённая архитектура: первый scheduled bootstrap теперь начинается с начала
непрерывного complete coverage интервала нужного symbol, содержащего frontier;
failed legacy checkpoint `start=0` без accepted origin не может выдумать coverage
от epoch. Продолжение открытого episode replay-ится от accepted origin с его
сохранённым seed, а late correction отдельно разрешает re-derive origin seed.
Уже выданный `public_ref` проходит scope/leg resolver и не переиспользуется как
новый anchor. Импорт хранит `facts_revision` + durable `dirty_from_ms`;
publication в одной SQLite-транзакции CAS-ит facts revision и boundary version,
записывает episodes/allocations/aliases/events/boundary и только после принятого
frontier очищает покрытый dirty. Migration **58** append-only добавляет marker и
консервативно помечает существующие facts/complete coverage, включая scopes без
старой `history_scope_state` строки; существующие facts/refs/coverage не удаляет.

**N1: CLOSED.** До исправления fresh scheduled capture на review/baseline мог
выбрать `start=0` при наличии только недавнего complete coverage и упасть на
coverage gap; failed checkpoint закреплял тот же неверный hint. После исправления
`tests/unit/history/test_capture.py::test_corrective_g1_t1_scheduled_bootstrap_uses_continuous_symbol_coverage`
проводит обычный import → реальный `run_scheduled`, проверяет origin от BTC
coverage start, игнорирование более широкого unrelated ETH coverage и вариант
failed checkpoint `start=0` для второго account.

**N2: CLOSED.** До исправления continuation от `latest.cutoff+1` восстанавливал
только quantity seed, терял старый entry basis/allocations и мог зарезервировать
старый UUID как новый anchor: baseline probe давал новый ref и stale open row.
После исправления
`tests/unit/history/test_replay.py::test_incremental_open_increase_partial_close_keeps_one_ref_and_full_basis`
проводит четыре отдельных scheduled capture: BUY 2@100 → BUY 1@130 → SELL 1@140
(`realized=10`) → SELL 2@140 (`realized=60`). На всех шагах один canonical ref;
итог `entry_qty=3`, `average_entry=110`, после partial `open_qty=2`, финально
`exit_qty=3`, `average_exit=140`, gross `70`, четыре allocations, одна closed row,
stale open rows отсутствуют. `test_incremental_open_episode_ref_survives_restart_and_matches_batch`
перезапускает DB между increase и partial, сравнивает incremental с независимой
batch projection и доказывает no-op без дублей events/allocations.
`test_open_frontier_replays_from_origin_without_rederiving_seed` отдельно фиксирует
разницу normal open continuation и late correction, включая отсутствие нового
ref reservation.

**N3: CLOSED.** До исправления `changed_from` жил только в текущем `ImportDelta`:
отдельный late BUY/SELL после уже принятого flat frontier терялся после restart,
а stale projection мог пройти между чтением facts и publication commit. Теперь
`tests/unit/history/test_capture.py::test_corrective_g1_t3_dirty_survives_restart_and_same_frontier_noop_import`
сохраняет late roundtrip, reopens SQLite, получает пустой следующий import delta
при том же frontier и всё равно запускает scheduled capture; появляется ровно
один closed episode, dirty очищается только publication, следующий pass — no-op.
`tests/unit/history/test_episodes.py::test_corrective_g1_t4_publication_rejects_facts_revision_race`
вставляет новый fact прямо между projection read и real publish: stale commit
отклонён, dirty остаётся, retry публикует новую revision. Отдельный
`test_corrective_g1_t4_publication_failure_rolls_back_all_visible_state` роняет
boundary write внутри transaction и проверяет отсутствие половины episode /
allocation / event / boundary. Coverage symmetric-diff и upgrade закреплены
`tests/unit/history/test_storage.py::test_corrective_g1_t5_coverage_gap_and_loss_mark_earliest_dirty`
и `tests/unit/storage/test_database_upgrade.py::test_corrective_g1_t5_v57_upgrade_marks_retained_history_dirty`.

**T1–T6:** focused corrective matrix через реальные repositories/capture/service:

```text
T1  tests/unit/history/test_capture.py::test_corrective_g1_t1_scheduled_bootstrap_uses_continuous_symbol_coverage
T2  tests/unit/history/test_replay.py::test_incremental_open_increase_partial_close_keeps_one_ref_and_full_basis
T2b tests/unit/history/test_replay.py::test_incremental_open_episode_ref_survives_restart_and_matches_batch
T3  tests/unit/history/test_capture.py::test_corrective_g1_t3_dirty_survives_restart_and_same_frontier_noop_import
T4  tests/unit/history/test_episodes.py::test_corrective_g1_t4_publication_rejects_facts_revision_race
    tests/unit/history/test_episodes.py::test_corrective_g1_t4_publication_failure_rolls_back_all_visible_state
T5  tests/unit/history/test_storage.py::test_corrective_g1_t5_coverage_gap_and_loss_mark_earliest_dirty
    tests/unit/storage/test_database_upgrade.py::test_corrective_g1_t5_v57_upgrade_marks_retained_history_dirty
T6  tests/unit/application/test_history_acceptance.py::test_all_surfaces_share_one_ref_and_facts
    tests/unit/application/test_history_acceptance.py::test_foreign_user_sees_nothing_on_any_surface
```

Focused набор (`test_capture.py`, `test_replay.py`, `test_storage.py`,
`test_episodes.py`, application history acceptance и database upgrade) —
**80 passed**. T6 использует те же T2 totals: один owner `trade_ref`, qty 3,
average entry 110, exit 140, gross 70 на list/detail/feed/outbox; foreign scope
пуст/denied по существующему контракту.

Полный §6 gate **точно на tested commit `84cabfb`**:

```text
PYTHONPATH=src:. .venv/bin/pytest -q -m 'not testnet'
  1439 passed, 2 skipped, 45 deselected
.venv/bin/ruff check .
  PASS
.venv/bin/ruff format --check .
  PASS (274 files)
python3 scripts/check_doc_links.py
  PASS (49 files scanned)
git diff --check
  PASS
```

Три независимых blocker-only re-review текущего corrective дерева также не нашли
оставшихся блокеров N1, N2 или N3; это дополнительная проверка, а не новый общий
acceptance cycle.

**N4: DEFERRED до S3 details.** Известное ограничение pagination семейства events
после первых 200 записей не исправлялось этим corrective; перед включением details
нужен repository-level `after_seq` / owned alias family paging test на 201 event.

**N5: DEFERRED до event-delivery rollout.** Старый default recovery path при
`False` всё ещё не считается корректно отключённым для user-event sweep и имеет
известный capped сценарий >1000. Новый event delivery не включать до N5; durable
consumer C6 остаётся отдельным уже существующим механизмом.

**N6: DEFERRED до следующего signed live harness.** До следующего запуска harness
обязан отменять именно journal-generated placement `clientOrderId`, затем cleanup
защитных orders и финально доказывать `flat + open_orders=[]`. Этот corrective
live не запускал и не объявляет старый user label правильным target.

Live: новый signed run не выполнялся и для §6 не требовался. Existing 26.56s PASS
сохранён как evidence прежнего сценария, но не превращён в доказательство N6 или
фактического SL fill. До production rollout новой истории вместе с G3 остаются
фактический SL fill и STOP cleanup evidence. Отдельный 10–30 worker smoke полезен,
но не является обязательным §6 gate; 100-account load campaign намеренно не
повторялась.

**Вердикт: G1 CLOSED по §6.** Повторный общий verdict автора ревью не требуется.
Следующее действие — продолжить S2 по общему плану, сохраняя сроки N4–N6 выше.

## Шаг 31 — S2.1–S2.6 / G2 CLOSED (2026-09-07)

S2 выполнен на текущем четырёхвкладочном UI без S3-редизайна. Один
session-scoped `MiniAppResources` владеет Dashboard/Positions/Risk/Notifications/
Status/AccountBuilder DTO, weak/strong singleflight, read/write/invalidation
generations, scope disposal и mutation locks. `api.ts` держит timeout через чтение
body, снимает внешний abort-listener и fail-safe отклоняет invalid JSON. Root
keep-alive сохранён; Home/Settings и Home/Positions делят ресурсы, foreground/TTL
сведены к одному lifecycle owner. Settings prefetch стартует только после входа,
не более двух weak reads одновременно; cached nested reopen не показывает skeleton.

Финальный corrective после трёх blocker-only аудитов закрыл прямые hard-session
границы и unknown-outcome semantics: connected `401`/`invalid_session` из shared
reads и Activity/Risk/Notifications/Home/AccountBuilder уничтожает owner и возвращает
session gate; ambiguous Risk/Notifications/pause/resume/reauthorize/disconnect не
повторяет mutation автоматически, а сначала делает authoritative reconciliation.
Поздние аудиты добавили causal fence и симметричный retained-unknown lifecycle для
всех mutation owners: Risk/Notifications/pause-resume/AccountBuilder перед recovery
инвалидируют свой resource, поэтому pre-mutation flight не может быть принят за
post-write truth; при необходимости выполняется один новый post-boundary follow-up.
Double-unknown остаётся fail-closed под session mutation lock через Back/remount и
снимается только более поздним authoritative evidence либо сменой scope, без blind
replay.
Settings stale Dashboard больше не выглядит fresh green; Activity получает app
foreground edge; Agent expiry, modal Back order, nested scroll/focus и dirty Risk
policy conflict закреплены regressions.

Frontend gate на current S2 WIP (base HEAD `b10f01a`): **27 test files / 316
passed**, oxlint **0 warnings/errors**, `tsc -b && vite build` PASS,
`git diff --check` clean. Финальный production artifact: main JS **336.56 kB raw /
96.46 kB gzip**, CSS **19.42 / 4.41 kB**.

Recorded production-bundle evidence на engine preview (`copyc1`, Headless Chrome
152, 390×844): cold startup **4 read POST**; warm root 160 samples p95 **1.0 ms**,
warm cached nested 120 samples p95 **1.9 ms**, при искусственных 300 ms network
latency warm root p95 **6.0 ms** — во всех трёх warm сериях **0 API reads**.
Settings speculative peak **2**. 50-cycle browser lifecycle: visible DOM
**319→319**, `Memory.getDOMCounters` documents/nodes/listeners
**1/443/160 → 1/443/160**, post-GC heap **+3.25%**, global listeners **4→4**,
timers/intervals/rAF **0**, long tasks >50 ms **0**, page errors **0**.

Эти browser latency/DOM/heap числа относятся к G2 measurement run до финальных
post-audit correctness-only corrective; current tree после них повторно прошёл
полный automated gate/build, но новый CDP performance run здесь не заявляется.

Explicit signed `status(refresh=true)` через тот же testnet application path:
5 reads **1.45–2.21 s**, median **2.05 s**, `age_seconds=0`, Aster `ok`, permissions
valid. Cached UI поэтому измеряется отдельно от strong verification и не выдаёт
старое evidence за fresh. Полный журнал архитектуры, regressions и measurement:
`MINIAPP_S2_PROGRESS_2026-09.md`.

После первоначального post-fix review выполнен отдельный трёхчастный аудит новыми
review agents по transport/resources, shell/lifecycle и Settings/performance. Его
corrective ledger и финальные verdicts — `MINIAPP_S2_REVIEW_HANDOFF_2026-09.md`.

**Вердикт: S2/G2 CLOSED.** Физические Telegram WebKit/haptics/snap-stop и итоговая
device acceptance остаются в S3.5/G3 по уже принятому разделению этапов; S3 можно
начинать. N4/N5/N6 сохраняют сроки corrective G1 и этим gate не переопределяются.

## Шаг 32 — S3 WIP: Home, history, Activity и Settings (2026-09-07)

Начат S3 на базе `41fba43`. Реализованы рабочие срезы overview Home,
Open/Closed/Trade detail, bounded session history resources, structured Activity
feed, owned launch intent и display-контракты Settings/Risk/permissions. На
backend добавлены alias-family `after_seq` paging для N4, 251-event regression,
event presenter text и удалён default capped user-event recovery scan в
`Stage2Process`. Текущий TODO, архитектура и ограничения вынесены в
`MINIAPP_S3_PROGRESS_2026-09.md`; G3 не закрыт.

Промежуточные проверки: targeted backend **59 passed**, TypeScript/Vite build
**PASS**, oxlint **PASS с 2 предупреждениями**, полный frontend Vitest **272
passed / 44 failed** из-за ещё не обновлённых legacy S2 contract/lifecycle
assertions. Продолжение: сначала frontend test/contracts и S3.2/S3.3 rollout,
затем offline/browser/live evidence по TODO в S3-журнале.

### S3: остаток реализации передан на отдельную проверку (2026-09-08)

Функциональные S3.0–S3.4 реализованы: завершены menu/push/feed consistency,
production durable delivery, owned links/split choices и detail edge states.
S3.5 документация готова; проверка отложена по просьбе владельца. G3 OPEN,
нового offline/browser/live evidence нет. Источник текущего состояния —
[журнал S3, §16](MINIAPP_S3_PROGRESS_2026-09.md) и
[передача тестирования](MINIAPP_S3_TESTING_HANDOFF_2026-09.md).

## Итоговая UI acceptance-сводка (2026-09-09)

Фактические ручные проверки Mini App записаны в append-only разделах S2/S3:
Telegram Desktop WebView — S2-only revalidation и S3 history/detail/Activity/
Settings/Home flows; Chromium/Computer — S3 browser pass с ошибочными состояниями
и 17 screenshots; исторический owner iPhone pass P2.0–P2.7 — в
`STAGE3_PART2_PROGRESS_2026-09.md` и `acceptance-p27/`.

Список frontend `*.test.ts(x)`, хронология добавления тестов, последний
автоматический gate, артефакты и G3 OPEN собраны в
[MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).

## Финальный verdict S3/G3 (2026-09-09)

После предыдущей промежуточной сводки выполнены свежий §19.7 performance trace,
controlled error/history matrix, Telegram Desktop и iPhone Mirroring проходы,
а также signed testnet N6 с реальным `STOP_MARKET` fill и чистым cleanup.
Android по решению владельца исключён из scope и verdict.

**S3/G3 CLOSED в согласованном scope.** Текущая карта evidence —
[MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md),
детали — [MINIAPP_S3_PROGRESS_2026-09.md](MINIAPP_S3_PROGRESS_2026-09.md) §§19–26.
