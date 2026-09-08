# S3 — интерфейс и функциональность: TODO и журнал

Дата начала: 2026-09-07. База перед S3 WIP: `41fba43`.
Статус: **IN PROGRESS; G3 OPEN**. Исполнение одним агентом, без субагентов.
Источник требований: [UX-план](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md),
§5–§8, §11–§16, §19–§21; corrective зависимости N4–N6 из §24–§25.

## Анализ и принятая архитектура

- Сохраняем четыре mounted root slots, существующие CSS tokens, native Back,
  resource owner S2, singleflight и read/write fences. Никаких router/query/
  motion frameworks, localStorage финансовых DTO и новых торговых controls.
- Источник фактов — application DTO S1. TypeScript сейчас ещё отражает старые
  контракты: добавляем точные зеркала, runtime validation и typed API.
  Финансовые суммы/атрибуция остаются в Python Decimal/accountant.
- Home получает overview из dashboard; полностью удаляем preview positions read.
  Open snapshot и история имеют независимую свежесть; нет snapshot ≠ пустой счёт.
- История получает небольшой session-owned bounded cache: три неактивных
  фильтра, до пяти страниц по 20 в каждом, десять неактивных detail DTO.
  Активный список и Back targets сохраняются; детали грузятся только по входу.
  Пагинация сохраняет server order, dedup по refs, независимые fills/events cursors.
- Nested routes типизированы; каждый stack entry владеет scroll/focus и состоянием
  возврата. Позиция без ref открывает snapshot details. Split — owned resolution,
  неизвестная/чужая ссылка — одинаковая недоступность.
- Mini App, Telegram menu и push переходят на user events. N4 исправляется
  до detail, N5 до delivery; legacy command facts удаляются из user-facing paths.
  Query intent не является авторизацией и сохраняется через gate/recovery.
- G3 требует отдельного фактического evidence: offline/browser не подменяют
  signed testnet, Telegram WebView, iOS/Android haptics и SL/STOP cleanup.

## TODO — порядок исполнения

1. [x] Прочитать корневой/docs/miniapp AGENTS, полный UX-план, product truth,
   архитектуру и последние diff; сопоставить G1/G2 и отложенные зависимости.
2. [ ] S3.0: зеркала DTO/API/validation; N4 SQL family cursor + regression
   >200 событий/aliases; недостающие аддитивные display contracts (цены closed,
   owned split resolution, live position detail) без нового финансового алгоритма.
3. [ ] S3.1: Home aggregate, компактный статус/freshness, рабочие переходы;
   PositionSummaryCard: qty/unit, PnL, entry/mark, stop, stale; small-value formatting.
4. [ ] S3.2: Open/Closed previews по три; полные списки/search/period/attention;
   bounded history resources, detail money/provenance/protection/events/fills;
   pagination retry/revision change/dedup, Back/scroll/focus и partial states.
5. [ ] S3.3: структурированный feed/pending/new-events; единый presenter menu/push;
   N5 durable-only delivery и rollout floor; owned launch intent через gate/recovery;
   удалить legacy трактовку, сохранить toggle/recipient rules.
6. [ ] S3.4: Settings values/environment, backend risk preview, dirty-leave;
   resume selector flat/exposed/unknown; nullable permissions/no fallback consent;
   Notifications delivery honesty, Help/Security/Success/gate/recovery copy;
   локальный календарь UTC/Moscow/New_York/DST; сохранить принятую RiskRuler механику.
7. [ ] S3.5 offline: targeted regressions, frontend lint/test/build, backend offline
   suite, ruff, документационные ссылки; обновить карты и единый test-status.
8. [ ] S3.5 browser: production bundle, 320/360/390/430, active/paused Home снимки,
   состояния списков/details/feed/settings; основной проход и один corrective.
   Cold ≤4 root POST, deep-link отдельный detail, ≥30 warm samples, delayed API,
   длинная история/Risk trace, 50 циклов и границы cache.
9. [ ] S3.5 live/G3: исправить N6 harness (journal coid, scheduled captures,
   SL fill, flat + no open orders), выполнить доступный выделенный testnet path;
   Telegram owner smoke/deep link/native Back/device gestures. Записать реальные
   ограничения и verdict; G3 закрывать только при полном evidence.

Каждый пункт закрывается после реализации и соответствующей проверки.
Счётчики suite хранятся только в `docs/ENGINE_TEST_STATUS_2026-08.md`.

## Промежуточные итоги

### 1. Анализ baseline

