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

### 18. Verification pass: миграция тестов, N5, N6 harness (2026-09-09)

Проверка по [S3 testing handoff](MINIAPP_S3_TESTING_HANDOFF_2026-09.md),
блоки 1–2 и N6-исправление. Реализация не менялась — только тесты и живая
документация.

**Миграция старых тестов (handoff §1).** Collection падал на
`ImportError: _activity_kind_filter`; после снятия блокера полный offline-прогон
дал 9 падений — все оказались устаревшими fixtures/assertions, не регрессиями
кода. Переведены на S3-контракты: `test_stage2_adapters` (feed SQL-фильтр до
LIMIT вместо удалённого runtime `read_activity`), `test_adapters` (убран
`read_activity`), `test_service` (5 activity-тестов читают user events,
категории/порядок/feed-pagination), `test_renderers` (`ActivityFeedPage` +
pending + owned trade-link), `test_notifications` (удалены 4 теста на удалённый
`router.recover_user_events`; эквиваленты уже в C6), `test_history_acceptance`
и `test_history_acceptance_live`/`test_builder_resume_live`
(`recover_user_events_durable`). Два теста приведены к фактическому коду:
presenter-копия «PnL до комиссий» (совпадает с frontend) и Web App keyboard
только в приватном чате (`chat_id == user_id`).

**N5 (handoff §2).** Добавлены тесты, а не ручная проверка:
- `test_user_event_delivery_c6`: crash до atomic commit (ни строк, ни курсора)
  и replay ровно один раз; crash после commit → 0 новых строк; disable →
  событие → enable (muted не доставлен, пост-enable доставлен); prepare →
  toggle(disable) → commit (строки не вставлены, курсор вперёд); prepare →
  disable → enable → commit (fence съедает строку); no-op `enable=True` не
  двигает fence; disable помечает PENDING outbox `FAILED/recipient_disabled`;
  `_commit_batch` держит курсор монотонным (MAX).
- `test_engine`: production factory `True` vs low-level `False` (legacy без
  delivery task); два boot на >1000 событий — ровно 1001 outbox-строк, 0
  дублей, третий boot 0 новых; delivery task отменяется до закрытия БД;
  сбой sweep → retry без смерти task; legacy `execution.confirmed`
  `FAILED/superseded_by_user_events`, `command:cmd-1` не появляется,
  user-event ровно один.

**N6 live-harness (handoff §7) — закрыт живым прогоном.**
`tests/testnet/test_history_acceptance_live.py`: CANCEL STOP теперь целится в
фактический `execution_commands.client_order_id` (`ct<key>`) из строки
PLACE_STOP, не в исходный plan-label; до отмены доказано наличие ордера
(`query_order=NEW` + присутствие в `open_orders`), после — `query_order`
CANCELED/None и отсутствие order_id в списке (одного CONFIRMED после −2011
недостаточно); cleanup отменяет protective-ордера и подтверждает flat +
`open_orders()==[]`; добавлены scheduled captures между шагами open →
increase → reduce → close с проверкой одного `public_ref` и durable-boundary
окна публикации (scheduled captures публикуют окна до финального capture,
поэтому `published.start` = `latest.cutoff_ms + 1`, а не `t0`); сценарий
реального SL fill — тик-прижатый ратчет: стоп ставится на
`floor(mark/tickSize)*tickSize` (immediate-trigger отвергается −2021), окно
75 с, перестановка только при дрейфе mark вверх >2 тиков, grace-цикл 5 с
перед отменой, отмена терпима к гонке с исполнением, бюджет блока 600 с.

