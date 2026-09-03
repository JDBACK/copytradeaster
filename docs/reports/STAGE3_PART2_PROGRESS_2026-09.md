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

## P2.3 — Settings root и Notifications: CLOSED (2026-09-02)

Backend (переиспользование, без новых service/repository/таблиц):

- `POST /miniapp/api/notifications` — чтение `NotificationSettingsDTO`
  (enabled, last_delivery_at, pending, failed) для user_id из проверенного
  initData (или testnet dev-header);
- `POST /miniapp/api/notifications/enabled` — mutation через существующие
  `application.set_notifications` (control-audit boundary сохранён); на
  HTTP boundary принимается только настоящий boolean: missing, строки,
  числа, списки и объекты → `400 invalid_input`; прочие typed ошибки →
  400/503 по общей карте;
- проводной сурфейс ограничен safe-полями DTO: chat_id, account_id,
  payload, last_error, event_key и прочие outbox-internal поля не
  отдаются; отключение уведомлений меняет только router-гейт доставки и
  не влияет на торговлю.

Frontend (`miniapp/`):

- `screens/SettingsScreen.tsx` по эталону `06-settings.png`: панель
  «Копирование» (tone по copy_state) + секции
  Настройки копирования / Подключение и безопасность / Поддержка со
  строками Риск на Stop, Уведомления, Статус Aster, Аккаунт и Builder,
  Безопасность, Помощь, Среда=Aster testnet. Chevron только у строк,
  реально открывающихся в P2.3 (Уведомления, Безопасность, Помощь);
  Риск/Статус/Аккаунт+Builder и Среда — read-only без притворной
  навигации. Значения из authoritative `DashboardDTO`; stale/degraded
  никогда не подписаны «Всё работает»; Builder — нейтральный masked
  wallet («Проверено» запрещено до P2.6 attestation).
- `screens/NotificationsScreen.tsx` (nested): switch `role="switch"` +
  `aria-checked`/`aria-busy`; UI меняется только по DTO сервера, при
  ошибке сохраняется последнее подтверждённое состояние, double tap
  заблокирован (disabled + reducer-гвардия); safe delivery-факты
  (последняя доставка / в очереди / не доставлено); явный copy
  «отключение не влияет на торговлю».
- `screens/InfoScreens.tsx` — короткие статические Help (как работает
  копирование, пауза-semantics, уведомления) и Security (модель
  разрешений без утверждений о текущем состоянии Agent).
- Pure view-models: `settingsView.ts` (copySummary, systemStatusValue,
  notificationsValue), `notificationsView.ts` (toggle state machine);
  `navigation.ts` + nested kind `security`; AppShell рендерит
  notifications/security/help через `NestedRoute` и typed stack +
  Telegram BackButton; CSS: settings-list/rows/section-label, switch.

Tests:

- HTTP (`tests/unit/miniapp/test_server.py`): auth обоих endpoint'ов,
  safe-сериализация (запрещённые outbox-поля), strict boolean ×8
  (missing/None/"true"/"false"/1/0/list/object → 400 invalid_input),
  malformed body, проброс boolean, typed errors (ValidationError →
  400 invalid_input, ServiceUnavailableError → 503), read-503.
- Frontend: `settings.test.tsx` (11) — view-model маппинги (включая
  honest tones: только ready → «Всё работает»), порядок секций/строк,
  ровно 3 кнопки-chevron, отсутствие «Проверено» и внутренних
  идентификаторов, aria-label статус-панели; `notifications.test.tsx`
  (10) — reducer: double-tap (reference-равенство), серверный DTO как
  источник истины, сохранение confirmed при ошибке; a11y switch
  (aria-checked/disabled/aria-busy); loading frame без switch;
  `navigation.test.ts` +1 (notifications/security/help на стеке).

Gates (точные счётчики — в `ENGINE_TEST_STATUS_2026-08.md`):

- offline pytest: 773 passed / 2 skipped / 32 deselected, coverage 87%,
  ruff check/format clean, `git diff --check` clean;
