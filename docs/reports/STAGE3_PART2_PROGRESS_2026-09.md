# Stage 3 Part 2 — журнал прогресса

Дата: 2026-09-01

Ветка: `codex/stage-3-mini-app-part2` (baseline `main@660c1f5`).

## P2.0 — app shell и завершение Home: CLOSED (2026-09-01)

Сделано:

- typed route state без router dependency: `miniapp/src/navigation.ts`
  (ровно четыре root tabs `Главная/Позиции/Активность/Настройки`, nested
  stack для Risk/Notifications/System Status/Account+Builder/Help);
- `miniapp/src/components/AppShell.tsx`: постоянный bottom navigation,
  Telegram BackButton для nested screens, SVG-иконки, haptics;
- Gate/Wizard/Recovery/Success остались вне shell (`miniapp/src/App.tsx`);
- Home перенесён в shell без изменения логики Pause/Resume/freshness;
  compact verified trust summary (Wallet/Agent/Builder/Sync) сохранён;
  placeholder «появится в следующей версии» убирается в P2.1 вместе с
  подключением Positions DTO (по плану);
- tabs Positions/Activity/Settings — interim stub screens до P2.1–P2.3;
- CSS: `.shell`/`.bottom-nav`/`.nav-item`/trust-strip в `index.css`.

Найден и исправлен layout-баг при визуальной проверке:

- `.shell` имел `flex: 1` внутри `#root` (flex column, `min-height: 100dvh`);
  `flex-basis: 0%` перекрывал `height: 100dvh`, shell растягивался контентом
  (869px при viewport 577px) и bottom nav уходил ниже сгиба. Исправлено:
  `.shell` задаёт собственную definite height `100dvh` без `flex: 1`.

Gates:

- frontend: vitest 53/53 (navigation и corrective trust/accessibility cases), oxlint 0/0,
  `pnpm build` зелёный;
- offline backend suite (regression): 740 passed, без изменений;
- визуальная проверка: `scripts/preview_miniapp_engine.py` (полный engine
  boot на копии prod БД `data/miniapp-preview.sqlite3`, dev-user 900000001 →
  account `tg-8264892202`, порт 8085). После fix: shell = высоте viewport,
  nav прибит к низу; все четыре tabs переключаются без перезагрузки и потери
  session; Home показывает реальные данные (`Всё работает`, система готова,
  риск 1%, позиций 0, свежий snapshot). Скриншоты: `/tmp/p2-shots/01..04`.

Owner gate закрыт 2026-09-02 в фактическом Telegram WebView: Home и четыре
root tabs работают на current HEAD, safe-area/bottom nav проверены на
мобильном viewport. Реальные Pause/Resume ранее подтверждены владельцем на
том же Builder-follower и остаются действующим evidence Part 1/P2.0.

## P2.1 — read-only Positions: CLOSED (2026-09-02, corrective gate passed)

Сделано:

- Backend boundary: `PositionSummaryDTO`, `PositionPageDTO`, `PositionSide`, `PositionProtectionState` в `src/copytradeaster/application/models.py`;
- `PositionState` в `domain/models.py` расширен безопасным полем `unrealized_profit: Decimal = Decimal(0)` с сохранением строгих decimal инвариантов;
- `Coordinator._snapshot` и `MasterProjector` заполняют `unrealized_profit` из биржевого `PositionDTO.un_realized_profit`;
- `AppStage2RuntimeAdapter` сохраняет точные `(AccountState, captured_at: datetime)` timestamps (injectable clock) для кэша снепшотов; свежесть bounded TTL `max(2 × reconcile_interval_s, 30s)`: просроченный кэш → `is_stale=True` + `risk_to_stop_usdt=None`; при сбое `refresh=True` — fallback на кэш как stale с сообщением;
- `UserApplicationService.positions` и `_parse_position_summary`:
  - исключает нулевые позиции (`qty == 0`);
  - нормализует направления `LONG`/`SHORT` для `ONE_WAY` и `HEDGE`;
  - строгая валидация Stop: противоположная сторона (missing/unknown → `UNCONFIRMED`), флаги `reduce_only`/`close_position`, quantity-based покрытие `orig_qty >= abs(qty)` (нулевой → `UNCONFIRMED`, неполный → `PARTIAL_STOP`);
  - Decimal-расчёт `risk_to_stop_usdt` строго по свежему snapshot (`is_fresh=True`) и полностью подтверждённому Stop (с учётом `fee_buffer_pct`);
  - исключены сырые секреты, clientOrderId, account_id и orderId;
  - сортировка по алфавиту `symbol`;