**Живой signed прогон N6 (testnet, 2026-09-09).** Три пробы измерили механику
mark на testnet: обновляется пачками раз в ~1–2 мин шагами 26–88 USDT, между
обновлениями статичен до 8 знаков; immediate-trigger STOP отвергается `-2021
"Order would immediately trigger"`. Прогон №4 — `1 passed in 85.56s`, SL сел с
первой попытки (`stop 78869.50` при mark `78869.53878384`); прогон №5 —
`1 passed in 185.61s`, две перестановки до фила. Покрыто живым прогоном:
open → increase → protective STOP place+cancel (доказательство по `ct<key>`)
→ reduce → контролируемый UNKNOWN close (query-before-retry, ровно один
ордер) → restart-персистентность → HTTP surfaces (trades/trade/events/feed)
→ durable recovery → foreign isolation → реальный SL fill (FILLED, fill в
`user_trades` и импортированных fills, эпизод закрыт). Попутно найдена и
закрыта утечка: БД, открытая после симуляции рестарта (`history_db2`), не
закрывалась никем — aiosqlite-поток не умирал, и pytest висел в
`Py_FinalizeEx` после PASSED; фикстура теперь закрывает `_EXTRA_DATABASES`
(идемпотентно, в том числе на падении), прогон №5 вышел чисто (`EXIT=0`,
процессов не осталось).

**Offline gate (handoff §4).** backend **1449 passed / 2 skipped / 45
deselected**; frontend **28 files / 349 passed** (в §15 добавлены 18
TZ/DST-кейсов, «подпись времени = локальный день» и 3 resume-confirm
Sheet-кейса; в presenter — кейс знака малого отрицательного PnL); oxlint
clean; `tsc -b && vite build` PASS; `ruff check` / `ruff format --check`
clean (включая автоформат `test_history_acceptance_live.py` и ранее
отклонявшегося `test_trade_events_c2.py`); doc links OK (54 файла).

**Осталось до G3.** Browser-матрица (production bundle, 320/360/390/430,
cold/warm budgets, delayed API, 50 циклов), Telegram WebView (owned links,
native Back, haptics, WebKit) — нужен owner evidence. Timezone/DST закрыт
offline-матрицей (§15: 18 кейсов + локальный день подписи); signed live N6
выполнен на testnet (`1 passed`, реальный SL fill, чистый выход). G3
остаётся **OPEN**.

### 19. Browser/UI-проход: частичное покрытие (2026-09-09)

Проход остановлен владельцем по лимиту сессии. Ниже — только фактически
выполненное; непокрытое перечислено явно, G3 остаётся **OPEN**. Числа — один
проход, не заменяют §19.7.

**Стенд.** `scripts/preview_miniapp_engine.py --account tg-8264892202
--confirm-engine-preview --port 8085`: копия прод-БД (backup API), testnet,
unsigned dev-session (900000001). Production bundle `miniapp/dist` собран с
`VITE_DEV_USER_ID=900000001` (inline env; `.env.production.local` не создавался
— известная ловушка dev-режима в проде). После прохода `dist/` пересобран
чистым (`pnpm build` без env) — для §6/реального бота dev-режим в бандле
отсутствует; при возобновлении browser-прохода стенд требует повторной сборки
с `VITE_DEV_USER_ID=900000001`. HEAD `09bd528`. Browser: Chromium 152
(browseuse/CDP), viewport 390×844, DPR 3; визуальная проверка скриншотов
(зрение) по каждому экрану.

**Исправление preview-харнесса (коммит вместе с этим журналом).**
`preview_miniapp_engine.py` падал `OwnershipConflictError`: dev-юзер 900000001
в снапшоте прод-БД привязан к `copyc1`. Теперь скрипт перед upsert отвязывает
dev-юзера (и его outbox-строки) в одноразовой копии; прод-БД не затронута.

**Живые testnet-данные для UI.** Master `open BTCUSDT 0.002 1.0 long` →
follower tg-8264892202 открыл **0.037 BTC** (200×, риск 2%, стоп 1%, стоп
подтверждён), затем `close`; обе стороны flat, `open_orders=[]`. По ходу
зафиксировано ограничение: у BTCUSDT брекет 200× имеет **notional cap $4 000**;
при стопе 0.5% плановый объём (риск 2% → 0.074 BTC ≈ $5.8k) биржа отвергает
`-2027`, при стопе 1% объём 0.037 BTC проходит. Плечо follower'а при этом
корректно-автоматическое: `LeverageManager` сам выставляет `maxInitialLeverage`
символа (200×) через leverage maintenance, `SET_LEVERAGE` в плане не требуется;
ручная смена на 10× была возвращена движком к 200×. Bracket-cap сайзинга в
RiskEngine не учитывается — зафиксировано как наблюдение (не UI-дефект).