- frontend: vitest 97 passed / 11 файлов, oxlint 0/0, build clean;
- live: `tests/testnet/test_live_notifications_acceptance.py` — 1 passed
  за 26.99s на `fapi.asterdex-testnet.com` через production composition
  (NotificationRouter + `build_stage2_composition().application` +
  production aiohttp + настоящий `TelegramOutboxWorker`/Bot API) на
  реальной паре user/follower `tg-8264892202`:
  disable через HTTP → реальный master open + Stop → production engine
  скопировал позицию (signed positionRisk ≠ 0) при нуле новых
  пользовательских outbox rows; enable через HTTP → реальный partial
  close → durable outbox row (id=130) доставлена настоящим Telegram Bot
  API до статуса SENT (chat 8264892202); read endpoint подтвердил
  enabled=true и last_delivery_at; cleanup flat + исходный toggle
  восстановлены. Fake sender/FakeAster/MockTransport не использовались.
- bounded impeccable: detector по изменённым UI-файлам — 0 findings;
  один browser walkthrough (390×844, `preview_miniapp_engine.py` на
  копии prod-БД, порт 8093): settings rows/sections/chevrons на
  реальных данных, toggle-цикл вкл→выкл→вкл с обновлением UI по
  серверному DTO (подтверждён в preview SQLite), Help/Security,
  tab-switch сбрасывает stack; corrective pass не потребовался.

Owner acceptance закрыта владельцем в реальном Telegram WebView на commit
`fe84d2c`: Settings root и Notifications работают штатно. Три ручных
переключения прошли через production HTTP → общий application layer → SQLite;
`control_audit` зафиксировал `disabled → enabled → disabled` в
21:08:51–21:08:53. Владелец явно принял результат; отдельный screenshot как
дополнительный артефакт не требуется. P2.3 полностью закрыт, P2.4 не начат.

Corrective pass по ревью P2.3 (2026-09-02, три замечания, отдельный коммит):

1. Storage-изоляция live-гейта: тест работает на throwaway-копии реальной
   БД (`sqlite3.Connection.backup` → pytest `tmp_path`, `Settings.model_copy`
   для `db_path`/`log_path`). Все тестовые мутации — флаги, RiskPolicy,
   удаление execution_commands, telegram_users, outbox — только в копии;
   реальные Aster Testnet сделки и реальный Telegram Bot API сохранены.
   Re-run: 1 passed за 28.92s. Прод-БД не изменилась (row 8264892202
   `notifications_enabled`/`updated_at` нетронуты); боевой экземпляр на
   время теста остановлен (иначе прод-бот по WS дублировал бы копирование
   и перехватил phase-5 close) и поднят обратно через `scripts/dev-up.sh`.
2. Double-tap: синхронный in-flight lock `createToggleGate()` в
   `notificationsView.ts` закрывается до запуска `api.setNotifications` и
   открывается в `finally`; render-проверки остаются бэкстопом. Два
   regression-теста (два одновременных тапа → ровно один mutation; лишний
   `end()` безвреден). Browser-проверка на новом бандле (preview :8094):
   два `click()` в одном синхронном тике → ровно 1 POST
   `/miniapp/api/notifications/enabled`, `aria-checked` true→false один раз.
3. «Статус Aster»: `systemStatusValue(state, snapshotCapturedAt)` —
   «Всё работает» только при `ready` + непустом `snapshot_captured_at`;
   ready без snapshot → «Ожидание данных» (muted). Тесты ровно по кейсам
   ревью: ready без snapshot, ready со snapshot, stale со snapshot.
4. Corrective gate: targeted frontend tests 26 passed (notifications 12 +
   settings 14); Vitest 102 passed / 11 файлов (было 97), oxlint 0/0,
   `tsc -b && vite build` clean; ruff check/format и `git diff --check`
   clean по изменённому Python-файлу и дереву; полный offline suite не
   перезапускался (scope ревью), счётчики пунктов offline/live выше
   относятся к `fe84d2c`.

## P2.4 — Risk on Stop: CLOSED (2026-09-03, implementation/live complete)

Policy утверждена владельцем как developer (отклонение от чернового
`min 0.001/step 0.001/presets 0.005-0.01-0.02-0.05`): **min 0,5% / max 5% /
step 0,5% / presets 1%, 2%, 3%, 5%** — «от 1 до 5 с шагом 0.5, 0.5 тоже
оставить, пресеты 1 2 3 5».