G1/G2 CLOSED подтверждены документацией и commits `84cabfb`/`41fba43`.
`HomeScreen` ещё использует `positions[0]` и trust strip; frontend history API
отсутствует. `_family_events` читает первые 200 каждого ref с `after_seq=0`,
поэтому N4 воспроизводим по коду. Default recovery всё ещё вызывает старый
capped user-event scan — N5 актуален. Принятые S2 mutation fences сохраняются.

### 2. Срез выполненного WIP (2026-09-07)

Следующие пункты реализованы в текущем коммите как рабочие срезы S3; они не
закрывают S3/G3 без полного offline/browser/live evidence.

- **S3.0 — частично выполнен:** добавлены зеркала новых DTO, typed API и
  runtime validation для overview, trades, trade detail, events, feed и risk
  preview; введён session-owned bounded `HistoryResources`; N4 переведён на
  repository-level `after_seq`/`LIMIT` paging. Регрессия на 251 событии одной
  alias-family проходит без пропусков и дублей.
- **S3.1 — UI-срез выполнен:** Home читает dashboard overview, показывает
  summary/status/freshness и ведёт в Positions/Risk/Account/Status; старые
  trust-rail и `positions[0]` удалены. Добавлена компактная
  `PositionSummaryCard` с qty/unit, entry/mark, PnL, stop и stale-state.
- **S3.2 — UI-срез выполнен:** добавлены Open/Closed previews, полные списки,
  trade detail, независимые fills/events cursors, dedup/merge cache и typed
  nested routes. Доступность неизвестного или чужого ref остаётся единым
  fail-closed состоянием; полноценные pagination/revision/browser проверки ещё
  впереди.
- **S3.3 — UI/backend-срез выполнен:** Activity переведён на структурированный
  feed и user-event presenter с локальной группировкой дат; добавлен owned
  launch intent через gate/recovery; legacy default recovery scan удалён из
  `Stage2Process`. N5 delivery-only rollout, menu/push smoke и Telegram
  evidence ещё не выполнены.
- **S3.4 — UI-срез выполнен:** обновлены Settings/Help/Security/Success/gate
  тексты, nullable permissions показываются честно, добавлен risk-preview и
  сохранены dirty-leave/risk mechanics; форматирование малых значений и
  локальный календарь событий приведены к display-контракту. Устройство,
  Telegram WebView и timezone/DST acceptance ещё не пройдены.

Проверки этого среза:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/unit/application/test_trade_events_c2.py \
  tests/unit/application/test_trade_views.py \
  tests/unit/application/test_history_contracts.py \
  tests/unit/application/test_engine.py \
  tests/unit/miniapp/test_history_server.py
  59 passed

cd miniapp && pnpm exec tsc -b && pnpm build
  PASS
cd miniapp && pnpm lint
  PASS with 2 existing/new warnings (complex Activity useEffect deps;
  AppShell ref read during render)
cd miniapp && pnpm test -- --run
  272 passed / 44 failed, 27 files; failures are the pre-S3 contract and
  lifecycle tests that still assert removed legacy Settings/Home/Activity
  surfaces and require the next corrective pass.
git diff --check
  PASS
