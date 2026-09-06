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