**Проверено в браузере** (скриншоты: `docs/reports/acceptance-s3/`, 17 PNG,
390×844, HEAD 09bd528):

- **Home active**: сводка (копирование включено, PnL открытых, 1 позиция,
  «Стоп подтверждён 1 из 1», доступно, риск 2%); TrustRail и карточка первой
  позиции отсутствуют — S3.1 подтверждён визуально и по DOM.
- **Home paused**: «Новые входы на паузе» + «Возобновить»; пауза применяется
  сразу. **Resume confirm sheet**: текст варианта «позиция мастера» (не flat);
  подтверждение server-confirmed, возврат в active ≈4.5 c.
- **Positions**: сегменты «Открытые 1/Закрытые», карточка BTCUSDT (qty, PnL,
  200×, вход/марк/стоп «подтверждён»); detail = snapshot-fallback («История
  этой позиции ещё уточняется») с Вход/Сейчас/Stop/Риск до Stop/Плечо 200× авто.
- **Activity**: фильтры Все/Сделки/Система, баннер «Есть новые события»,
  события со временем. На момент прохода — только системные `ENTRY_SKIPPED`
  (`NO_STOP`, `STALE_MASTER`): проекция fills/episodes в копии пуста
  (`follower_episodes=0`, `follower_fills=0`, `user_activity_events=13`).
- **Settings + все 6 вложенных** (Риск, Уведомления, Статус подключения,
  Подключение Aster, Безопасность, Помощь): отрендерены, секретов/сырых
  payload нет; Status честно «Данные устарели · не удалось обновить»,
  AccountBuilder — «Прямое подключение, Builder не используется».
- **Native Back** (Telegram-WebApp stub с BackButton, initData пустой → dev
  session сохраняется): detail → список, BackButton visible true→false;
  вложенные экраны возвращаются на Settings.
- **RiskRuler**: тапы по тикам меняют draft; реальные save **2%→1%** и
  **1%→2%** server-confirmed (~1 c, `control_audit`). CDP-драг и
  синтезированные PointerEvent'ы в этом харнессе не двигают линейку
  (`Input.dispatchMouseEvent` mouseMoved стабится/таймаутит на pointer-capture);
  жесты приняты ранее в `MINIAPP_RISK_RULER_2026-09.md` — этот проход их не
  переобъявляет.
- **Notifications double-tap gate** (instrumented `fetch`): два клика в одном
  тике → ровно **1 POST**; интервалы 60 ms и 300 ms → **2 POST** (первая
  мутация успевала settle: локальный RTT ~1 ms). Гейт закрывает burst в одном
  тике; осознанные последовательные тапы не блокирует. Итоговое состояние ON
  восстановлено.

**Не выполнено (продолжение приёмки):**

1. Viewports **320/360/430** — снят только 390×844.
2. Состояния ошибок списков/details/feed (блокировка API, throttle).
3. **§19.7 бюджеты**: cold root ≤4 read POST; ≥30 warm samples p95 ≤100 ms;
   delayed API ~300 ms; длинная история; scroll/long-task trace; 50 циклов
   root→nested→Back и nodes/listeners/timers/rAF/`body.overflow`.
4. **Closed trades list + trade detail** (fills/events/partial/split/alias) —
   нужна непустая проекция истории; в копии её не было.
5. **§6 Telegram WebView на устройстве владельца** (owned links, native Back,
   haptics, короткий viewport, keyboard) — нужен owner evidence.
6. Offline/signed гейты в этом проходе не перезапускались (счётчики не
   менялись); live N6 закрыт ранее (§18), повторный signed-прогон не требовался.

G3 — **OPEN** до закрытия пунктов 1–6.

### 20. Live-проход 2026-09-09: copy open→close + состояния ошибок (Computer/Chrome)

Стенд перезапускать не пришлось: preview `tg-8264892202` на `:8085` жив,
Chrome был на пустой вкладке — открыт `/miniapp/` через Computer. HEAD
`e9b3f3a`, testnet, dev-сессия `900000001`.