```

### 3. Откуда продолжать

1. Сначала обновить/добавить frontend contract tests под новые Home,
   Positions, Activity, Settings и nested history routes; разобрать 44 текущих
   падения и закрыть два oxlint warning без изменения S2 fences.
2. Затем закончить S3.2 pagination/revision/partial-state regressions и S3.3:
   единый menu/push presenter, N5 durable-only delivery, pending/new-event
   rollout и Telegram deep-link smoke.
3. После этого выполнить S3.4/S3.5 offline + browser matrix, затем N6 signed
   harness и G3 evidence (device/Telegram WebView, BackButton/haptics,
   journal-coid cleanup, flat + no open orders). До этих шагов G3 остаётся
   **OPEN**.

### 4. Возобновление: causal fence истории (2026-09-08)

Проверка текущего `957bef5` показала, что предыдущая формулировка «падения
из-за старых assertions» не доказывает отсутствие регрессий. В HistoryResources
найдена реальная гонка: invalidation во время read поглощалась поздней
публикацией `receivedAt`. Добавлена revision-граница: pre-invalidation data/error
не публикуются; повторный запрос consumer объединяется в один follow-up после
settle. Неактивный запрос без нового обращения остаётся stale и не запускает
скрытый follow-up. Добавлены regressions в `historyResources.test.ts`, targeted
проверка и TypeScript проходят. Полный suite ещё не перепроверен; G3 OPEN.

### 5. Activity: контракты и TTL (2026-09-08)

Auth/scope regression tests переведены на `activityFeed`: hard-session gate,
abort прежнего scope и запрет поздней публикации проверяются через новый API.
Static UI assertions учитывают фильтры и отсутствие выдуманной testnet-среды.
Calendar fixture теперь задаёт локальные даты, а не ошибочно считает UTC-день
локальным. В history owner восстановлен 60-секундный Activity TTL и добавлен
regression для активаций на 35/65 секундах. Устранён complex-deps warning
календарного effect. Полный suite повторён: актуальные числа в едином
ENGINE_TEST_STATUS; оставшиеся падения относятся к Home/Settings/форматированию
и shell lifecycle и ещё не закрыты.

### 6. Home/Settings и финансовое отображение (2026-09-08)

Home title и Settings assertions приведены к §8.1: «Копирование включено»,
«Риск на сделку», «Подключение Aster», среда из session. Проверки prefetch,
cached navigation и AccountBuilder behavior проходят после обновления названий
строк, без ослабления проверок API ownership. Исправлен реальный display edge:
пустые строки в цене/PnL/USDT показывают отсутствие факта, а не ноль; малые
ненулевые USDT показываются как порог со знаком. Добавлены regressions для
малых цен, положительного/отрицательного PnL и malformed/missing amounts.
TypeScript проходит. Следующий участок — оставшийся shell keep-alive suite.

### 7. Shell fixtures: переход на feed (2026-09-08)

После `06eba59` shell mocks переведены с legacy activity на activityFeed.
Фикстуры возвращают event_ref/text/high_water/next_cursor, а pagination
проверяет отправленный cursor и сохранение строк после Back вместо удалённого
DOM page-counter. Названия Settings и Activity markers обновлены по S3 UI.
Targeted результаты находятся в ENGINE_TEST_STATUS; TypeScript проходит.
Оставшиеся падения ещё не классифицированы окончательно: Home preview
expectations нужно заменить overview contract, а stale/retry/pagination
сценарии проверить на реальные регрессии. G3 OPEN.

### 8. Shell corrective: offline suite зелёный (2026-09-08)

Home fixtures теперь содержат account overview; проверка отдельного Home
доказывает отсутствие positions-preview reads. Проверки раннего входа в Positions
сохраняют обязательный единственный follow-up после strong dashboard. Навигация,
scroll/focus, mutation locks и pause publication проверяются на текущих UI
markers. Activity TTL probe сохраняет обе накопленные страницы и показывает
«Есть новые события», не подменяя их новой первой страницей. Positions error
action явно называется «Повторить». Полный frontend suite и TypeScript проходят;
счётчики — в ENGINE_TEST_STATUS. Продолжать S3.2 pagination/detail/cache edge
cases и оставшийся AppShell ref warning, затем S3.3 N5/delivery и общая приёмка.

### 9. Повтор неудавшейся страницы (2026-09-08)

Исправлен сброс накопленных detail pages при retry: history owner запоминает
режим неудавшегося read, и кнопка повторяет events/fills/more с прежним cursor.
При `history_page_changed` retry выполняет явный refresh. Activity и ClosedTrades
используют тот же retry вместо угадывания по наличию next_cursor. Regression
проверяет сохранение предыдущих events и независимого fills cursor. Targeted
проверка и TypeScript проходят; полный suite после этой правки ещё не повторён.

### 10. Явный restart после смены ревизии (2026-09-08)

После `history_page_changed` блокируются automatic ensure и дальнейшее more:
старый срез остаётся видимым до явного refresh/retry пользователя. Проверено,
что invalidation/повторная активация не поглощают этот статус, а explicit retry
не передаёт старый cursor. Targeted regressions и TypeScript проходят.
Обычная TTL-ревалидация накопленных trades/events без server revision error
пока требует отдельной проверки: текущий код может заменить их первой страницей.

### 11. TTL probe без потери страниц (2026-09-08)

Trades/events сохраняют исходную первую страницу для сравнения. При ensure
неизменная первая страница подтверждает срез без сброса накопленных элементов
и курсоров; изменение фактов переводит его в explicit-refresh state. Сигнатура
публикуется только вместе с принятым по generation/revision ответом. Regression
покрывает events append → unchanged probe → changed probe; TypeScript проходит.
Отдельное покрытие trades, browser scroll и остальные S3 требования остаются.

### 12. Closed trades и render keys (2026-09-08)

Добавлено отдельное покрытие closed-trade append/dedup/TTL/changed/restart.
Показанный набор и continuation cursor сохраняются до explicit replacement.
AppShell больше не читает ref при render: ключи mounted entries передаются
через React state; синхронный ref остаётся для Back/scroll bookkeeping.
Полный frontend suite проходит, oxlint clean; актуальные counters в
ENGINE_TEST_STATUS. Browser performance, cache bounds, detail partial states
и полный delivery path пока не приняты; G3 OPEN.

### 13. Cache bounds и stale live detail (2026-09-08)

Regressions подтверждают лимит трёх inactive filters, сохранение subscribed
Back target и целого длинного списка до release. Уход освобождает oversized
slice целиком, без некорректной пары truncated data/cursor. В TradeDetail
добавлены live snapshot error, stale warning, snapshot time и явное strong
обновление позиции: устаревшие PnL/protection больше не показываются без
оговорки. TypeScript/oxlint проходят; визуальная приёмка этих состояний впереди.

### 14. Порядок продолжения после лимита сессии (2026-09-08)

Следующий проход ведётся implementation-first. Не запускать полный frontend
suite после каждой мелкой правки. Сначала закончить функциональные блоки S3.2 и
S3.3: detail partial/unknown states, полные trades/events/fills flows, Telegram
menu/push presenter, N5 durable-only delivery и owned deep links. Затем одним
проходом выполнить offline gate (frontend/backend/ruff/docs), после чего перейти
к browser/device/live evidence S3.5/G3. Мелкие targeted checks допустимы только
для нового риска и не заменяют завершение функционального объёма.

### 15. Завершение реализации перед отдельной приёмкой (2026-09-08)

Уточнение владельца: этот проход посвящён реализации; тесты, browser/device и
signed live выполняются следующей сессией. G3 до evidence остаётся OPEN.
Конкретный TODO текущего прохода:

- [x] S3.3: перевести Telegram Activity и совместимый HTTP activity на user events;
  удалить командный mapper пользовательских фактов.
- [x] S3.3/N5: включить durable delivery в production composition, добавить
  периодический drain и корректный shutdown; исключить двойные command push.
- [x] S3.3: согласовать query ссылки worker и frontend, включить owned trade links.
- [x] S3.2/S3.4: закончить состояния detail/loading/partial/unknown и проверить
  по коду оставшиеся требования Settings/resume/calendar.
- [x] S3.5: обновить guides и передать конкретную матрицу приёмки без её запуска.

### 16. Реализация остатка S3 перед передачей (2026-09-08)

TODO §15 выполнен по коду. Telegram menu переведён на user-event feed с
bounded cursor history и ссылками; legacy HTTP activity стал временным
adapter к той же модели. Удалены command Activity mapper/readers и capped
user-event recovery. Production factory включает durable delivery с recurring
sweep/shutdown, rollout floor и подавлением старых command push. Link URL
согласован (`trade`), старый `trade_ref` читается для совместимости.

Trade detail поддерживает owned split resolution с выбором successor, явные
loading/empty/partial состояния и группировку fills по order/role. Missing ref
очищает cached detail. Closed list показывает время сверки, changed revision
блокирует continuation. Исправлен отрицательный дробный знак общего presenter.
Сохранены S2 ownership/fences, серверная денежная арифметика, read-only позиции.

S3.0–S3.4 функционально реализованы; исходные acceptance checkboxes выше
остаются открытыми до проверки. S3.5 guides готовы; offline/browser/device/N6
signed приёмка отложена владельцем. Тесты не запускались; TypeScript и static
Python checks проходят. Детальный порядок проверки, изменённые контракты и
границы — [S3 testing handoff](MINIAPP_S3_TESTING_HANDOFF_2026-09.md).

### 17. Граница выключения уведомлений (2026-09-08)

Code review после `903d4f4` выявил race: выключение/включение между sweep могло
доставить событие выключенного периода. Toggle теперь одной транзакцией
фиксирует published event high-water (только после delivery rollout), меняет
настройку и отменяет PENDING outbox при выключении. No-op toggle не двигает
границу. Durable commit повторно проверяет recipient/ownership и committed
cursor; поздно подготовленные строки не вставляются, cursor не откатывается.
Отправку, уже начатую до выключения, эта граница не отзывает.
Тесты по просьбе владельца не запускались; ruff check/format проходят.
