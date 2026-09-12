# Гайд ревьюеру: ручное закрытие follower и зеркалирование режима маржи

Дата: 2026-09-13. Исходная база сравнения: `main` / `cf29412`; первый вариант
закреплён baseline-коммитом `e6f0d0f`. Следующий corrective описан в конце
этого гайда; он уточняет устаревшие формулировки выше. Scope ревью — два
поведения торгового движка, а не Mini App или mainnet-допуск.

## Что должно измениться для пользователя

1. Если follower полностью закрыл свою позицию в Aster, а master всё ещё держит
   ту же сделку, follower остаётся flat до новой master-сделки. Периодическая
   сверка, следующее событие master и рестарт процесса не должны открыть её
   заново. Остальные followers продолжают копирование.
2. Для нового риска follower должен повторить режим маржи master:
   `CROSSED` или `ISOLATED`. В isolated прежний денежный риск до Stop сохраняется,
   а плечо ограничивается так, чтобы начальный залог был не меньше двойного
   расчётного убытка до Stop с fee-buffer. Невозможное или неподтверждённое
   переключение режима запрещает вход; сокращение/закрытие/защита продолжаются.

## Куда смотреть в diff

| Участок | Существенное изменение |
|---|---|
| [`domain/models.py`](../../src/copytradeaster/domain/models.py), [`aster/models.py`](../../src/copytradeaster/aster/models.py) | Нормализация `CROSS/CROSSED/ISOLATED`, режим и `isolatedMargin` в состоянии, `SET_MARGIN_TYPE`, отдельная причина отказа от re-entry. |
| [`master/projector.py`](../../src/copytradeaster/master/projector.py), [`trading/coordinator.py`](../../src/copytradeaster/trading/coordinator.py) | Авторитетный master `positionRisk` задаёт режим; полный follower REST snapshot сохраняет режим даже flat-строк. Здесь находятся sizing, блокировка роста, post-fill проверки и детектор внешнего закрытия. |
| [`trading/planner.py`](../../src/copytradeaster/trading/planner.py), [`trading/executor.py`](../../src/copytradeaster/trading/executor.py), [`aster/trading_api.py`](../../src/copytradeaster/aster/trading_api.py) | Цепочка `SET_MARGIN_TYPE → SET_LEVERAGE → MARKET_OPEN`, journal до config POST, подтверждение signed чтением и boot recovery. |
| [`storage/database.py`](../../src/copytradeaster/storage/database.py), [`storage/repositories.py`](../../src/copytradeaster/storage/repositories.py) | Миграция #60 и episode-bound durable observation/latch в `sync_basis`; per-action `trade_id` связывает подтверждённое закрытие движка с master episode. |
| [`notifications/formatter.py`](../../src/copytradeaster/notifications/formatter.py), [`notifications/trade_cards.py`](../../src/copytradeaster/notifications/trade_cards.py) | Причина запрета повторного входа и причины отказа по режиму доступны для уведомления. |

## Инварианты, которые следует проверить

**Внешнее закрытие.** Coordinator сравнивает две полные авторитетные REST-картины
follower (`balance`, `openOrders`, `positionRisk`) только при активном
`copy_trades.trade_id`. Переход `non-zero → flat` не должен ставить внешний latch,
если новая подтверждённая `MARKET_CLOSE` движка покрывает предыдущий наблюдённый
объём либо связанный bot-owned Stop/TP доказан как `FILLED`. Частичный
engine-close, старый cursor или недоступная причина не объясняют полный flat:
случай классифицируется `EXTERNAL_OR_UNKNOWN` и блокирует вход в этот episode.
Неполный REST-read не обновляет observation. На заблокированном flat planner
отменяет оставшиеся bot-owned Stop/TP, не снимая latch. Новый master `trade_id`
сбрасывает запрет и observation baseline; обычная повторная сверка — нет.

