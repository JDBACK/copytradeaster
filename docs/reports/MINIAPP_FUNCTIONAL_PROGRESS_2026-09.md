# Реализация функционального Mini App — 2026-09-06

Источник требований: [план](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md), §14.
Кампания IN PROGRESS. G1/G2/G3 открыты. Реализация небольшими проверяемыми шагами.

## Рабочий todo

- [ ] S1.1: typed Aster reads, signed read evidence, учётная политика и units.
- [ ] S1.2: schema/repository, observations, bounded sync, retention и замер роста.
- [ ] S1.3: episodes, partial/flip, cashflows, stable refs, user events.
- [ ] S1.4: application/HTTP contracts, ownership, aggregate, settings/scope/freshness.
- [ ] S1.5: общий presenter, durable structured notifications.
- [ ] S1.6 / G1: offline + signed acceptance по §10.9.
- [ ] S2: baseline, session resources, transport/lifecycle, nested settings cache, G2.
- [ ] S3: Home summary, Open/Closed/detail, activity/deep links, settings/copy, G3.
- [ ] После основы: независимый review выполненной шкалы и исправление найденных дефектов.
- [ ] Финал: Telegram smoke шкалы и новых flows, screenshots, performance acceptance.

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
Не обещать, что два одинаковых snapshot без полной истории сами доказывают net.