- Transport boundary: `POST /miniapp/api/positions` с валидацией Telegram `initData` / dev-testnet заголовков;
- Frontend:
  - типы и `api.positions(...)`; хелперы `formatPrice`/`formatPnl`;
  - токены `--radius`/`--surface-2` в `index.css`;
  - `PositionsScreen` по эталону `03-positions.png` (warning copy вынесен в тестируемый `PROTECTION_WARNING`);
  - `accountMasked` пробрасывается в `PositionsScreen` и фиксируется сразу после onboarding;
  - Home: последовательный fetch (dashboard refresh → positions из кэша);
- Gates (актуальные счётчики — в `ENGINE_TEST_STATUS_2026-08.md`):
  - offline pytest: passed, coverage ≥ 87%, ruff clean (включая integration-регресс resume/tuple с настоящим `AppStage2RuntimeAdapter`);
  - frontend: vitest passed (включая `positions.test.tsx` — renderToStaticMarkup UI-контракт: warning copy, read-only, скрытие риска при null), oxlint clean, build clean;
  - in-browser walkthrough мобильного viewport: пустой экран и активная карточка с предупреждением;
  - live acceptance на Aster Testnet: 8/8 фаз через реальный production copy engine (master → engine → follower `copyc1` → Mini App API → signed positionRisk/openOrders сверка → Stop/TP перестановка → partial close → warning → flat cleanup).
  - owner Telegram WebView acceptance 2026-09-02: Builder-follower
    `0x3c45…F975` показал реальную BTCUSDT LONG карточку с Entry/Mark/PnL,
    Stop, `risk_to_stop_usdt` и leverage.

## P2.2 — safe Activity timeline (Stage 3 part 2): CLOSED (2026-09-02, live acceptance passed)

Backend boundary:

- `ActivityItem` в `application/models.py` расширен безопасными полями
  `category` (trade/system), `title`, `status` (completed/pending/failed),
  `symbol`, `occurred_at` и optional `pending_age`/`remediation`; старые
  `action`/`result`/`detail` удалены, `command_key`, `account_id`, addresses,
  nonce, `client_order_id`, raw Aster code, payload, traceback и любые
  внутренние exception-строки фильтруются на backend и никогда не
  попадают в DTO.
- `ActivityCategory` / `ActivityStatus` / `ActivityFilter` (all/trade/system)
  в `application/models.py`; `ApplicationProtocol.activity` принимает
  `filter`.
- `_map_activity_item` и `_safe_activity_title` маппят journal kind →
  safe Russian title (`Открыта позиция BTCUSDT`, `Поставлен защитный Stop · BTCUSDT`,
  `Плечо подготовлено: 200× · BTCUSDT` и т.д.); `symbol` извлекается
  строго из `payload.symbol` после JSON-парсинга с верхней границей и
  whitelist символов; `pending_age` ограничен `now - occurred_at`;
  `remediation` присутствует только для `failed`/`UNKNOWN` и не
  содержит raw Aster code или `last_error`.
- `_activity_rows` (SQL fallback) и `AppStage2RuntimeAdapter.read_activity`
  выбирают `payload` дополнительно, чтобы backend мог извлечь `symbol` без
  раскрытия остального JSON. Category predicate применяется в SQL до
  `ORDER BY/LIMIT/OFFSET`: страницы и `has_next` считаются по выбранной
  категории, а неизвестные journal kinds сохраняются в fail-safe категории
  `system`.
- Транспорт: `POST /miniapp/api/activity` с валидацией Telegram initData
  (или `CT_MINIAPP_DEV_UNSIGNED` для testnet dev session), тело
  `{"page": N, "filter": "all|trade|system"}`; невалидный `page` или
  `filter` → 400 с `invalid_input`; `application.activity` бросает
  типизированный `ValidationError` → 400; прочие `ApplicationError` →
  соответствующие 4xx/503.