Backend (переиспользование, без новых repository/service/engine слоёв):

- `RiskDTO` в `application/models.py`: только `risk_percent`, `min`,
  `max`, `step`, `presets`, `risk_base`; module-константы `RISK_MIN` /
  `RISK_MAX` / `RISK_STEP` / `RISK_PRESETS` / `RISK_BASE` — backend authority.
  Legacy `max_leverage`/`max_entry_deviation_pct` остались только во
  внутреннем `RiskPolicy`/storage/engine.
- `UserApplicationService._risk_dto` / `update_risk`: только `risk_percent`,
  `>= min`, `<= max`, `(value - min) % step == 0` — всё через `Decimal`;
  invalid/off-step/out-of-range/NaN/Infinity/float/bool/None → typed
  `ValidationError`; forged leverage/deviation fail closed; пустой patch
  теперь тоже `ValidationError` (раньше был read-through); persistence через
  существующий `patch_policy`; control audit (`attempt/success/rejected`,
  `changed_fields=risk_percent`) сохранён.
- Telegram `menu.risk` fallback-конструктор обновлён (рендер и так использовал
  только `risk_percent`); Telegram-пресеты 1%/2%/5% остаются валидными.

Transport (`miniapp/server.py`, существующий HMAC/initData boundary):

- `POST /miniapp/api/risk` → authoritative DTO;
  `POST /miniapp/api/risk/percent` с strict single-key границей
  (`{"risk_percent": "<decimal-string>"}`; legacy/extra/account_id →
  `400 invalid_input`, не-строки/NaN/Infinity/malformed → 400;
  application `ValidationError` → safe 400 без exception text).

Frontend (`miniapp/`):

- `RiskDTO` в `types.ts`; `api.risk` / `api.setRiskPercent` (fraction строкой).
- `riskView.ts`: `enumerateRiskSteps` (позиции линейки из DTO, exact
  string-math), `formatRiskPercent`, `isRiskAllowed` (по DTO, backend
  перепроверяет), `isRiskDirty`, `demoLossUsdt` (demo balance 1240 USDT).
- `screens/RiskScreen.tsx` — интерактивная шкала-линейка вместо пресетов и
  ручного ввода (редизайн по решению владельца 2026-09-03, Tsukuyomi):
  отдельный ряд кнопок удалён, тянуть можно саму шкалу в панели
  (`RiskRuler`: свайп / drag мышью / колесо / стрелки, snap к шагу,
  точное центрирование через rect-математику — `scrollIntoView smooth`
  глохнет на mandatory snap; мгновенный jump вместо догоняющей анимации).
  Крупное значение живьём следует за выбором (`Новое значение`), `Сейчас`
  держит подтверждённое; `enumerateRiskSteps` перечисляет позиции из DTO
  (parse ручного ввода удалён — invalid-класс исчез как явление).
  Второй проход по просьбе владельца: живой трекинг без задержки (rAF,
  выбор следует за пальцем, вибрация только на тапе и на остановке),
  «рыбий глаз» чёрточек (`--off` → scale/opacity, только transform,
  reduced-motion путь) и фиксированный курсор центра — значения идут
  к центру, а не наоборот. Доводка краёв: px-точный паддинг
  (`fitRulerPadding` от `clientWidth`, не `%` — WebKit иначе даёт 16px
  на крайних тиках) + контрольный добор центрирования и refit на resize.
  Подтверждение сейва: тост `Сохранено · X` (`role=status`, 2,2с,
  reduced-motion путь) — кнопка после save disabled и молчит без него.
  Решение по `presets` в контракте: поле остаётся (стабильность API P2.4,
  тесты на нём), клиент его не читает — шкала перечисляется из
  min/max/step и покрывает все пресеты; убирать нечего.
- Settings: строка «Риск на Stop» кликабельна с chevron (теперь 4
  chevron-кнопки); `AppShell.NestedRoute` (`export` для контракта) рендерит
  `risk` → `RiskScreen`; BackButton/tab-reset — существующий стек.
- CSS: `risk-*` классы на существующих токенах.