**Живой copy-цикл (testnet).** Master `open BTCUSDT 0.002 LONG, stop 1%`
(`@~78715.13`, stop `77927.98`) → projector `trade_id=223`, сначала
`ENTRY_SKIPPED NO_STOP` до прихода стопа, затем планы всем follower:
`tg-8264892202` → `MARKET_OPEN 0.037 + PLACE_STOP`; `execution.confirmed`
open + stop. Positions refresh: **1 open, 0.037 BTC LONG, 200×**,
entry `78671.5`, stop `77927.98` подтверждён, PnL `+1.61 USDT`. Home
и Positions подтверждены визуально в Chrome (Computer): Home `+1,61 USDT`,
`Открытые 1`, `Стоп подтверждён 1 из 1`; карточка `BTCUSDT · LONG`,
`0,037 BTC · 200×`, `Вход/Марк/Стоп подтверждён`.
Master `close BTCUSDT` (скрипт сначала снял ордера) → follower
`CANCEL_ORDER + MARKET_CLOSE 0.037` confirmed; positions refresh **0, flat**.
Риск `2%`, уведомления `ON`, копирование `active` — восстановлены/сохранены.

**Граница проекции.** После цикла `trades.items=[]`,
`coverage_complete=false`, `follower_episodes=0`, `follower_fills=0`.
`HistoryCapture` в preview-engine не запланирован — проекция пуста не из-за
UI. Непустой closed detail через этот стенд невозможен без капчера;
HTTP-уровень истории закрыт signed N6 (§18). Фиксируется как граница стенда,
а не UI-дефект.

**Состояния ошибок в Chrome (Computer, live):**

- Открытые 0 после close: stale snapshot
  `Снимок позиций устарел. Статус защиты требует обновления.`
  + `Обновить данные`.
- Закрытые: честное `Диапазон истории уточняется · может быть неполной`,
  `Последняя сверка: 9 сентября 2026 г. в 05:24 GMT+3`,
  `Загружаем историю. Доступный диапазон пока не подтверждён.`
  + `Обновить историю`.
- Unknown ref (`/miniapp/?trade=deadbeef…`): fail-closed
  `Сделка недоступна. Откройте список своих позиций. Ссылка могла устареть
  или относиться к другому аккаунту.` + `Повторить` / `Мои позиции`.
- Финал Home flat: `0,00 USDT`, `Открытые 0`, `0 из 0`,
  `Доступно 1 472,75 USDT`, риск `2%`, `Копирование включено`.

**Осталось до G3.** Viewports 320/360/430; полные CDP-бюджеты §19.7 (cold ≤4,
≥30 warm samples p95 ≤100 ms, delayed API 300 ms, 50 циклов, memory/long-task);
непустой closed detail (нужна проекция с капчером); §6 device (owner evidence).
G3 — **OPEN**.

### 21. Telegram WebView: закрытая история, Settings и pause/resume (2026-09-09)

Повторный live-проход выполнен непосредственно в открытом Telegram Desktop
WebView бота `@aster_copytrade_bot`, на аккаунте `0x3c45…F975`, с текущим
testnet-стендом. Уведомления не переключались. Действие pause/resume выполнено
для проверки уже принятого control-flow и возвращено в исходное состояние.

**Positions / closed history.** До обновления отображался серверный баннер
`История изменилась — обновить`; после `Обновить` список согласовался и показал
непустую историю. Визуально подтверждены две карточки: `BTCUSDT · LONG`
`+2,25 USDT`, вход `78 671,5`, выход `78 732,3`, менее минуты; и
`+1,62 USDT`, вход `78 848,3`, выход `78 892,2`, 6 минут. Открытых позиций —
`0`.

Первая карточка открыла owned detail без чужих данных: PnL до комиссий
`+2,25 USDT`, вход `0,037 BTC`, закрыто `0,037 BTC`, остаток `0 BTC`, средний
вход `78 671,5`, средний выход `78 732,3`. В блоке событий показаны открытие
и закрытие; длительность — `58 с`, PnL до комиссий — `2,2496 USDT`. Оба
исполнения раскрываются отдельно: вход `0,037 BTC` по `78 671,5` с PnL доли
`0,00 USDT`; выход `0,037 BTC` по `78 732,3` с PnL доли `+2,25 USDT`.
Native Back вернул detail в сохранённый список закрытых сделок.