**Режим маржи.** WS служит триггером; режим берётся из signed `positionRisk`.
Противоречащие HEDGE-строки или неизвестный режим master запрещают новый риск.
Если на символе занята любая HEDGE-нога, смена режима и увеличение запрещены;
для плоской ноги с orphan-защитой остаётся zero-target на её отмену. На плоском
символе planner выставляет режим, затем плечо и только после подтверждения
открывает позицию. FAILED/UNKNOWN конфигурационной команды блокирует зависимый
`MARKET_OPEN`; close/cancel от неё не зависят. У config POST нет order COID:
неизвестный исход разрешается новым signed `positionRisk`, без слепого повтора.
`-4046` означает уже установленный режим, но executor всё равно проверяет
фактическое состояние. `-4168` при Multi-Assets остаётся биржевым отказом,
production не переключает account-wide режим автоматически.

Для isolated pre-entry потолок плеча равен
`floor(expected_entry / (2 × |expected_entry − Stop| × (1 + fee_buffer)))`
в дополнение к существующему liquidation/operator cap. Если даже `1x` не
обеспечивает запас, вход запрещён. После фактического fill проверяются режим,
реальный `isolatedMargin ≥ 2 × |qty| × |entry − Stop| × (1 + fee_buffer)` и
расположение liquidation относительно Stop; нарушение закрывает ногу защитно.
Это риск до планового Stop, а не гарантия цены исполнения при гэпе.

## Фактическая проверка на Aster Testnet

Обязательное доказательство здесь — signed Testnet, не fake exchange. В
эксклюзивном окне старый `run-bot` временно остановлен через `SIGSTOP`, тесты
использовали настоящие `main/copyc1` ключи и свежую SQLite для journal/episodes.
Целевые сценарии в [`test_one_way_engine_e2e.py`](../../tests/testnet/test_one_way_engine_e2e.py)
прошли вместе: **2 passed за 173.23s** (2026-09-12).

- External-close: прямой signed reduce-only close follower, несколько
  reconcile без re-entry, новый Coordinator на той же SQLite и повторный вход
  только после новой реальной master episode.
- Margin-mode: оба перехода (`ISOLATED`, затем `CROSSED`), реальные
  master/follower позиции и Stop, порядок команд в journal, отсутствие
  дубликатов после reconcile и `UNKNOWN → CONFIRMED` по signed биржевому чтению.
  В isolated Aster вернул `15x`, `isolatedMargin=5.19196191` при требовании
  `4.64043580`, `liquidationPrice=72350.05795489` при Stop `74973.10`.

Полный ONE_WAY engine cycle также прошёл в отдельном live-запуске. В парном
запуске с ним старый HEDGE fixed-protection тест сначала упал: synthetic
`MasterState` не задавал обязательный `margin_type`, и новый fail-closed gate
корректно не открыл follower. После исправления fixture HEDGE resize обеих
ног прошёл отдельно: **1 passed за 61.07s**. Это не доказательство полного
margin-mode mirror в HEDGE; оно проверяет совместимость прежней fixed-quantity
защиты с новым обязательным полем master.

Тесты вызывают production `Projector → Coordinator → Planner → Executor` и
реальные signed REST операции, но master-события/сверки ведёт harness, а не
фоновый WS `App`. Оба новых сценария завершились cleanup; независимый signed
read-only аудит после последнего запуска показал на всех пяти REAL-аккаунтах
`positions=0`, `open_orders=0`, `AUDIT: FLAT-ALL`. После `SIGCONT` bot вновь
достиг `READY`. Подробный журнал —
[`MARGIN_MODE_MIRROR_2026-09.md`](MARGIN_MODE_MIRROR_2026-09.md), продуктовый
контракт — [`EXTERNAL_FOLLOWER_CLOSE_POLICY.md`](../EXTERNAL_FOLLOWER_CLOSE_POLICY.md).

## Регрессии и границы решения

Старый тест, который требовал повторный вход после ручного close, заменён на
обратный integration-регресс; unit/integration матрица покрывает episode/restart,
partial engine-close, собственный protective fill, dust, неполный snapshot,
HEDGE conflict, planner dependency и boot UNKNOWN resolver. Вспомогательный
offline gate не заменяет подписанные проверки выше. Актуальная точка счётчиков
и live verdict — [`ENGINE_TEST_STATUS_2026-08.md`](../ENGINE_TEST_STATUS_2026-08.md).