Tests:

- Application (`test_service.py`): обновлены 3 старых теста под новую
  policy + новый matrix `test_risk_p24_*` (exact constraints, min/max/step
  accepted, below/above/off-step/NaN/±Infinity/float/bool/None/list/dict
  rejected, forged rejected, durability read-after-write, storage legacy
  columns intact, audit attempt+success).
- HTTP (`tests/unit/miniapp/test_risk_server.py`, новый): auth, exact
  contract без legacy/internal полей, round-trip, 19 forged/malformed
  payloads + non-object body → 400 без вызова application,
  ValidationError → safe 400.
- Frontend (`risk.test.tsx`, 20 тестов после редизайна): enumerate/format/
  policy-gate/dirty/demo/`nearestRiskStep`/ruler-render/nested route;
  новый `riskBehavior.test.tsx` (jsdom + RTL, 5 behavioral: tap→dirty→
  single POST→confirmed+toast с double-tap gate, failed save держит
  confirmed без тоста, freeze линейки mid-save, scroll/settle race перед
  POST отброшен (негативный контроль без фикса — падает), legacy banner
  с явным сейвом); `settings.test.tsx` — 4 chevrons.

Gates:

- offline pytest: 816 passed / 2 skipped / 32 deselected (coverage — см.
  `ENGINE_TEST_STATUS_2026-08.md`); ruff check/format clean; mypy
  недоступен в текущем venv (не входит в offline gate этого окружения).
- frontend: Vitest 127 passed / 13 файлов, oxlint 0/0, `tsc -b &&
  vite build` clean (финальный билд без `VITE_DEV_USER_ID`; dev-билд
  использовался только для browser walkthrough, `dist/` git-ignored).
- live: `tests/testnet/test_live_risk_acceptance.py` — 1 passed за 33.97s
  (`fapi.asterdex-testnet.com`, production App + control-audited
  application + production aiohttp на `sqlite3.backup`-копии прод-БД):
  read policy → set A `0.01` → master BTCUSDT LONG 0.002 + Stop 73296.8 →
  reconcile → signed follower qty **0.004 == Decimal-расчёт** (equity
  1573.02, entry 77119.7) → cleanup → set B `0.03` → эквивалентная сделка →
  qty **0.012 == расчёт** (ratio 3.0x) → realized stop-loss обоих legs в
  пределах одного lot step → forged bodies → 400 → cleanup flat →
  fingerprint + `copy_settings` прод-БД до/после совпали.
- browser walkthrough (preview engine на копии прод-БД, `copyc1`, :8095/
  :8096, desktop + 390×844): Settings chevron → Risk clean (save
  disabled) → тап по линейке (dirty `Сейчас 1,0% После 4,0%`, демо
  пересчёт) → double-tap save → single mutation → тост `Сохранено`,
  Settings показывает новое; края `0.005/0.05` и центр — offset 0px;
  скелетон `role=status`, фокус тика после тапа, тики 64×54. Owner
  acceptance закрыт 2026-09-03 на iPhone в реальном Telegram WebView
  (боевой бот + временный cloudflare-туннель): линейка, вибрация,
  fish-eye, края, тост — «Работает».

External review corrective (remote `09a4d74`, 8.8/10, P0/P1 нет) — закрыт
одним коммитом: (1) legacy P2 — read-only проверка рабочей БД: все 5 строк
`copy_settings` на новой сетке (`0.01`×4 main/copyc1–3, `0.015`
tg-8264892202) + UI-страховка `nearestRiskStep` (exact integer math) с
баннером «такое значение больше недоступно» и явным сейвом, тихой
перезаписи нет; (2) ruler freeze P2 — `RiskRuler disabled={saving}`
(кнопки + scroll/wheel/drag/keyboard, `aria-disabled`) + 4 behavioral
теста в новом `riskBehavior.test.tsx` (jsdom + RTL: single POST +
confirmed + toast, failed save держит confirmed, freeze mid-save, legacy
banner); `risk.test.tsx` — 3 теста `nearestRiskStep`; (3) docs P3 —
PROGRESS/ENGINE_TEST_STATUS/SKELETON/adapted plan приведены к факту
`09a4d74` + corrective.