**Activity.** В Telegram подтверждены фильтры `Все` и `Сделки`, события
`Открыта BTCUSDT · LONG` / `Закрыта BTCUSDT · LONG` и owned-кнопка
`Открыть сделку`, ведущая в тот же detail. После Back экран Activity сохраняет
выбранный фильтр. Фильтр `Система` переключается; его отдельный loading/empty
кадр не трактуется как ошибка API.

**Settings и Home.** На Settings отрендерены Risk (`2% · по стопу`),
Notifications (`Отключены`), Status, Aster connection, Security и Help.
Notifications показал `Доставка выключена`, очередь `0`, ранее не доставлено
`0`; toggle не нажимался. Status честно показал stale/aged данные, при этом
система и копирование отображались работающими, позиции `0`. Aster показал
прямое подключение, `Builder не используется`, фьючерсная торговля разрешена,
вывод запрещён; секреты и payload не показаны. Back из дочерних экранов вернул
в Settings.

На Home подтверждено flat-состояние: `PnL открытых позиций 0,00 USDT`,
`Открытые позиции 0`, риск `2%`, копирование включено. Pause показал
`Новые входы на паузе` и кнопку `Возобновить`; Resume Sheet потребовал отдельное
подтверждение. После подтверждения экран вернулся в `Копирование включено`.
Фактическая БД после прохода: `allow_new_entries=1`, `risk_percent=0.02`,
owner `notifications_enabled=0`, `COPY_PAUSED` и `COPY_RESUMED` записаны,
незавершённых execution commands `0`; история аккаунта — `170` episodes,
`487` fills.

Этот раздел закрывает Telegram Desktop evidence для непустого closed list/detail,
fills/events, Activity, owned navigation, Settings, native Back и pause/resume.
Мобильные iOS/Android WebView, haptics/keyboard, viewports `320/360/430`,
полный §19.7 CDP budget trace и split/alias/partial-specific history остаются
границами G3. G3 — **OPEN**.

### 22. Telegram S2-only revalidation (2026-09-09)

По запросу владельца после S3 detail-pass выполнен отдельный прогон только
Mini App внутри Telegram Desktop WebView, без повторного backend/S2 общего
набора. Проверены root-переходы, Settings cache/revisit, Risk dirty draft и
Sheet-first Back, Notifications read-only, stale Status/Aster, scroll/back,
`Settings → Risk → Back` **50 циклов**, а также pause → Resume Sheet → Cancel
с сохранением paused и последующий server-confirmed Resume → active.

Факты: повторный Risk открылся сразу с серверными `2,0%`; draft `2,5%` после
выхода без сохранения не попал в БД; Telegram Home в финале показывает
`Копирование включено`, открытых позиций `0`, риск `2%`. Notifications не
переключались и остались выключены. Финальная БД: `allow_new_entries=1`,
`risk_percent=0.02`, `notifications_enabled=0`, `170` episodes, `487` fills,
`0` незавершённых execution commands; за текущий цикл записаны ровно один
`COPY_PAUSED` и один `COPY_RESUMED`.

Этот прогон даёт **PASS** для Telegram Desktop S2 Mini App UI/lifecycle smoke.
Он не измеряет из Telegram Desktop число HTTP POST, CDP heap/listeners или
mobile WebKit/haptics; эти границы остаются явно отделены от G2/G3 verdict.

Сводная карта фактических S2/S3 UI-проверок Mini App — Telegram WebView,
Chromium/CDP, frontend-тесты и screenshots — находится в
[MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).

### 23. Browser viewport recheck — 320/360/430 (2026-09-09)

После основного S3 browser pass выполнен отдельный Computer Use-проход на
production bundle preview-стенда `:8085` с dev-session `900000001`, высота
viewport — `844px`. Последовательно проверены ширины `320px`, `360px` и
`430px`.

На каждой ширине фактически открыты и проверены Home, Positions (Открытые и
Закрытые), Activity, Settings и все шесть nested screens: Risk, Notifications,
System Status, Aster connection, Security и Help. AX/DOM-проверка дала на всех
экранах `documentWidth = bodyWidth = viewportWidth` и `horizontalOverflow=false`:

