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