P2.4 ACCEPTED / CLOSED (external review + owner acceptance).

## P2.5 — System Status: ACCEPTED / CLOSED (2026-09-03)

Read-only control-plane vertical slice поверх существующего
`UserApplicationService.status()`. Второй status/health subsystem не
создавался; P2.0–P2.4 не рефакторились (только интеграция P2.5).

Backend (`application/models.py`, `application/service.py`):

- Новый public typed контракт `StatusDTO` (закрытые enum, без free-form):
  `state` (`ready|stale|degraded|blocked`), `system_state`, `copy_state`,
  `aster_state` (`ok|stale|blocked|unknown`), `permissions_state`
  (`valid|stale|blocked|unavailable`), `account_masked`, `signer_masked`,
  `positions_count`, `permissions`, `snapshot_captured_at`,
  `last_check_at`, `age_seconds`, `reason` (14 значений), `remediation`
  (`refresh|open_bot|null`). Старые `aster_connected`/`last_refresh_at`/
  `message` удалены из Mini App contract; Telegram presenter переведён на
  новый контракт.
- Authoritative источники: `system_state` — существующий `_system_state()`;
  `copy_state` — exact semantics Dashboard через общий private helper
  `_copy_state_and_message()` (Status и Dashboard не расходятся;
  `_dashboard_for` переиспользует его же, сообщения нетронуты);
  Aster — binding + доказуемая свежесть snapshot (без ping);
  permissions — существующий AgentValidation + настоящий signed refresh
  (`can_read/can_perp_trade=true`, `can_withdraw=false`, не просрочен,
  testnet override policy переиспользована, не выдумана).
- `READY` только при одновременных условиях: usable account, system
  READY, copy ACTIVE/PAUSED, свежий snapshot, валидные permissions,
  реальный capture timestamp. PAUSED сам по себе не unhealthy.
  Precedence: `blocked → degraded → stale → ready`.
- Stale lifecycle на существующем process-local runtime cache: при
  `refresh=True` fresh попытка → при failure fallback на cached с
  исходным `snapshot_captured_at`, timestamp не минтится; без cache —
  safe stale/blocked с `positions_count=null`. Auth/ownership/testnet
  ошибки — fail closed, не stale.
- Permission split: transient `ServiceUnavailable` → stale + `refresh`;
  `StaleOperation`/rejected → `blocked` + `open_bot` (старый valid cache
  не выдаётся за fresh).
- `last_check_at` — min(snapshot, permission evidence), не время попытки;
  `age_seconds` считает backend.
- Безопасность: только masked identities + safe facts; ключей, подписей,
  адресов, nonce/order id, raw payload, traceback/exception text в DTO нет.

Transport: `POST /miniapp/api/status` (`{}` → refresh false; present
`refresh` — только настоящий JSON boolean; non-object/malformed/
non-boolean/лишние поля → `400 invalid_input`; без account/user ID и без
query-альтернативы; auth `401/403`, application `503` без `str(exc)`).

Frontend: TS mirror + `api.status`; Settings-строка «Статус Aster» стала
nested navigation с chevron (теперь 5 chevron-рядов);
`system-status → SystemStatusScreen` в `NestedRoute`. Экран: заголовок,
health block, trust rail (Wallet/Agent из evidence, Builder всегда
neutral, Sync только при backend ready), шесть read-only diagnostic rows
без chevrons/actions, refresh, amber stale note, persistent bottom nav.
DTO только в local component state; первое открытие — `refresh=True`;
ручной refresh держит DTO + busy/disabled, failure — transport-stale
warning без зелёного healthy, success снимает; race закрыт sequence ref.
Remediation: `refresh` → `api.status(auth, true)`, `open_bot` →
`closeMiniApp()`, unknown — не исполняется. Ready — вторичная «Обновить».

Telegram: `render_status()` на новом контракте (typed labels/reason/
remediation notices), presenter-only, scope не расширен.

Что НЕ сделано (по плану): P2.6/P2.7 не начинались; Builder не verified;
copy engine/planner/executor/reconciler не тронуты; нет repository/
migration/Redis/health service/global store/polling/URL-actions/ping.