Frontend (`miniapp/`):

- `types.ts`: `ActivityCategory`, `ActivityStatus`, `ActivityFilter`,
  `ActivityItemDTO`, `ActivityPageDTO`; `api.activity(auth, filter, page)`.
- `activityView.ts` — pure view-model (`dayBucket`, `groupActivityByDay`,
  `activityStatusLabel`, `activityStatusTone`, `activityEmptyCopy`,
  `isActivityStatus`); бакеты day вычисляются в UTC, чтобы
  идентичный instant группировался одинаково в любой таймзоне.
- `screens/ActivityScreen.tsx` — заменил interim `StubScreen` в
  `AppShell.tsx`; заголовок «Активность», подзаголовок «Безопасный
  журнал действий», три фильтра (Все/Сделки/Система), timeline с
  группами Сегодня/Вчера/Ранее, глифы для completed/pending/failed
  (зелёная галка / янтарная точка / красный крестик), русские titles
  и statuses, pending-age секунды, remediation под failed-строками,
  loading/error/empty states, кнопка «Показать ещё» с server-side
  `page`+1, стабильный append без потери уже отображенных строк.
  Строго read-only: ни торговых кнопок, ни raw-кодов, ни технических
  payload. Wallet chip / testnet badge — как в `PositionsScreen`.
- CSS: `.segmented`, `.activity-timeline`, `.activity-row`, `.activity-day-label`,
  `.activity-marker`/`.activity-connector`, `.activity-status` (tone-*),
  `.activity-remediation`, `.activity-safe-note`.

Tests:

- Unit (`tests/unit/application/test_service.py`):
  - `test_activity_safe_mapping_filters_and_sorts` — полный набор journal
    rows (open/close/stop/tp/cancel/leverage + pending + failed +
    unknown), проверка safe DTO shape, фильтров trade/system/all,
    pending_age (40–60s окно), типизированной remediation copy, и
    недопустимости 17 запрещённых полей (command_key, account_id,
    address, client_order_id, nonce, private_key, -1003, boom, …);
  - `test_activity_pagination_boundary` — 25 MARKET_OPEN строк, точная
    пагинация 10/10/5 через `page=0/1/2`, has_next/has_previous,
    стабильность фильтра.
  - `test_activity_category_filter_paginates_the_filtered_set` — смешанный
    journal trade/system, страницы 10/4 для каждой категории, без пропусков
    и дубликатов.
  - `test_activity_paging_validation_and_notification_toggle` —
    существующий, обновлён под новые поля и filter-валидацию.
- HTTP (`tests/unit/miniapp/test_server.py`):
  - `test_activity_endpoint_serializes_safe_dto` — сериализация DTO,
    проверка отсутствия command_key/client_order_id/account_id/payload/address/nonce/private_key;
  - `test_activity_endpoint_passes_page_and_filter` — проброс body params;
  - `test_activity_endpoint_requires_auth` — 401 без initData/dev-header;
  - `test_activity_endpoint_rejects_invalid_filter` — 400 на неизвестный
    filter (валидация на HTTP boundary);
  - `test_activity_endpoint_rejects_invalid_page` — 400 на отрицательную
    страницу;
  - `test_activity_endpoint_serializes_pending_age` — pending status
    корректно сериализуется с `pending_age`.
- Frontend (`miniapp/src/activity.test.tsx`, 11 tests) — pure view-model
  + production `ActivityScreen` render:
  - `activityStatusLabel`, `activityStatusTone`, `dayBucket` (UTC),
    `groupActivityByDay`, `activityEmptyCopy`, `isActivityStatus`;
  - `ActivityScreen` render в loading state: title, brand, testnet badge
    или wallet chip, skeleton «Загрузка журнала»;
  - рендер не содержит торговых кнопок (`<button` отсутствует), ни
    close/reverse/buy/sell/закрыть/перевернуть/купить/продать, ни
    command_key/client_order_id/account_id/private_key/nonce/payload/traceback.