| Viewport | documentWidth | bodyWidth | page-level overflow |
|---:|---:|---:|---|
| `320×844` | `320` | `320` | отсутствует |
| `360×844` | `360` | `360` | отсутствует |
| `430×844` | `430` | `430` | отсутствует |

Home визуально проверен на всех трёх ширинах, Risk — на `320px`. Элементы,
выходящие за границы в Risk, относятся к его штатной внутренней горизонтальной
шкале выбора; горизонтального overflow страницы нет. В процессе не менялись
risk/settings/notifications и торговые данные. После проверки viewport override
сброшен, browser возвращён на Home; Telegram Desktop состояние не затронуто.

**Результат: viewport-пункт G3 закрыт — PASS.**

### 24. Свежий S3 §19.7 performance trace — PASS (2026-09-09)

После финальных S3 transport/history changes выполнен новый trace на
production bundle `HEAD=49cbde7`. Стенд —
`preview_miniapp_engine.py --account tg-8264892202 --confirm-engine-preview
--port 8085`, testnet и disposable SQLite backup; production DB не менялась.
Для измерений использован отдельный одноразовый профиль Chrome `152.0.7977.83`
с viewport `390×844`, DPR 3, Playwright/CDP-инструментацией. User Chrome и
Telegram UI этим измерителем не управлялись; Computer Use-проходы записаны в
§§23 и 26.

**Cold root.** Получены ровно **4 успешных read POST**: `session` (200),
`dashboard(refresh=true)` (200, около 1,5 с из-за testnet snapshot),
`positions(refresh=false)` (200) и `activity/feed` (200). В ответе Home:
testnet, `0x3c45…F975`, active, открытых позиций `0`, риск `2%`, доступно
около `1 474,5 USDT`.

**Warm navigation.** Выполнено `40` переключений каждого root tab — всего
`160` samples, и ни один переход не добавил API read (`apiDelta=0`):

| Экран | Samples | p95 | Max |
|---|---:|---:|---:|
| Главная | 40 | 4,2 ms | 7,6 ms |
| Позиции | 40 | 4,4 ms | 10,9 ms |
| Активность | 40 | 5,4 ms | 11,3 ms |
| Настройки | 40 | 5,6 ms | 7,9 ms |
| Все root tabs | 160 | 5,3 ms | 11,3 ms |

Nested pass — `50` открытий Risk и по `30` открытий Notifications, System
Status, Aster, Security и Help: всего **200 samples**, aggregate p95 `5,3 ms`,
max `7,7 ms`; худший отдельный p95 — Aster `6,6 ms`. Delayed API с задержкой
`300 ms` на каждый `/miniapp/api/**` дал ещё `160` warm root samples,
`apiDelta=0`, p95 `4,4 ms`, max `15,9 ms`. На delayed pass не было long task
`>50 ms`, ошибок страницы или unhandled rejection.

**Длинная история и lifecycle.** В Activity подтверждены реальные `493`
события; четыре нажатия `Показать ещё` дали пять feed pages, итоговый body —
`12 554` символа. Scroll trace занял около `253,2 ms`, long tasks/errors —
`0/0/0`. В deep link на реальный ref
`740d3c5c52d9423a9a38b8afbfc0e774` выполнен ровно один `/trade` read и один
`/trade/events` read для detail; shell prefetch остался отдельными reads.
Risk drag trace не делал mutation и также не дал long task/page error.

В `50` Risk cycles и по `30` cycles остальных пяти nested screens одновременно
сняты bounds: перед nested pass CDP показывал `1` document, `512` nodes и
`210` JS listeners; после — `1`, `512` и `214`. Рост nodes отсутствует,
listener counter увеличился только на `4` после `200` смешанных переходов и не
образовал runaway; page-level timers, intervals и rAF в финальном trace — `0`.
Layout на проверенных фазах: `documentWidth=bodyWidth=viewportWidth=390`.

Машинный артефакт: `data/miniapp-perf/g3-cdp-trace-2026-09-09.json`.
Этот раздел закрывает свежий S3 §19.7 budget evidence — **PASS**.