Gates (факт):

- backend: новый `tests/unit/application/test_status.py` (18 тестов:
  fresh/cheap-read/ready/stale-lifecycle/TTL/missing/transient/429-rate-limit/
  5xx/agent-not-found-blocked/persistence-failure/blocked/degraded/
  system-stale-unknown/disconnected/copy-parity/last-check/secrets/auth) + обновлённые `test_service.py`/
  `test_renderers.py`/`test_runtime.py`/`test_scale_gate.py`;
  corrective после review `c35e2b9`: transient permission errors (любой
  `AsterError` — 429/418/5xx/transport — и persistence/upsert failure)
  классифицируются как `ServiceUnavailable` → stale + refresh, а не как
  доказанный rejection; proven-invalid (`OnboardingError`/`ValueError`
  triple/expiry/whitelist/record checks) по-прежнему → blocked + open_bot;
  corrective-2 после второго review: `-2015/-2016` (AGENT_NOT_FOUND,
  конвенция direct/builder onboarding) исключены из transient — это
  доказанный rejection → blocked + open_bot, без 24h override fallback
  в resume-gate (fail-closed на отозванном Agent);
- HTTP: новый `tests/unit/miniapp/test_status_server.py` (19 тестов:
  auth/`{}`/exact bool/11 forged-non-bool/extra/non-object/malformed/
  exact DTO/forbidden fields/typed errors без leak);
- frontend: новый `systemStatus.test.ts` (8 unit) + новый
  `systemStatusBehavior.test.tsx` (13 behavioral: loading→fresh,
  refresh→fresh, backend stale/degraded/blocked, hard error→retry,
  transport-stale + снятие, race, remediation refresh/open_bot/unknown,
  read-only rows, Builder neutral, nested route); `settings.test.tsx` —
  5 chevron-кнопок;
- offline pytest: 855 passed / 2 skipped / 34 deselected, coverage 87%
  (порог >= 87%); ruff check/format clean; `git diff --check` clean;
- frontend: oxlint clean, Vitest 149 passed / 15 файлов, `pnpm build`
  clean (финальный билд без `VITE_DEV_USER_ID`; dev-билд только для
  browser walkthrough, `dist/` git-ignored);
- live: `tests/testnet/test_live_status_acceptance.py` — 1 passed за
  7.54s (production App + composition на `sqlite3.backup`-копии прод-БД,
  прод-БД не тронута): `status(refresh=True)` с настоящими signed
  snapshot + permission reads → `ready/none`, timestamps < 180s,
  `last_check <= snapshot_captured`, только masked identities, exact
  safe HTTP DTO, forged `"true"` → 400;
- browser walkthrough (preview engine на копии прод-БД, `copyc1`, :8085,
  390×844): Settings — 5 chevron-рядов; System Status fresh
  (`Всё работает`, 6 rows, trust rail, overflow 390/390); degraded
  (флаг на копии, движок честно вернул `degraded/degraded`,
  значения сохранены, amber «Система требует проверки»);
  4 root tabs, safe-area/bottom nav без изменений;
- owner Telegram WebView acceptance P2.5 — COMPLETE (2026-09-03, iPhone,
  боевой бот + trycloudflare-туннель, прод-БД, аккаунт tg-8264892202):
  fresh open («Всё работает», 6 rows); offline → transport-stale (данные
  целы, amber, зелёный снят) → online → fresh — owner-confirmed ok;
  degraded по синхронному флагу — дословно «Система требует проверки»;
  Resume gate: «Возобновить» под degraded отклонён («что-то пошло не так»),
  через ~20 сек после самоисцеления sweep'ом повторный resume прошёл,
  аккаунт active, система ready (проверено и серверной стороной:
  degraded_active=false, copy_state=active). P2.5 ACCEPTED / CLOSED.

## P2.6 — Account + Builder attestation: IMPLEMENTED / AWAITING REVIEW + OWNER ACCEPTANCE (2026-09-03)

Nested control screen поверх `UserApplicationService.account_builder()`.
P2.5 не рефакторился (только trust-интеграция); trading/copy engine,
planner/executor/coordinator/reconciler не тронуты; P2.7 не начинался.