Live gate (Aster Testnet, `tests/testnet/test_live_activity_acceptance.py`):

- Production copy engine: master открыл BTCUSDT LONG + Stop Market →
  `projector.rebuild_full` + `reconcile_follower("copyc1")` →
  `execution_commands` journal на `copyc1` (предварительно
  `DELETE FROM execution_commands WHERE account_id='copyc1'`);
- `POST /miniapp/api/activity` через production aiohttp path и реальный
  `UserApplicationService`; Mini App endpoint вернул safe DTO с
  `category: trade` / `title: "Открыта позиция BTCUSDT"` /
  `status: completed` / `symbol: "BTCUSDT"` для open и
  `"Поставлен защитный Stop · BTCUSDT"` для Stop;
- trade-filter вернул ровно те же записи с `category == "trade"` и
  `symbol == "BTCUSDT"`; production `Executor` выполнил реальный
  `SET_LEVERAGE` на Aster Testnet и записал его в journal; system-filter
  вернул непустую отдельную выборку с `category == "system"`,
  `symbol == "BTCUSDT"` и заголовком с префиксом «Плечо»;
- ни в одном item на проводе нет command_key / client_order_id /
  account_id / payload / address / nonce / private_key или raw Aster
  code (`MARKET_OPEN`, `PLACE_STOP`, `MARKET_CLOSE`, `SET_LEVERAGE`,
  `-1003`);
- partial close от мастера добавил `MARKET_CLOSE` row → Mini App
  endpoint вернул `"Закрыта позиция BTCUSDT"`;
- cleanup flat; повторный опрос endpoint подтвердил сохранение safe
  DTO shape (никаких утечек после flatten).

Gates (точные счётчики — в `ENGINE_TEST_STATUS_2026-08.md`):

- offline pytest: 759 passed / 2 skipped / 31 deselected, coverage 87%,
  ruff check/format clean,
  `git diff --check` clean;
- frontend: vitest 75 passed / 9 файлов (11 новых в `activity.test.tsx`),
  oxlint 0/0, `pnpm build` clean;
- live: corrective `tests/testnet/test_live_activity_acceptance.py` passed за 27.15s
  против `https://fapi.asterdex-testnet.com`; `preview_miniapp_engine.py`
  на порту 8091 подтвердил отдачу safe DTO через тот же endpoint
  (3 trade items: `Закрыта позиция BTCUSDT`, `Поставлен защитный Stop · BTCUSDT`,
  `Открыта позиция BTCUSDT`) и HTML `/miniapp/` с собранным bundle.

Owner Telegram WebView acceptance закрыт 2026-09-02 на current HEAD:
production engine скопировал master BTCUSDT LONG + Stop на
`copyc1`/`copyc2`/`copyc3`/`tg-8264892202`; владелец на Builder-follower
`0x3c45…F975` подтвердил Positions и Activity screenshots. Activity показал
safe записи «Открыта позиция BTCUSDT» и «Поставлен защитный Stop · BTCUSDT».
После приёмки live-сценарий закрыт через master; все followers подтвердили
cancel + close и вернулись в flat с reconciliation 0 действий.

## Следующие подчасти

- P2.3 — Settings root и Notifications (`06-settings`).
- P2.4 — Risk on Stop (`05-risk-settings`).
- P2.5 — System Status (`08-system-status`).
- P2.6 — Account + Builder attestation (`07-account-builder`).
- P2.7 — Builder fee в обычном engine и финальное закрытие Stage 3.

Открытые решения владельца (нужны перед соответствующими подчастями):

1. точные `risk min/max/step/presets` (перед P2.4);
2. disconnect semantics: только локальный или + отдельный remote revoke
   (перед P2.6);
3. auth-модель `/builder/userTrades` — bounded live probe (перед P2.7).

Part 2 backlog (не блокирует переход к P2.3):

- owner screenshots и Telegram WebView acceptance для P2.0–P2.2 закрыты
  2026-09-02; повторять их без UI-изменений не требуется;
- bounded $impeccable detector/browser pass выполнять только для новых или
  изменённых экранов P2.3+.