Публичного same-episode re-arm пока нет: после ручного close нормальный путь —
новая master-сделка. Liquidation/ADL попадают в безопасную общую классификацию
`EXTERNAL_OR_UNKNOWN`, без отдельной пользовательской причины. Production не
меняет Multi-Assets и не добавляет `positionMargin`. Полный HEDGE mirror
`CROSSED↔ISOLATED`, mainnet и реальное исполнение Stop при гэпе в этом
Testnet-пакете не сертифицированы. Существующий liquidation guard не считает
`liquidationPrice=0` доказательством нарушения; isolated-позиция всё равно
обязана пройти отдельную проверку фактического `isolatedMargin`. Следующий
ревьюер должен оценивать эти границы, а также отсутствие обхода latch через
повторный reconcile или рестарт и отсутствие `MARKET_OPEN` до подтверждения
режима/плеча.

## Corrective к независимому ревью — 2026-09-13

Исходная фраза выше «занята HEDGE-нога → запрещено увеличение» отменена:
запрещена смена режима занятого символа, а рост в уже совпадающем режиме
разрешён. Это согласовано с подписанным HEDGE resize-сценарием. Конфликт
мастерских HEDGE-строк, включая flat-ногу, делает режим неизвестным и блокирует
новый риск.

По external close zero-target теперь применяется и к ручному same-episode
reopen ненулевой ноги. Journal хранит исполненный биржей `executed_qty` для
`MARKET_CLOSE`: запрос полного объёма с нулевым `-2022` или частичным fill не
доказывает engine-close. Подтверждённый linked Stop/TP `FILLED` сохраняется
отдельно на `trade_id`; временная ошибка следующего `query_order` не открывает
ногу. Для существующего `ISOLATED` рост проходит тот же предварительный 2×
collateral cap и зависимость `SET_LEVERAGE → MARKET_OPEN`.

Фраза «рестарт процесса» в прежней acceptance-матрице была чрезмерной:
проверена пересборка Coordinator/Executor над той же SQLite, а не полный
`App`/OS-process boot. Полный process-restart остаётся отдельным P2 proof gap;
без него это свойство не следует считать live-сертифицированным.

## Второй corrective по P1 #3 — 2026-09-13

Повторное независимое ревью приняло P1 #1/#2/#4/#5/#6, но воспроизвело re-entry
после обновления `e6f0d0f → #61`: старый процесс уже видел `FILLED`, сохранил
`last_observed_qty=0`, но не сохранил provenance. Если следующий `query_order`
временно недоступен, новый guard не знает о старом fill. Пять пунктов выше
не переделывались.

Добавлена отдельная data migration #62 (не переписана уже применённая #61):
active episode + старое flat-наблюдение + linked bot-owned Stop/TP + отсутствие
dust/external/нового protective proof получают durable
`UPGRADE_FLAT_CAUSE_UNKNOWN`. Это безопасный запрет входа, а не ложное заявление
о доказанном собственном Stop/TP. Координатор сохраняет zero-target, отдельный
skip и честную пользовательскую причину. Проверки моделируют оба входа в
миграцию (#60 и уже применённую #61), временную ошибку `query_order`, отсутствие
re-entry и сброс только на новом `trade_id`. Возможная цена — консервативный
запрет для старого flat иной природы с linked защитой, так как источник до #61
не сохранялся. Полный `App`/OS-process restart остаётся P2 proof gap.

Независимый re-review #62: **GO, все шесть исходных P1 приняты**. Проверены
upgrade #60→#62 и #61→#62, потеря старого proof, transient `query_order`,
flat-исход и сброс блока новым episode; прицельно 6 passed, полный offline
1604 passed / 2 skipped / 94 deselected, Ruff и diff-check PASS. Reviewer не
повторял signed Testnet и не менял дерево. P2 не является blocker для фиксации
этого corrective, но полный `App`/OS-process restart всё ещё не доказан.