Persistent identity (одна append-only migration, без history-таблицы):
`accounts` расширены `identity_source` (`direct|builder`), `builder_address`,
`builder_name`, `builder_max_fee_rate`, `builder_agent_expires_at_ms`,
`builder_verified_at` (nullable). Backfill: существующие rows →
`identity_source=direct`, Builder-поля NULL; legacy Builder status не
угадывается (unverified/not-applicable до authoritative onboarding).
Builder commit атомарно пишет credentials + current Agent validation +
`identity_source=builder` + verified snapshot. Direct onboarding/replacement
атомарно ставит `direct` и чистит proof. Disconnect в той же durable
transaction удаляет credentials + Agent validation + Builder snapshot
(local-only; remote revoke не реализован и не заявляется).

Authoritative verified — только одновременные signed reads backend:
владение account серверным TG user, current credential identity, живой
`GET /agent` = current signer с допустимыми permissions (read+perp,
без withdraw, expiry валиден), exact-match `GET /builder` запись текущего
account под configured Builder (`userAddress`, `builderAddress`,
фактический `maxFeeRate` == cap; имя — только presentation metadata).
Typed `BuilderDTO`, raw dict до application/frontend не доходит; float для
fee нет. Onboarding не COMMITTED как `builder`, если Agent подтверждён,
а Builder proof — нет (новые `BUILDER_NOT_CONFIRMED/BUILDER_MISMATCH/
FEE_CAP_MISMATCH`, без raw exchange text).

`AccountBuilderDTO` (transport-neutral, только masked данные, без
секретов/raw подписей/nonce): `identity_source`, `account_masked`,
`signer_masked`, `builder_address_masked`, `builder_name`, фактический
`max_fee_rate`, permission summary, Agent expiry, `verified_at`/last-check,
typed state (`verified|expiring|stale|blocked|unavailable|not_applicable`,
без `pending`), typed reason, remediation (`refresh|reauthorize|null` —
frontend сам reauthorization не решает).

Semantics: успешный combined check → `verified` + snapshot/`verified_at`;
близкий expiry → `expiring` + `reauthorize`; transient failure → `stale`
с видимым snapshot и возрастом (remediation `refresh`), без snapshot →
`unavailable`; authoritative mismatch/invalid/expired → не `verified`,
proof инвалидируется (`clear_builder_snapshot`, не воскресает после
restart). Cached read никогда не `verified` при stale.

Race: `account_version` marker (credentials+validation triple) снимается до
signed reads и сверяется перед commit; mismatch → `IDENTITY_CHANGED` без
commit; replacement/disconnect чистят proof атомарно (regression tests).

Reauthorization только при backend remediation: reuse Part 1 wallet/EIP-712
(payload привязан к текущему account/user/Agent/Builder, nonce/TTL/
ownership/anti-replay, чужой wallet отклоняется, duplicate submit
безопасен), same-signer (`reauth-` pending prefix, без ротации signer и
orphan Agent), после успеха account остаётся/возвращается paused. Отдельный
`reauthorize-prepare` внутри application boundary с server-side проверками;
frontend `force`-флагов нет, `/builder/prepare` не ослаблен.

HTTP (`miniapp/server.py`, только через `UserApplicationService`, strict
validation): `POST /miniapp/api/account-builder` (`{refresh: bool}`),
`builder/reauthorize-prepare|submit`; disconnect — существующие
confirmation endpoints, нового нет.

Frontend: `AccountBuilderScreen.tsx` (nested из Settings, Telegram
BackButton, 4 root tabs нетронуты; Settings row `Аккаунт и Builder` стала
chevron-navigation `{kind:'account-builder'}`): masked account/Agent/
Builder + fee cap + permissions/expiry + время server-side verification +
typed status; loading/verified/expiring/stale/blocked/unavailable/direct/
hard transport error; refresh не стирает данные, stale не выглядит
verified; `Перепроверить разрешения`, `Переавторизовать` только по backend
remediation, destructive `Отключить аккаунт` через confirmation Sheet +
existing token; duplicate taps — одна mutation. Единый trust source
`isBuilderVerified()` для Account/Home/Status (`builder_verified` в
DashboardDTO/StatusDTO; done только при authoritative `verified`).