### 25. Error matrix и специальные history cases — PASS (2026-09-09)

Через реальный Mini App/API path на preview выполнен controlled response fault
injection; production DB и состояние аккаунта не менялись. Проверены `11`
кейсов: blocked и throttled для Positions list, Closed trades list, Activity
feed, trade detail и trade events, а также throttled `filter=trade`. В каждом
случае экран показал правильное разделение `Нет связи` или `Сервис недоступен`
и пользовательский retry. При ошибке events ранее загруженный detail остался
на экране, а ошибка ограничилась блоком истории/fills. Для blocked Positions
повторный retry был виден как второй intercepted request; бесконечного
background retry в этом finite pass не обнаружено.

Три специальные history-варианта прогнаны отдельными fresh sessions на
реальной разметке и роутинге Mini App, с подменой только DTO-ответа:

- **alias** — opaque alias открыл тот же owned detail, события и два fills;
- **split** — показаны `История сделки уточнена`, объяснение разделения и
  `Открыть эпизод 1`; переход открыл канонический detail с событиями/fills;
- **partial history** — показано `Начало истории восстановлено не полностью`,
  неизвестные начало/объём/цена отображены как `—`, доказанные события и fills
  остались доступны.

Каноническая запись для fixture была реальной закрытой BTCUSDT-сделкой
`740d3c5c52d9423a9a38b8afbfc0e774`, фактическая БД не мутировалась. Скриншоты:
[alias](acceptance-s3/g3-history-alias.png),
[split](acceptance-s3/g3-history-split.png),
[partial](acceptance-s3/g3-history-partial.png). Машинный результат —
`data/miniapp-perf/g3-error-matrix-2026-09-09.json` и
`data/miniapp-perf/g3-history-variants-2026-09-09.json`.

Нормальный non-empty closed list/detail, stale/history-changed и unknown-ref
остаются подтверждены живыми данными в §§21, 23 и 26. Этот раздел закрывает
error/history slice — **PASS**.

### 26. Повторный Telegram WebView и мобильный iPhone pass — 2026-09-09

После удаления временного `VITE_DEV_USER_ID` и чистого `pnpm build` Mini App
заново открыт из меню бота `@aster_copytrade_bot` в Telegram Desktop. После
короткого skeleton-экрана через примерно `5 s` показан production Home без
dev-banner: testnet, `0x3c45…F975`, `Копирование включено`, открытых позиций
`0`, `Стоп подтверждён 0 из 0`, риск `2%`. Это подтверждает чистый bundle на
реальном Telegram WebView после повторного открытия.

Через Computer Use тот же Mini App открыт в iPhone Mirroring, portrait
WebKit/WebView. Фактически прошли: Home; Positions `Открытые/Закрытые`; две
реальные закрытые BTCUSDT-карточки; detail с `+2,25 USDT`, входом/выходом
`0,037 BTC`; несколько вертикальных свайпов до `События сделки` и
`Исполнения`; раскрытие первого fill; native Back; Activity `Все` и `Сделки`;
owned `Открыть сделку`; Settings и Risk.

На физическом iPhone RiskRuler tap перевёл draft `2,0%→2,5%`, клавиша Right —
`2,5%→3,0%`, End — к `5,0%`, Home + три Right вернули draft на `2,0%`.
Сохранение в этой mobile-сессии не нажималось, поэтому server state не
менялся; риск остался `2%`. Реальные touch/scroll/keyboard paths WebKit
пройдены; haptic wrapper вызывался теми же пользовательскими tap/selection
действиями, но физический импульс не имеет отдельного машинного датчика в
Computer Use. Горизонтального page overflow или обрезанных подписей на
portrait-экране не обнаружено; узкие labels корректно переносятся.

Android WebView в подключённых Computer Use surfaces отсутствует, поэтому
Android-specific повтор и его haptic/WebView evidence остаются внешней
границей. iPhone slice — **PASS**, полный cross-device G3 — **OPEN только до
Android evidence**. Финальное состояние аккаунта после всех проходов: flat,
`open_orders=[]`, `allow_new_entries=1`, `risk_percent=0.02`,
`notifications_enabled=0`; preview продолжает работать на `:8085`.