Gates (факт):

- backend: 977 passed / 2 skipped / 40 deselected, coverage 87%
  (порог >= 87%); ruff check/format clean. Новые: storage identity (6),
  aster `test_builder_api` (9: bare-list/dict envelope, `""→None`,
  fee-cap decimal), attestation exact-match (13), reauthorize same-signer
  (12: no orphan, version binding, abort без commit), application DTO (34:
  verified/expiring/stale/unavailable/blocked/direct, remediation exact,
  masked-only, no secrets), adapter store reauth durability (16),
  credentials lifecycle (4: builder commit/direct clear/disconnect clear),
  HTTP `test_account_builder_server` (23: auth/spoofed user/strict body/
  refresh false+true/exact DTO/no full addresses/reauth без подмены);
  обновлены `test_builder.py`, `test_wp1.py`, `test_miniapp_builder_flow.py`
  (fake `GET /builder`), `test_status_server.py` (`builder_verified`);
- frontend: `accountBuilderBehavior.test.tsx` (11: verified/expiring+reauth/
  stale+snapshot/unavailable/blocked/direct/refresh-transition/transport
  не затирает DTO/disconnect-confirm/duplicate-taps/no-reauth таб/единый
  verdict/trust done только при verified) + обновлённые settings/home/
  systemStatus; Vitest 167 passed / 17 файлов, lint + `tsc` + build clean;
- live (Aster Testnet, scratch `sqlite3.backup`-копии прод-БД, прод-БД не
  тронута): attestation gate (direct/not_applicable честно) + local
  disconnect (flat → confirm → identity снесена, exchange-авторизация
  пережила — local-only доказан) + verified-onboarding
  `LIVE onboarded source=builder state=verified` за ~8s (fresh
  faucet-funded wallet `builder-gate`, стандартный Part 1 flow: prepare →
  in-process EIP-712 подпись → submit → agent+builder verify → preflight
  → atomic paused commit → DTO verified, `paused`, оба trust rail
  `builder_verified=true`). По пути зафиксированы live-факты биржи:
  never-deposited счёт — `-5050 after deposit` (openOrders/position-mode)
  и `-1000 No agent found` (balance/positionRisk), self-signed management —
  `-1000 No agent found` (нормализация mode — agent-ключом после APPROVAL);
  env-политика — HEDGE (`CT_POSITION_MODE`).

Что НЕ сделано (по плану): P2.7 (fee в ордерах), `/builder/userTrades`,
fee statement, remote revoke, history/ledger таблица, новый global store,
legacy auto-promotion, ослабление testnet/mainnet guards.

P2.6 IMPLEMENTED, ждёт независимого review + owner Telegram WebView
acceptance на current HEAD. P2.7 NOT STARTED.

## Следующие подчасти

- P2.5 — System Status: ACCEPTED / CLOSED (implementation/live/review/
  owner acceptance complete).
- P2.6 — Account + Builder attestation (`07-account-builder`):
  IMPLEMENTED, live gate complete (attestation + local disconnect +
  verified-onboarding `source=builder state=verified`), ждёт независимого
  review + owner Telegram WebView acceptance.
- P2.7 — Builder fee в обычном engine и финальное закрытие Stage 3:
  NOT STARTED.

Открытые решения владельца (остались перед соответствующими подчастями):

1. ~~точные `risk min/max/step/presets` (перед P2.4)~~ — утверждены
   2026-09-03: min 0,5% / max 5% / step 0,5% / presets 1/2/3/5.
2. ~~disconnect semantics: только локальный или + отдельный remote revoke
   (перед P2.6)~~ — решено 2026-09-03: local-only (remote revoke — отдельный
   main-wallet-signed destructive flow, не в P2.6).
3. auth-модель `/builder/userTrades` — bounded live probe (перед P2.7).

Part 2 backlog (не блокирует переход к P2.3):

- owner screenshots и Telegram WebView acceptance для P2.0–P2.2 закрыты
  2026-09-02; повторять их без UI-изменений не требуется;
- bounded $impeccable detector/browser pass выполнять только для новых или
  изменённых экранов P2.3+.
