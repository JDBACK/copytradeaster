# Статус работ — copytradeaster (этапы 1–3)

Журнал ведётся с 2026-08-22. Формат: дата → факт → статус. Актуальная рабочая
ветка Stage 3, часть 2: `codex/stage-3-mini-app-part2`.

## Текущее состояние

- **Stage 3, часть 2 (P2.6 — Account + Builder attestation) — IMPLEMENTED /
- **Stage 3, часть 2 (P2.6 — Account + Builder attestation) — ACCEPTED /
  CLOSED (2026-09-03, ветка `codex/stage-3-mini-app-part2`):** nested control screen поверх
  `UserApplicationService.account_builder()` — persistent `identity_source`
  + Builder snapshot (одна append-only migration, backfill `direct`),
  authoritative verified только одновременными signed `GET /agent` +
  exact-match `GET /builder` (typed `BuilderDTO`, без float), typed
  `AccountBuilderDTO` (только masked, state verified/expiring/stale/
  blocked/unavailable/not_applicable, remediation refresh/reauthorize/null),
  race-gate `account_version` + `IDENTITY_CHANGED`, same-signer
  reauthorization (`reauth-`, paused сохраняется), local-only disconnect с
  атомарной инвалидацией proof, единый trust source `isBuilderVerified()`
  для Account/Home/Status. P2.5 не рефакторился; engine/P2.7 не тронуты.
  1. **Offline gate:** offline pytest **977 passed / 2 skipped / 40
     deselected**, coverage **87%** (порог >= 87%); ruff check/format
     clean. Новые кейсы (117): storage identity (6), aster builder_api (9),
     attestation exact-match (13), reauthorize same-signer (12),
     application DTO (34), adapter reauth durability (16), credentials
     lifecycle (4), HTTP account_builder_server (23).
  2. **Frontend gate:** Vitest **167 passed / 17 файлов**
     (`accountBuilderBehavior.test.tsx` 11 + обновлённые settings/home/
     systemStatus), lint + `tsc` + build clean.
  3. **Live gate (Aster Testnet, scratch `sqlite3.backup`-копии прод-БД,
     прод-БД не тронута):**
     `tests/testnet/test_live_account_builder_acceptance.py` — attestation
     gate + local disconnect (exchange-авторизация пережила disconnect —
     local-only доказан) + verified-onboarding **`LIVE onboarded
     source=builder state=verified`** (fresh faucet-funded wallet,
     стандартный Part 1 flow, atomic paused commit, оба trust rail
     `builder_verified=true`). Live-факты биржи: never-deposited счёт —
     `-5050 after deposit` / `-1000 No agent found`; self-signed management
     отвергается; env-политика HEDGE.
     Детали — `STAGE3_PART2_PROGRESS_2026-09.md` (раздел P2.6).
- **Stage 3, часть 2 (P2.7 — Builder fee в copy-engine) — ACCEPTED / CLOSED
  corrective review пройден 2026-09-04, ветка
  `codex/stage-3-mini-app-part2`):** account-specific Aster Builder
  attribution в production copy-engine: `PlaceOrderRequest` + optional
  `builder`/`feeRate` (wire names, пара-инвариант, Decimal canonical string,
  NaN/Infinity/negative запрещены, absolute Futures cap `0.001` fail-closed,
  direct-сериализация byte-compatible), маленький
  `BuilderAttributionProvider` на composition boundary (DIRECT /
  BUILDER_VALID / BUILDER_UNAVAILABLE, durable P2.6 proof + current
  signer + Agent permissions/actual expiry + config match persisted
  address/fee vs `CT_BUILDER_ADDRESS`/`CT_MAX_FEE_RATE`, UI TTL и
  EXPIRY-warning не являются execution expiry), Executor применяет snapshot
  после journal identity под per-account mutation lock (command key и journal
  payload не зависят от metadata, `_with_coid` сохраняет attribution, retry
  сохраняет snapshot, UNKNOWN query-before-retry без fallback, boot recovery
  без Builder metadata), safety fallback (entry fail-closed до HTTP,
  close/Stop/TP без fields + degraded signal, cancel/leverage не блокируются).
  Planner/coordinator/reconciler не менялись. Frontend не менялся.
  1. **Offline gate:** offline pytest **1075 passed / 2 skipped / 42
     deselected**, coverage **87%** (порог >= 87%); ruff check/format
     clean. Новые кейсы (73): `test_builder_attribution.py` (60: payload +
     routing + strict tri-state permissions + time_in_force) + `test_builder_executor.py` (13: idempotency/fallback/race/
     emergency/security).
  2. **Frontend gate:** Vitest **168 passed / 17 файлов**, oxlint clean,
     `vite build` clean (без изменений Mini App).
  3. **Live gate (Aster Testnet, scratch копия прод-БД, прод-БД не
     тронута):** `test_builder_engine_attribution_live.py` — один
     production engine доказывает Builder и direct routing
     (`Projector -> Coordinator -> Planner -> Executor -> journal -> real
     AsterClient`, typed spy перед реальным `place_order`): Builder MARKET
     entry + STOP_MARKET + partial reduction + TAKE_PROFIT_MARKET + full
     close + final flat/no-orders, каждый Builder order с exact approved
          builder и Decimal-equal fee, direct всегда без fields; journal payload
     без `builder`/`feeRate`; честный above-cap probe: CONTROL с approved fee принят
     и отменён, тот же shape с превышением fee отвергнут именно -5026; слабый
     standalone-тест удалён, в `PlaceOrderRequest` добавлено optional `time_in_force`;
     `/builder/userTrades` с bare builder identity не queryable — честный non-gating
     limitation (как в Part 1 e2e). Full testnet suite: **41 passed / 1 skipped**
     — полный gate зелёный (review corrective: strict permissions, честный probe,
     стабилизация positions-теста с убранной INTENTIONAL_FLAT-интерференцией).
     4. **Owner acceptance (2026-09-03):** Computer Use-driven live-стенд, prod-БД: **9/9 PASS** (Home / Positions read-only / Activity / Risk-линейка без сохранения / Status ready / Account+Builder direct verified / Notifications вкл+выкл / Pause+Resume с confirm / регресс P2.0-P2.6); в прод-БД 0 builder-строк — builder trust покрыт P2.6 acceptance и scratch-live P2.7; стенд возвращён (active, риск 2,0%, уведомления выкл); скриншоты: docs/reports/acceptance-p27/ (12 PNG).
     Детали — `STAGE3_PART2_PROGRESS_2026-09.md` (раздел P2.7).
- **Stage 3, часть 2 (P2.5 — System Status) — ACCEPTED / CLOSED
  (2026-09-03):** read-only control-plane slice поверх
  `UserApplicationService.status()` — новый typed `StatusDTO`
  (`state/system_state/copy_state/aster_state/permissions_state/masked
  identities/positions_count/permissions/snapshot_captured_at/last_check_at/
  age_seconds/reason/remediation`), precedence
  blocked → degraded → stale → ready, stale lifecycle на существующем
  runtime cache без minted timestamps, `POST /miniapp/api/status` со
  strict boolean boundary, `SystemStatusScreen` с transport-stale honesty
  и race guard, Telegram presenter на новом контракте. P2.6/P2.7 не
  начинались, Builder не verified.
  1. **Offline gate:** offline pytest **855 passed / 2 skipped / 34
     deselected**, coverage **87%** (порог >= 87%); ruff check/format
     clean, `git diff --check` clean. Новые кейсы:
     `tests/unit/application/test_status.py` (18),
     `tests/unit/miniapp/test_status_server.py` (19).
  2. **Frontend gate:** Vitest **149 passed / 15 файлов**
     (`systemStatus.test.ts` 8 unit + `systemStatusBehavior.test.tsx`
     13 behavioral + `settings.test.tsx` с 5 chevron-кнопками), oxlint
     clean, `tsc -b && vite build` clean.
  3. **Live gate (Aster Testnet):**
     `tests/testnet/test_live_status_acceptance.py` — **1 passed за
     7.54s** на `sqlite3.backup`-копии прод-БД (прод-БД не тронута):
     настоящие signed snapshot + permission reads → `ready/none`.
  4. **Browser walkthrough:** preview engine на копии прод-БД
     (`copyc1`, :8085, 390×844) — fresh + degraded, overflow 390/390.
  5. **Owner Telegram WebView acceptance P2.5 — COMPLETE (2026-09-03,
     iPhone, боевой бот + туннель, прод-БД):** fresh open; offline →
     transport-stale (данные целы, amber) → online → fresh; degraded
     («Система требует проверки»); Resume под degraded отклонён, после
     самоисцеления sweep'ом resume прошёл (аккаунт active, ready).
     Детали — `STAGE3_PART2_PROGRESS_2026-09.md` (раздел P2.5).
     Статус P2.5: ACCEPTED / CLOSED.
- **Stage 3, часть 2 (P2.4 — Risk on Stop) — CLOSED
  (2026-09-03, implementation/live/owner acceptance complete + external
  review corrective applied):**
  1. **Backend Application:** public `RiskDTO` больше не содержит
     `max_leverage`/`max_entry_deviation_pct` — только `risk_percent`,
     `min`/`max`/`step`/`presets`, `risk_base` (backend authority, все
     проверки через `Decimal`). Policy утверждена владельцем как developer
     (отклонение от чернового плана: min `0.005` = 0,5%, max `0.05` = 5%,
     step `0.005` = 0,5%, presets `0.01/0.02/0.03/0.05` = 1/2/3/5%).
     `update_risk()` меняет только `risk_percent`: range + step enforced,
     invalid/off-step/out-of-range/NaN/Infinity/float/bool → typed
     `ValidationError`, forged leverage/deviation fail closed, persistence
     через существующий `SettingsRepo`, control audit сохранён. Legacy
     `RiskPolicy`/storage/engine для торгового ядра нетронуты.
  2. **Transport:** `POST /miniapp/api/risk` (чтение) и
     `POST /miniapp/api/risk/percent` (mutation) поверх существующего
     HMAC/initData boundary. Строгая single-key граница: только
     `risk_percent` строкой; missing/bool/null/number/object/array/NaN/
     Infinity/malformed и любые лишние поля (legacy/forged/account_id) →
     `400 invalid_input`; в ответе только safe DTO, без exception text.
  3. **Frontend (`miniapp/`):** `RiskDTO` в `types.ts`;
     `api.risk`/`api.setRiskPercent`; Settings-строка «Риск на Stop»
     кликабельна с chevron; `{ kind: 'risk' }` рендерит новый `RiskScreen`
     (отклонение от `05-risk-settings.png` по решению владельца 2026-09-03:
     вместо пресетов и ручного ввода — интерактивная шкала-линейка в панели:
     `RiskRuler` со свайпом/drag/колесом/стрелками, snap, px-точным
     центрированием `fitRulerPadding`, живым трекингом rAF, fish-eye
     чёрточек и фиксированным курсором; крупное значение следует за
     выбором, `Сейчас` держит confirmed, тост `Сохранено · X` после save,
     линейка заморожена на время POST, legacy off-grid значение даёт
     баннер с явным перевыбором вместо молчания).
     Pure `riskView.ts` (string-math, без float); confirmed меняется
     только по server response.
  4. **Offline gate:** offline pytest **816 passed / 2 skipped / 32
     deselected**, coverage **87%** (порог >= 87%);
     ruff check/format clean. Новые кейсы:
     application policy matrix (min/max/step accepted, below/above/
     off-step/NaN/Infinity/non-Decimal/forged rejected, durability,
     audit) и HTTP boundary (auth/read/round-trip/19 malformed-forged
     payloads/non-object/ValidationError→safe 400, no legacy fields).
  5. **Frontend gate:** Vitest **127 passed / 13 файлов** (`risk.test.tsx`,
     20 тестов: enumerate/format/policy-gate/dirty/demo/nearest/ruler/
     nested route; новый `riskBehavior.test.tsx`, 5 behavioral: tap→dirty→
     single POST→confirmed+toast, failed save держит confirmed, freeze
     линейки mid-save, scroll/settle race перед POST отброшен, legacy
     banner; `settings.test.tsx` — 4 chevron-кнопки), oxlint 0/0,
     `tsc -b && vite build` clean.
  6. **Live gate (Aster Testnet):**
     `tests/testnet/test_live_risk_acceptance.py` — **1 passed за
     33.97s** на throwaway `sqlite3.backup`-копии прод-БД (рабочая БД
     доказано не изменилась: fingerprint + `copy_settings` до/после).
     Production path: HTTP read policy → set A=`0.01` → master LONG
     0.002 + Stop 73296.8 → reconcile → signed follower qty **0.004**
     == Decimal-расчёт; cleanup → set B=`0.03` → эквивалентная сделка →
     qty **0.012** == расчёт (ratio 3.0x); realized stop-loss обоих legs
     в пределах одного lot step от configured; forged `max_leverage`/
     `max_entry_deviation_pct` → 400. Cleanup flat best-effort.
  7. **Browser walkthrough + owner acceptance:** preview engine на копии
     прод-БД (`copyc1`, порты 8095/8096): Risk clean (save disabled) →
     тап по линейке (dirty `Сейчас 1,0% После 4,0%`, демо пересчёт) →
     double-tap save → single mutation → тост `Сохранено`, Settings
     показывает новое; края `0.005/0.05` и центр `0.03` — offset 0px;
     mobile 390×844 — иерархия по макету. Owner acceptance закрыт
     2026-09-03 в реальном Telegram WebView на iPhone (боевой бот +
     временный туннель, прод-БД): линейка, вибрация, fish-eye, края,
     тост — «Работает». Журнал —
     `STAGE3_PART2_PROGRESS_2026-09.md`.
  8. **External review corrective (remote `09a4d74`, оценка 8.8/10, P0/P1
     нет):** (а) legacy proof — все 5 строк `copy_settings` рабочей БД
     на новой сетке (`0.01`×4, `0.015`; read-only проверка) + UI-страховка
     `nearestRiskStep` с баннером и явным сейвом, без тихой перезаписи;
     (б) линейка `disabled` на время save (кнопки + scroll/wheel/drag/
     keyboard, `aria-disabled`); (в) living docs приведены к факту
     (см. пп. 3/5/7, PROGRESS, SKELETON, adapted plan deviation note).
     Follow-up ревью `40b8323` (последний P2-low): полный freeze —
     при `disabled` отменяются pending rAF/trailTimer, их колбэки
     перепроверяют флаг через ref, контейнер физически лочится
     `.risk-ruler[aria-disabled='true'] { overflow-x: hidden }`, после
     выхода из saving — гарантированный `centerFraction(active)` +
     repaint; regression-тест гонки (scroll→save до срабатывания
     колбэков, stale settle обязан не тронуть selection; негативный
     контроль: без фикса тест падает) — Vitest 127/13.
- **Stage 3, часть 2 (P2.3 — Settings root и Notifications) — CLOSED
  (2026-09-02, implementation/live/owner acceptance complete):**
  1. **Backend Transport:** `POST /miniapp/api/notifications` (чтение) и
     `POST /miniapp/api/notifications/enabled` (mutation) в
     `miniapp/server.py` поверх существующих `NotificationSettingsDTO`,
     `notification_settings()` и `set_notifications()` — новый
     service/repository/таблицы не создавались. На HTTP boundary принимается
     только настоящий boolean: missing/string/number/list/object →
     `400 invalid_input`; application-level strict-bool check и существующий
     control-audit boundary `set_notifications` сохранены. user_id только из
     проверенного initData (или testnet dev-header). Проводной DTO-сурфейс:
     `enabled`, `last_delivery_at`, `pending`, `failed`; chat_id,
     account_id, payload, last_error и другие outbox-internal поля никогда
     не покидают backend. Отключение уведомлений меняет только
     `telegram_users.notifications_enabled` (router-гейт доставки) и не
     затрагивает торговый путь.
  2. **Frontend (`miniapp/`):** interim `StubScreen` заменён настоящим
     `SettingsScreen` по эталону `06-settings.png`: статус-панель
     «Копирование» и capability-список с секциями (Настройки копирования /
     Подключение и безопасность / Поддержка): Риск на Stop, Уведомления,
     Статус Aster, Аккаунт и Builder, Безопасность, Помощь, Среда=Aster
     testnet. Chevron только у реально открывающихся в P2.3 строк
     (Уведомления, Безопасность, Помощь); Риск/Статус/Аккаунт+Builder
     (P2.4/P2.5/P2.6) и статическая Среда — read-only строки без chevron.
     Значения из authoritative `DashboardDTO` (`risk_percent`,
     `system_state`, `snapshot_captured_at`, `notification_enabled`,
     `account_masked`); stale/degraded никогда не подписаны «Всё
     работает», а «Статус Aster» читает «Всё работает» только при
     `ready` **и** непустом `snapshot_captured_at` — ready без snapshot
     показывает «Ожидание данных»; Builder — нейтральный masked wallet
     без «Проверено» до attestation (P2.6).
     Вложенный `NotificationsScreen` через существующий typed route stack +
     Telegram BackButton (новый nested kind `security`); toggle — button
     `role="switch"` с `aria-checked`/`aria-busy`, UI меняется только по
     DTO, возвращённому сервером; при ошибке сохраняется последнее
     подтверждённое состояние + error box; double tap заблокирован
     синхронным in-flight lock `createToggleGate()` до запуска API
     (disabled во время сохранения и reducer-гейт `save_requested` —
     render-бэкстопы). Безопасные delivery-факты: последняя доставка,
     в очереди, не доставлено. Pure view-models `settingsView.ts`
     (copySummary/systemStatusValue/notificationsValue) и
     `notificationsView.ts` (toggle state machine + toggle gate).
     AppShell рендерит nested screens (`notifications`/`security`/`help`)
     через `NestedRoute`.
  3. **Offline gate:** offline pytest **773 passed / 2 skipped / 32
     deselected** за 17.78s, coverage **87%**; ruff check/format clean,
     `git diff --check` clean. Новые HTTP-кейсы
     (`tests/unit/miniapp/test_server.py`): auth для обоих endpoint'ов,
     safe-DTO сериализация без outbox-internal полей, strict boolean
     (8 вариантов невалидного payload → 400 invalid_input), malformed
     body → 400, проброс реального boolean в application, typed errors
     (ValidationError→400 invalid_input, ServiceUnavailableError→503).
  4. **Frontend gate:** Vitest **97 passed / 11 файлов** — новые
     `settings.test.tsx` (11: view-model маппинги, honest tones для
     stale/degraded, порядок секций и строк, ровно 3 кнопки-chevron,
     отсутствие «Проверено» и внутренних идентификаторов, aria-label
     статус-панели) и `notifications.test.tsx` (10: toggle state machine —
     double-tap reference-равенство, серверный DTO как источник истины,
     сохранение confirmed при ошибке; a11y switch; loading frame),
     `navigation.test.ts` +1 (notifications/security/help на стеке);
     oxlint 0/0, `tsc -b && vite build` clean.
  5. **Live gate (Aster Testnet + реальный Telegram Bot API):**
     `tests/testnet/test_live_notifications_acceptance.py` — **1 passed за
     26.99s**. Production-композиция: `NotificationRouter` + App +
     `build_stage2_composition().application` (общий application layer с
     control audit) + production aiohttp transport + настоящий
     `TelegramOutboxWorker` с реальным ботом из `.botkey`; follower
     `tg-8264892202` (реальная пара user/chat 8264892202). Фазы: чтение
     через production HTTP → disable через HTTP (DB row=0) → реальный
     master BTCUSDT LONG + STOP_MARKET → production copy engine
     скопировал позицию (signed `positionRisk` follower ≠ 0) при
     **ноле новых outbox rows для пользователя** → enable через HTTP
     (DB row=1) → реальный partial close мастера → durable outbox row
     (id=130, PENDING) → `worker.process_once()` доставил через настоящий
     Telegram Bot API → **SENT** («Закрыта позиция BTCUSDT…», chat
     8264892202) → read endpoint вернул `enabled=true` и
     `last_delivery_at`. Cleanup: оба счёта flat, toggle восстановлен.
     FakeAster/MockTransport/fake sender не использовались.
  6. **Bounded impeccable + browser walkthrough:** detector по изменённым
     UI-файлам — 0 findings; один walkthrough мобильного viewport
     (390×844) на `preview_miniapp_engine.py` (копия prod-БД, порт 8093,
     production application + реальные DTO): секции/строки/chevrons,
     toggle-цикл вкл→выкл→вкл с обновлением UI по серверному DTO
     (подтверждено и в preview SQLite), Help/Security static screens,
     tab-switch сбрасывает nested stack. Corrective pass не потребовался.
  7. **Owner acceptance:** закрыта владельцем в реальном Telegram WebView на
     commit `fe84d2c`: Settings root и Notifications открываются штатно,
     переключатель фактически изменяет durable state через production HTTP →
     application → SQLite. В `control_audit` подтверждены три ручные mutation
     `disabled → enabled → disabled` в 21:08:51–21:08:53. Владелец явно принял
     P2.3; отдельный screenshot как дополнительный артефакт не требуется.
  8. **Corrective pass (2026-09-02, ревью P2.3 — три замечания):**
     1. *Storage-изоляция live-гейта:* `test_live_notifications_acceptance.py`
        работает на throwaway-копии реальной БД (`sqlite3.Connection.backup`
        в pytest `tmp_path` + `Settings.model_copy` для `db_path`/`log_path`):
        флаги, RiskPolicy, удаление execution_commands, telegram_users и
        outbox мутируются только в копии. Реальные Aster Testnet сделки и
        реальный Telegram Bot API сохранены. Re-run: **1 passed за 28.92s**
        (боевой экземпляр на время теста остановлен и поднят обратно через
        `scripts/dev-up.sh` — иначе прод-бот по WS дублировал бы копирование
        и перехватил phase-5 close); изоляция подтверждена: row 8264892202
        в прод-БД (`notifications_enabled`, `updated_at`) не изменился,
        контрольных `set_notifications` записей тест не оставил.
     2. *Синхронный double-tap lock:* `createToggleGate()` в
        `notificationsView.ts` закрывается до запуска `api.setNotifications`
        и открывается в `finally` после settle; render-проверки
        (`state.saving`, reducer `save_requested`) остаются бэкстопом.
        Regression-тесты (2): два одновременных тапа → ровно один admitted,
        лишний `end()` не ломает lock. Browser-проверка на новом бандле
        (`preview_miniapp_engine.py`, порт 8094): два `click()` по switch в
        одном синхронном тике → ровно **1** POST
        `/miniapp/api/notifications/enabled`, `aria-checked` перевернулся
        один раз (true→false), hint обновился по серверному DTO.
     3. *Статус требует snapshot:* `systemStatusValue(state,
        snapshotCapturedAt)` — «Всё работает» только при `ready` +
        непустом `snapshot_captured_at`; ready без snapshot
        (missing/null/`''`) → «Ожидание данных» (muted). Тесты: ready со
        snapshot → «Всё работает»/success; ready без snapshot →
        «Ожидание данных» (unit + render-контракт SettingsView);
        stale со snapshot → «Данные устарели»/warning (не «Всё работает»).
     4. *Corrective gate:* targeted frontend tests **26 passed**
        (notifications 12 + settings 14); полный frontend — **Vitest 102
        passed / 11 файлов** (было 97), oxlint 0/0, `tsc -b && vite build`
        clean; ruff check/format по изменённому Python-файлу clean;
        `git diff --check` clean. Полный offline suite не перезапускался —
        scope ревью ограничен изменёнными файлами; счётчики offline suite
        и live gate из пунктов 3/5 относятся к commit `fe84d2c`.

- **Stage 3, часть 2 (P2.2 — read-only Activity) — CLOSED (2026-09-02,
  live acceptance на Aster Testnet через production copy engine):**
  1. **Backend Application & Transport:** `ActivityCategory` (trade/system),
     `ActivityStatus` (completed/pending/failed), `ActivityFilter`
     (all/trade/system) и `ActivityItem` (category / title / status /
     symbol / occurred_at + optional pending_age / remediation) в
     `application/models.py`; старые `action`/`result`/`detail`
     удалены, `command_key`, `account_id`, addresses, nonce,
     `client_order_id`, raw Aster code, payload, traceback и внутренние
     exception-строки фильтруются server-side и никогда не попадают в
     DTO; `_activity_rows` и `AppStage2RuntimeAdapter.read_activity`
     выбирают `payload` дополнительно, чтобы backend извлекал `symbol`
     без раскрытия остального JSON; category predicate выполняется в
     SQL до `ORDER BY/LIMIT/OFFSET`, поэтому `trade`/`system` пагинация
     работает по отфильтрованному множеству и не теряет старые строки;
     неизвестные journal kinds остаются в fail-safe категории `system`;
     `POST /miniapp/api/activity` с
     валидацией Telegram initData (или testnet dev-header), body
     `{"page": N, "filter": "all|trade|system"}`, невалидный page/filter →
     400, прочие `ApplicationError` → соответствующие коды.
  2. **Frontend (`miniapp/`):** `types.ts` (`ActivityItemDTO`,
     `ActivityPageDTO`); `api.activity(auth, filter, page)`; pure
     view-model `activityView.ts` (`dayBucket` в UTC,
     `groupActivityByDay`, `activityStatusLabel`/`Tone`,
     `activityEmptyCopy`); `ActivityScreen` заменил interim
     `StubScreen` в `AppShell.tsx`; заголовок «Активность», подзаголовок
     «Безопасный журнал действий», фильтры Все/Сделки/Система, timeline
     с группами Сегодня/Вчера/Ранее, глифы completed/pending/failed,
     pending-age секунды, remediation под failed-строками, loading /
     error / empty / pending states, стабильная пагинация «Показать ещё»
     через `page+1` без потери уже отрисованных строк. Строго read-only:
     ни торговых кнопок, ни raw-кодов, ни технических payload.
     CSS: `.segmented`, `.activity-timeline`, `.activity-row`,
     `.activity-day-label`, `.activity-marker`/`.activity-connector`,
     `.activity-status` (tone-*), `.activity-remediation`,
     `.activity-safe-note`.
  3. **Offline gate (после P2.2 corrective pagination):** offline pytest
     **759 passed / 2 skipped / 31 deselected** за 11.96s, coverage
     **87%**; новые кейсы:
     `test_activity_safe_mapping_filters_and_sorts` (полный набор
     journal rows: open/close/stop/tp/cancel/leverage + pending + failed
     + unknown; safe DTO shape; trade/system/all filters;
     pending_age 40–60s окно; типизированная remediation copy;
     проверка отсутствия 17 запрещённых полей, в т.ч. command_key,
     account_id, address, client_order_id, nonce, private_key, -1003,
     boom), `test_activity_pagination_boundary` (25 строк, точная
     пагинация 10/10/5 через `page=0/1/2`, has_next/has_previous);
     `test_activity_category_filter_paginates_the_filtered_set` и
     `test_real_adapter_pushes_activity_category_filter_into_sql`
     фиксируют смешанный journal, непустые страницы обеих категорий,
     отсутствие пропусков/дубликатов и SQL pushdown на реальном SQLite;
     `test_activity_paging_validation_and_notification_toggle` дополнен
     `ValidationError` на невалидный filter. Ruff check/format и
     `git diff --check` clean.
  4. **Frontend gate (после P2.2):** Vitest **75 passed / 9 файлов** (11
     новых в `activity.test.tsx` — pure view-model + production
     `ActivityScreen` render: title, brand, testnet badge / wallet chip,
     loading state, отсутствие `<button` и торговых строк, отсутствие
     command_key/client_order_id/account_id/private_key/nonce/payload/
     traceback), oxlint 0/0, `tsc -b && vite build` clean.
  5. **Live gate (Aster Testnet):** `tests/testnet/
     test_live_activity_acceptance.py` — production copy engine: master
     открыл BTCUSDT LONG + Stop Market, `rebuild_full` +
     `reconcile_follower("copyc1")`, journal на `copyc1` через
     production `Executor`/`SQLite`, `POST /miniapp/api/activity` через
     production aiohttp path вернул safe DTO с
     `category: trade` / `title: "Открыта позиция BTCUSDT"` /
     `title: "Поставлен защитный Stop · BTCUSDT"` / `status: completed`
     / `symbol: BTCUSDT`; trade-filter показал те же записи с
     `category == "trade"` и `symbol == "BTCUSDT"`; production
     `Executor` выполнил реальный `SET_LEVERAGE` на testnet и записал
     его в journal, после чего system-filter вернул непустую отдельную
     выборку с `category == "system"` и заголовком `Плечо…`;
     partial close от мастера добавил `MARKET_CLOSE` row → Mini App
     endpoint вернул `"Закрыта позиция BTCUSDT"`; ни в одном item на
     проводе нет command_key / client_order_id / account_id / payload /
     address / nonce / private_key или raw Aster code; cleanup flat
     подтвердил сохранение safe DTO shape. `preview_miniapp_engine.py`
     на порту 8091 подтвердил отдачу safe DTO через тот же endpoint и
     HTML `/miniapp/` с собранным bundle. Corrective live gate
     **passed за 27.15s.**
     **Owner Telegram WebView acceptance закрыт 2026-09-02:** владелец
     проверил current HEAD на Builder-follower `0x3c45…F975` во время
     реального production copy-cycle. Экран Positions показал BTCUSDT
     LONG, актуальные Entry/Mark/PnL, подтверждённый Stop, риск до Stop
     и leverage; Activity показал подтверждённые `MARKET_OPEN` и
     `PLACE_STOP` в safe timeline без внутренних payload/secrets.
     После приёмки master был закрыт, production engine подтвердил
     `CANCEL_ORDER` + `MARKET_CLOSE` для `copyc1`/`copyc2`/`copyc3` и
     `tg-8264892202`; последующий reconciliation вернул 0 действий по
     всем четырём followers.

- **Stage 3, часть 2 (P2.1 — read-only Positions) — CORRECTIVE ROUND 2
  (2026-09-02, commit `111d2e5` под внешним ревью):**
  1. **Backend Application & Transport:** `PositionSummaryDTO`,
     `PositionPageDTO`, `PositionSide`, `PositionProtectionState`;
     `PositionState` расширен безопасным `unrealized_profit`; метод
     `service.positions(...)` и эндпоинт `POST /miniapp/api/positions` с
     Telegram initData защитой и Decimal-логикой `risk_to_stop_usdt` только
     по свежему snapshot и полностью подтверждённому Stop.
  2. **Freshness (corrective):** `AppStage2RuntimeAdapter` кэширует
     `(AccountState, captured_at)` с injectable clock; `positions()` бондирует
     свежесть TTL `max(2 × reconcile_interval_s, 30s)` — просроченный кэш
     помечается `is_stale=True` с `risk_to_stop_usdt=None`; при неудачном
     refresh выполняется fallback на кэш как stale с human-readable message.
  3. **Stop validation (corrective):** сторона Stop строго противоположна
     позиции (missing/unknown side → `UNCONFIRMED`); требуется
     `reduce_only`/`close_position`; quantity-based Stop обязан покрывать
     `orig_qty >= abs(qty)` — нулевой/невалидный объём → `UNCONFIRMED`,
     неполный положительный → `PARTIAL_STOP`.
  4. **Frontend UI (PositionsScreen):** настоящий `PositionsScreen` по
     эталону `03-positions.png` (секция «Открытые позиции · N», карточки с
     PnL, входом, текущей ценой, Stop/TP, риском до Stop, плечом «N× авто»,
     warning-блоками и stale-статусом); `accountMasked` пробрасывается в
     PositionsScreen и сохраняется сразу после onboarding (wallet chip);
     Home делает последовательный fetch (dashboard refresh → positions из
     кэша) без двойного signed snapshot.
  5. **Offline & Frontend Gates (после corrective):** offline pytest **749
     passed / 2 skipped / 30 deselected** за 36.7s, coverage **87%** (вкл.
     integration `test_resume_snapshot_tuple_contract.py` — P0-регресс с
     настоящим `AppStage2RuntimeAdapter`, TTL fresh/expired и
     refresh-failure fallback unit-тесты, строгие Stop validation кейсы);
     Frontend Vitest **64 passed / 8 файлов** (новый
     `positions.test.tsx` — `renderToStaticMarkup(PositionCard)`: рендер
     карточки, warning copy всех protection states, отсутствие торговых
     кнопок, скрытие риска при null); Oxlint clean; tsc + Vite build clean;
     Ruff check/format и `git diff --check` clean.
  6. **Signed Aster Testnet Live Gate:** `tests/testnet/
     test_live_positions_acceptance.py` — 8/8 фаз через реальный production
     copy engine (master `App`/`MasterProjector`/`Coordinator`/`Executor` →
     follower `copyc1` → Mini App aiohttp API → сверка с signed
     positionRisk/openOrders): master open + Stop, engine copy, positions DTO
     query, перестановка Stop + добавление TP, partial close, warning при
     снятии Stop, cleanup flat) — **повторно прогнан после corrective-коммита
     (2026-09-02): PASSED за 68.0s**. Production Pause/Resume через реальный
     Mini App HTTP (`test_builder_resume_live.py`: L7 pause→resume,
     L8 diagnostic snapshot) — **2 passed за 17.7s** после P0-фикса tuple
     unpacking в `_resume_account`.
- **Stage 3, часть 1 — CLOSED (2026-09-01):** все обязательные
  исправления R1–R9 из
  `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` выполнены.
  Offline, frontend, полный state-changing signed Aster Testnet suite и
  ручная Telegram Mini App приёмка владельца зелёные.
  1. **Unknown remote outcome:** transport/5xx/`-1000/-1006/-1007` после
     `approveAgent` сохраняют `APPROVAL_SENT`; recovery сначала проверяет Agent
     idempotent signed GET и не делает blind повтор POST.
  2. **Recovery без второй подписи:** session публикует только безопасный
     `onboarding_state`; `POST /miniapp/api/builder/resume-attempt` разрешён без
     подписи только для server-owned `APPROVAL_SENT`/`APPROVED`. `PREPARED`
     по-прежнему требует wallet signature.
  3. **Durable lifecycle:** после paused account commit записывается
     `COMMITTED`; успешная runtime activation очищает attempt, а activation
     failure восстанавливается лениво и идемпотентно.
  4. **Home/UI/logs:** первый Home load требует fresh snapshot; account
     `updated_at` больше не выдаётся за snapshot time; `Sheet` снова является
     modal overlay с Escape/focus management; raw exception payload не пишется
     в Builder/Mini App logs.
  5. **Live harness:** L3 действительно отправляет Testnet `approveAgent`, затем
     теряет локальный success response; L7 использует aiohttp Mini App API,
     намеренно stale seed и реальные signed account/Agent permission reads;
     order POST имеет unique client id и разрешает
     unknown outcome только через query. Fake-only evidence больше не выдаётся
     за signed live.
  6. **Актуальный offline/frontend gate:** offline pytest **740 passed / 2
     skipped / 30 deselected** за **17.02 s**, total coverage **87%**; Ruff
     check/format и `git diff --check` clean. Frontend: Vitest **53 passed**
     (Part 2 P2.0: navigation и corrective trust/accessibility cases;
     baseline Part 1 — 41 passed), Oxlint clean, TypeScript + Vite production
     build passed.
  7. **Актуальный полный signed Testnet gate:** в едином запуске
     все **29 обязательных state-changing/live сценариев passed** за
     **756.35 s** против `https://fapi.asterdex-testnet.com`. Пройдены
     Builder onboarding/recovery, Mini App Pause/Resume, builder+feeRate order,
     over-cap rejection, leverage control-plane, ONE_WAY/HEDGE, WebSocket,
     emergency, boot recovery, protective-order convergence и flat cleanup.
     Один legacy-дубликат, требовавший хранить main private keys в
     production credentials, был skipped в этом запуске и затем удалён:
     его полностью заменяет прошедший `test_builder_mini_app_e2e.py`.
  8. **Incident-resilience live evidence (настоящий Aster Testnet):** во время
     частичного инцидента signed
     `balance`, `positionRisk` и symbol-scoped `allOrders` отвечают `200`, тогда
     как `positionSide/dual`, `openOrders`, `listenKey`, `leverageBracket` и
     `agent` во время инцидента отвечают `-1000`. Добавлены только
     доказуемые fallback-пути: `openOrders(symbol) -> allOrders` с фильтром
     `NEW/PARTIALLY_FILLED`; REST master snapshot на каждом sweep при
     недоступном stream; unanimous current-boot follower leverage из
     `positionRisk`; 24h bounded permission cache только для явного Testnet
     override и только после unknown Agent-read failure. Частичные/расходящиеся
     данные, mainnet и deterministic permission rejection остаются fail-closed.
     После восстановления routes полный live-suite прошёл без этих
     fallback-путей как подмены assertions.
  9. **Финальный production runtime после live-suite:** `READY`, master
     user-stream запущен, Telegram polling и новая публичная Mini App
     отвечает `HTTP 200`. Startup/post-stream/sweep дали по `0` действий
     на `copyc1`–`copyc3` и
     `tg-8264892202`. Production Mini App flow `session -> confirmation ->
     resume` вернул `HTTP 200`, `copy_state=active`, `system_state=ready` для
     `tg-8264892202`; leverage readiness подтверждена реальными
     `positionRisk`: BTC/ETH=200, SOL=100. Оставленная emergency-тестом
     глобальная пауза снята; `allow_new_entries=true`, все пять аккаунтов
     enabled. Это отдельное transport/application evidence.
  10. **Owner manual Telegram acceptance:** production-лог подтверждает
      ручной WebView flow владельца: `13:08:42 pause attempt` →
      `13:08:44 pause success/state=paused` → `13:08:49 resume attempt` →
      real Testnet leverage confirmation BTC/ETH=200, SOL=100 →
      `13:08:58 resume success/state=resumed`. Последующие sweep дают
      `tg-8264892202: 0`. Ручной UI-gate закрыт.
- **Stage 1 CLOSED (corrective acceptance 2026-08-23):** новых P0/P1 по
  заявленному testnet scope не осталось; код можно фиксировать и пушить для
  перехода к Stage 2. Mainnet по-прежнему запрещён guard-ом Stage 1.
- **Stage 2 CLOSED (2026-08-24):** Telegram-бот, direct V3 onboarding,
  управление риском, notification outbox, общий Bot/Mini App application layer,
  а также полный цикл ручной приёмки владельцем (WP9b), включая живой
  `resume/copy cycle` (открытие BTCUSDT LONG, авто-плечо 200x, стоп, доставка
  Telegram-уведомлений `MARKET_OPEN`, `PLACE_STOP`, `CANCEL_ORDER`, `MARKET_CLOSE`,
  полный flat всех подписчиков), успешно выполнены и проверены на Aster Testnet.
- **Ручной WP9b ПРИНЯТ:** владелец подтвердил captcha, выдачу доступа, admin
  approval, direct onboarding/подключение, notifications toggle, изменение
  risk percent, итоговое inline-меню, а также полный живой resume/copy trading
  cycle на Telegram-follower.
- Все аккаунты (`main`, `tg-8264892202`, `copyc1`–`copyc3`) после цикла находятся
  в чистом `flat` (0 позиций, 0 открытых ордеров).
- Движок работает на futures testnet (`fapi.asterdex-testnet.com`), HEDGE-режим;
  ONE_WAY официально поддержан и покрыт signed E2E через ядро (см. ниже).
- Аккаунты (wallet.txt): `main test` (0x235E…5176, мастер) + copyc1–copyc3.
  copyc4 удалён: его API-кошельки на стороне биржи вели на счёт мейна
  (пересечение 49/50 ордеров подтверждено allOrders).
- **Leverage control-plane ПРИНЯТ (2026-08-25, внешний acceptance `69fa148`):**
  баги #25/#26 и весь leverage-этап закрыты цепочкой
  `86a51ec → 6f01917 → 54279d8 → 69fa148`: P0 dependency gating, flat-leverage,
  hot path без leverage REST, unknown-outcome без blind retry, crash recovery
  SET через authoritative positionRisk, ambiguous → fail-closed, boot-порядок
  catalog→reconcile, lifecycle worker'а. Дословные ревью:
  `docs/reviews/review_leverage_control_plane.md`; счётчики гейтов выше.
- Актуальный offline gate после внедрения leverage control-plane и финального acceptance (баги #25–26,
  `trading/leverage.py`, `69fa148`): **666 passed / 2 skipped / 21 deselected**, coverage
  **87.29%** при пороге 87%; ruff check/format clean. Signed testnet gate
  после изменений плеча (2026-08-25, `CT_TESTNET=1`): **16 passed / 1 skipped**
  (skip — Builder Stage 3). E2E ONE_WAY прошёл полный цикл через новый
  leverage-путь: fixture подтвердил candidate BTCUSDT=200 подписанным bracket
  GET + `POST /leverage` на copyc1, вход исполнился без холодного SET; первый
  прогон упал на транзитном `-1007 transport timeout` в polling-хелпере фазы 5
  (флак тестнета, ретрай не предусмотрен харнессом) — повторный прогон чистый
  за 178s.
- Ревью-гейт leverage control-plane (2026-08-25, независимый review `6f01917`):
  закрыты четыре находки. (1) `_prepare_locked`: unknown-outcome POST
  (`-1000/-1006/-1007`, 503/504 и любые не-биржевые ошибки) больше не
  триггерит слепой fallback — сначала авторитетный positionRisk: совпало с
  target → confirm без записей; доказуемо не применено → безопасный
  account-maximum fallback; чтение не удалось → fail closed. (2) Boot
  recovery для journaled SET_LEVERAGE без coid: исход разрешается через
  positionRisk — совпадение → CONFIRMED, расхождение → FAILED (reconcile
  переподготовит), нечитаемо → остаётся in-flight fail-closed. (3)
  Account-specific readiness после fallback: проверено воспроизведением —
  observe() УЖЕ сохраняет подтверждение той же revision (early-continue);
  несогласие с ревью зафиксировано, контракт заморожен регрессионными
  тестами (global=200/follower=100 → prepare → observe(100) → still ready;
  бамп ревизии инвалидирует). (4) Lifecycle: убран дубль task_names,
  выбрасывавший `_leverage_task`; shutdown-test с живым worker'ом
  доказывает остановку до закрытия HTTP/DB; initial refresh_catalog()
  переставлен ДО startup reconciliation (ARCHITECTURE §12 обновлён), чтобы
  рестарт-реконструкция из positionRisk валидировалась против живой
  генерации без лишних POST. Offline gate: **665 passed / 2 skipped**,
  coverage 87%; signed gate: **20 passed / 1 skipped** за 579s.
- Live-дополнение (2026-08-25, `tests/testnet/test_leverage_control_plane_live.py`,
  `CT_TESTNET=1`, 4 passed): закрыты лайв-дыры, недоказуемые офлайн-фейками —
  (1) завышенный candidate отвергается биржей (-4028) и fallback подтверждает
  подписанный максимум аккаунта; (2) неготовый control-plane режет вход
  LEVERAGE_NOT_READY без единого ордера, живой worker-цикл доводит готовность;
  (3) реально отклонённый SET блокирует зависимый MARKET_OPEN в executor'е,
  риск-снижающий MARKET_CLOSE при том же провале исполняется (P0-контракт на
  реальном -4028); (4) реальный дауншифт 200→20 держит пару durably-unready:
  закрытие мастера течёт, повторный вход скипается с enqueue подготовки и
  после prepare_account исполняется. Изоляция: торговые репо во временной БД
  на тест, общая локальная БД — только ключи/nonce. Полный signed gate после
  добавления: **20 passed / 1 skipped** за 571s. Вывод пробы: через полный
  coordinator-пайплайн отказной SET недостижим по конструкции — авторитетный
  снапшот лечит RAM-дивергенс до планирования; поэтому P0 доказан на границе
  executor'а с настоящим отказом биржи.
- Независимый signed REST-аудит после полного набора и двухчасового soak:
  `main` и `copyc1`–`copyc3` в HEDGE, позиции 0, открытых ордеров 0.
- Runtime leverage follower — RAM control-plane (`LeverageManager`,
  `trading/leverage.py`): глобальный кандидатный каталог по symbol
  stale-while-revalidate + account-specific подтверждённое плечо,
  подтверждаемое `POST /fapi/v3/leverage` (reject → account bracket GET →
  POST максимума аккаунта). Подготовка — на onboarding/resume/boot-recovery и
  в bounded background worker; hot path сделки читает только RAM. Рестарт
  восстанавливает состояние из authoritative positionRisk
  (`AccountState.symbol_leverages`, flat symbols включительно). Смена
  максимума биржей инвалидирует готовность нового входа до переподготовки;
  закрытие/сокращение/защита не зависят от плеча. Stage 2 по-прежнему не
  меняет `marginType`; isolated enforcement отложен.

## Найденные и исправленные баги (живой тестнет)

| # | Баг | Корневая причина | Фикс |
|---|-----|------------------|------|
| 1 | Осцилляция таргетов свипами | трекер master-магнитуд обновлялся только на WS-дельтах | remember на каждом recompute (`5e32837`) |
| 2 | Deadlock «database is locked» | implicit transactions/cursor lifetime и конкурирующий nonce path при fan-out | autocommit, закрытие cursors, общий `Database.write_lock`, atomic nonce `INSERT…RETURNING`; проверено 30×100 live |
| 3 | Координатор молча пропускал аккаунты | import не создавал copy_settings | дефолтная политика при импорте (`7f4a8b0`) |
| 4 | Дорастивание позиций после partial-close | ветка b считала cap от близкого стопа по устаревшему состоянию | STALE_MASTER guard (<5 c) (`0ae383a`) |
| 5 | Копиры никогда не получают TP | `_targets_for` хардкодил `tp_price=None` | уровень TP мастера в таргете (`b2a892d`) |
| 6 | **Фантомное переоткрытие позиции** | `_apply_fill` брал направление из ноги (LONG→+1): закрывающий SELL = «покупка»; ACCOUNT_UPDATE дублировал дельту | направление только по `S`; AU мутатором не является (`63ac80a`) |
| 7 | Ранний return пропускал присвоение qty | слоты защит чистились до `current.qty = new_qty` | очистка inline, без return (`e9ac649`) |
| 8 | Ре-вход после своего сработавшего ТП | guard покрывал только стопы | OWN_PROTECTIVE_FILLED: стоп ИЛИ тейк активного эпизода (`ff7f490`) |
| 9 | STOP/TAKE_PROFIT (лимитные флейворы) игнорировались | проектор знал только *_MARKET | слоты принимают оба флейвора, тип ордера копируется (`f85c9a8`) |
| 10 | Лимитные защиты отклоняются (-1102) | не отправлялся обязательный `price` | planner выводит price = trigger ±0.05% (`bce8c5b`) |
| 11 | -4014 Price not increased by tick size | `quantize("0.10")` держит 2 знака вместо сетки 0.1 | выравнивание делением на тик (`d834e9c`) |
| 12 | **Stale защита после FAILED dependent cancel** | `_pick()` снапшота выбирал только новейший Stop/TP: при `PLACE B CONFIRMED → CANCEL A FAILED` следующий reconcile видел current==target и выдавал пустой план — старый уровень висел рядом с новым | снапшот отдаёт дубликаты слота (`extra_stops`/`extra_tps`), planner-правило 9 всегда их отменяет, включая flat-ветку |
| 13 | **Ретрай CANCEL/SET_LEVERAGE после FAILED невозможен** | `_submit_cancel`/`_submit_leverage` всегда делали INSERT и падали на `UNIQUE(command_key)` при повторе той же транзиции | существующая FAILED-строка переводится в PENDING и ретраится под исходным командным ключом (вскрыто e2e-тестом пункта 12) |
| 14 | **Dust-сокращение до нуля переоткрывало позицию** | при `scaled_reduce → 0` внутри живого эпизода мастера `_persist_refs` стирал строку basis; следующий recompute видел «мастер в позиции + follower flat» и трактовал это как дрейф — немедленный re-entry сразу после намеренного закрытия | флаг `sync_basis.zero_target` (миграция #12): намеренный флэт персистится до конца эпизода мастера; входная ветка skip'ает `INTENTIONAL_FLAT`; флаг снимают новый эпизод мастера (`open_trade`) и `mark_zero_target` (сброс `issued_target`, чтобы следующий ненулевой таргет получил новую ревизию) |
| 15 | **Фикс №14 ломал ONE_WAY flip** | `_persist_refs` клеймил ЛЮБОЙ `qty=0` при живом мастере как intentional-flat: транзитный ноль ветки flip («мастер перевернулся — закрой эту сторону») получал маркер и навсегда блокировал открытие обратной стороны | явная причина нуля: `TargetPosition.hold_flat_until_episode_end=True` только для same-direction редукции в ноль; flip-ноль — транзитный, basis очищается; флаг входит в `_canonical_target` (разные command keys) (`79be6b1`) |
| 16 | **Flip не был границей эпизода** | `_apply_fill` открывал новый episode только при `active is None`: после флипа LONG→SHORT жил старый эпизод, а снапшот-синк вообще не смотрел на знак позиции | flip = закрыть старый episode + открыть новый с `direction=±1` (миграция #13 `copy_trades.direction`); snapshot-синк детектит рассинхрон направления после офлайна; новый эпизод автоматически снимает sticky-маркеры (`8ea3dbd`) |
| 17 | **Fixed Stop/TP не покрывал grow остаток** | follower snapshot терял `origQty`, а planner сравнивал только trigger level | `ProtectiveOrder.orig_qty`; market protection через `closePosition=true`; fixed STOP/TP quantity-aware replacement; authoritative post-fill safety и fail-safe flatten |
| 18 | **Повторный emergency проглатывался историческим CONFIRMED** | одинаковые payload/revision давали прежний command key, а pause не был частью аварийного flow | persisted UUID `operation_id`, pause до snapshots, bounded passes и два flat verification snapshots |
| 19 | **Boot мог дать READY при неизвестном финансовом исходе** | query-error оставлял in-flight, но startup не проверял остаток | структурированный boot report, bounded retry, unresolved блокирует READY и новые mutations |
| 20 | **READY переживал kill как ложный живой статус** | SQLite-флаг сбрасывался только graceful stop | runtime UUID + heartbeat; CLI считает старый heartbeat stale; kill/recovery доказан реальным SIGKILL |
| 21 | **Fake/Shadow скрывали exchange semantics защиты** | doubles допускали flip/oversize и не моделировали closePosition/fixed remainder | строгие ONE_WAY/HEDGE reduce rules, conditional expiration, closePosition и partial-fill hooks |
| 22 | **Direct onboarding скрывал причину preflight failure** | application service схлопывал `OnboardingError` в generic `ValidationError`, Telegram терял replacement context и показывал только restart copy | typed `OnboardingApplicationError.code`, enum-based Telegram remediation/error screen, revision-safe connect/replace retry и сохранённые secret/FSM invariants |
| 23 | **Onboarding-карточки наслаивались после повторного ввода** | старый prompt сохранялся рядом с новым, expiry отправлял отдельный ответ, а удалённый anchor оставался в FSM до replacement send | все три input-сообщения удаляются до проверки; valid step transitions используют fresh cards с edit-fallback без дублей; expiry заменяет текущую карточку recovery-экраном; retired anchor инвалидируется и удаляется из FSM до send |
| 24 | **Сырые системные логи в Telegram-уведомлениях** | движок слал внутренние имена RPC-команд (`MARKET_OPEN`, `PLACE_STOP`, `CANCEL_ORDER`, `NO_STOP`) напрямую в пользовательский чат | выделен модуль `notifications/formatter.py` (`format_confirmed_action`, `format_skip_reason`); уведомления форматируются в чистые трейдерские карточки (🟢 Открыта позиция, 🛡 Защитный стоп, 🎯 Тейк-профит, 🔴 Закрыта позиция, 🗑 Снят ордер, ⏳ Ожидание стопа), технический шум убран |
| 25 | **FAILED `SET_LEVERAGE` не блокировал `MARKET_OPEN`** | priority в плане — только порядок, а не зависимость: исполнитель продолжал план после FAILED prerequisite и отправлял вход с неподтверждённым плечом (fail-closed для плеча фактически отсутствовал) | generic dependency-модель плана: `ExecutionAction.action_id` / `depends_on_action_id`; planner связывает SET_LEVERAGE → вход/защиту, executor блокирует (`BLOCKED`) зависимые действия при FAILED/UNKNOWN prerequisite; закрытия/отмены зависимости не получают |
| 26 | **Движок терял известное плечо flat-symbol** | `_snapshot()` выбрасывал строки positionRisk с `positionAmt==0`, а planner трактовал «нет позиции» как «плечо неизвестно» — каждая новая сделка платила холодный `SET_LEVERAGE` (+TTL bracket GET) даже при годами стоящих на бирже 200x | `AccountState.symbol_leverages` из всех строк positionRisk (включая flat; неоднозначные per-side значения → unknown/fail-closed); planner сравнивает known vs target и эмитит SET только при расхождении; hot path сделки — ноль leverage REST-запросов |

## Известные ограничения (осознанные решения этапа 1)

1. **ACCOUNT_UPDATE информационный**: позицию мутирует только TRADE-дельта;
   пропущенный ивент = дрейф до reconnect (логируется, лечится rebuild_full).
2. **Вход копиров всегда MARKET**: лимитка мастера копируется в момент
   исполнения (fill-copy). Зеркалирование висящих лимиток — этап 2.
3. `availableBalance` тестнета колеблется сильнее wallet balance — сайзинг
   считается от него (policy risk_base=available_balance).
4. **Один runtime process.** Heartbeat обнаруживает kill/stale и смену runtime,
   но не является распределённым mutex для двух одновременно запущенных App.
   Для одного трейдера это операционное правило запуска, а не повод добавлять
   Redis/оркестратор в Stage 1.
5. Перед запуском оператор подтверждает одинаковый position mode аккаунтов.
   Signed preflight и финальный audit это делают; автоматическая смена mode на
   аккаунте с открытой позицией может быть отклонена самой биржей.

## Кампания полной проверки (12 пунктов) — 2026-08-22

| Пункт | Статус | Результат |
|-------|--------|-----------|
| 1. Покупка long | ✅ | копиры BUY + стопы, ~7 c, WS-путь |
| 2. Продажа short | ✅ | SELL + BUY-стопы, ~7 c |
| 3. Тейк-триггер | ✅ | TP @77942/77847 сработал мастеру и всем копирам в одну секунду |
| 4. Стоп-триггер | ✅ | стоп @78073 → TRADE l=0.002, все копиры вышли своими стопами |
| 5. Перенос стопа | ✅ | place-new→cancel-old, мелкие движения 10-15$ |
| 6. Перенос тейка | ✅ | move-tp 78200→77942, PLACE_TP+CANCEL ×3, ~5 c |
| 7. Несколько позиций | ✅ | BTC long + SOL long одновременно, независимые эпизоды |
| 8. Казусы | ✅ | kill -9 с позой → recovery без дублей; быстрый цикл эпизодов; ручная отмена защиты мастером → корректный NO_STOP |
| 9. Вход без стопа→set-stop→set-tp | ✅ | SKIP(NO_STOP) → вход после set-stop → PLACE_TP после set-tp |
| 10. Вход лимиткой | ✅ | висел 2.5 ч без действий копиров; филл → реакция за секунды |
| 11. Лимитные защитные (STOP) | ✅ | copyc1–3 держат type=STOP sp=78201 px=78161.90 (биржа) |
| 12. Сверка ордеров с биржей по clientOrderId | ✅ | каждый прогон |

## Инфраструктура прогонов

- Скрипт `scripts/manual_master_order.py`: open/open-nostop (long/short),
  set-stop/move-stop, set-tp/move-tp, partial-close, close.
- Движок — hub-процесс `engine`; журнал команд — SQLite `execution_commands`,
  события мастера — `master_events`; сверка — query_order/allOrders по coid.

## Рефакторинг по review_stage1 (2026-08-22)
Проведён рефакторинг по итогам внешнего код-ревью: persisted sizing basis
(restart после пропущенного partial close), идемпотентность по ревизии перехода,
boot-recovery SENDING/SUBMITTED/UNKNOWN с order_links, async persist-before-send
nonce, реальный pause, deviation/post-fill риск, перенос стопа в прибыли,
tick-grid равенство, TP removal/orphan cleanup, HEDGE-rebuild защита, TRADE вне
порядка + entry от L, fail-closed routing, decoupled WS ingest.
Полная инвентаризация и верификация — [REFACTOR_STAGE1_REPORT.md](reports/REFACTOR_STAGE1_REPORT.md).
На момент закрытия review-specific доработок герметичный сьют дал **240 passed /
10 skipped** без
`CT_TESTNET`, `ruff check` и `ruff format --check` — clean. Signed testnet:
**9 passed / 1 skipped**; отдельно после финальной правки dependency gate
повторены one-tick stop replace и ONE_WAY cycle — **2 passed**. Read-only
сверка после cleanup: `main` снова HEDGE, `main` и copyc1–copyc3 flat,
открытых ордеров нет. Единственный skip — Builder e2e, который не блокирует
торговое ядро Stage 1 и перенесён в контур Stage 3.

## Hybrid load campaign — 2026-08-23

Проведён отдельный testnet-only нагрузочный гейт перед ревью:

- `scripts/hybrid_load_test.py`, 30 follower slots, 100 циклов;
- 3 REAL подписанных follower-а (`copyc1`–`copyc3`) + 27 явно помеченных
  SHADOW rows; fake credentials не создавались;
- **12 302/12 302 `CONFIRMED`**, `FAILED/UNKNOWN/non-terminal=0`,
  `ingest_dropped=0`, cleanup errors=0;
- run-БД до/после cleanup: 31 account flat/no-orders;
- независимый signed REST после прогона: `main` и copyc1–copyc3 flat,
  `open_orders=0`;
- `CT_LOAD=1` production-path integration: **1 passed** (30×100 shadow
  cycles), full suite после изменений: **247 passed / 11 skipped** (счётчик
  той кампании; актуальный — в «Текущем состоянии» и разделе 4).

Финальный JSON: `data/hybrid-load-20260822T233258Z.json`. Полная матрица
evidence и ограничения shadow-контура: [HYBRID_LOAD_TEST_REPORT_2026-08.md](reports/HYBRID_LOAD_TEST_REPORT_2026-08.md).

## Финальный gate по внешнему код-ревью — 2026-08-23

Текст ревью: [review_stage1_final.md](reviews/review_stage1_final.md). Два пункта:

### 1. P1 — защитная конвергенция (последний code blocker)

Сценарий ревьюера воспроизведён и закрыт на трёх уровнях:

- юнит (`test_planner_transitions.py::TestDuplicateProtectionConvergence`):
  extras отменяются при совпадающем target, рядом с обычной заменой (без
  двойного cancel), для TP и в flat-ветке;
- e2e на FakeAster (`test_end_to_end_fake.py::TestDuplicateProtectionConvergence`):
  инжект FAILED dependent cancel после CONFIRMED replacement → оба уровня
  висят → следующий reconcile удаляет stale, журнал содержит CONFIRMED CANCEL
  по старому coid, повторный проход выдаёт 0 команд; то же для TP и flat;
- **побочный наход e2e:** ретрай CANCEL/SET_LEVERAGE после FAILED падал на
  `UNIQUE(command_key)` — зависимый cancel не мог повториться вообще. Исправлено
  (FAILED → PENDING под исходным ключом), покрыто тем же e2e.

### 2. ONE_WAY signed E2E через ядро

Новый `tests/testnet/test_one_way_engine_e2e.py`: master open → stop →
stop move (замена защиты) → partial close → TP → master close, всё через
Projector→Coordinator→Risk→Planner→Executor→journal на REAL follower `copyc1`.
Живой прогон 2026-08-23: **PASSED за 36 с**. Аудит журнала: 0 FAILED / UNKNOWN /
BLOCKED; все закрывающие команды с `reduceOnly=true` и без `positionSide`;
финал — оба аккаунта flat, открытых ордеров нет. Первый запуск вскрыл ошибки
самого сценария (снятие baseline после события, неверное ожидание при
минимальном размере копира) — предикаты приведены к семантике движка.

### 3. Dust zero-target edge case (второй проход ревью, 2026-08-23)

Ревьюер принял оба пункта gate и нашёл последний edge case: при
`scaled_reduce → 0` (мастер режет позицию так, что пропорция follower'а
округляется ниже step) `_persist_refs` стирал basis-строку; следующий
post-fill recompute видел «мастер открыт + follower flat» и **немедленно
переоткрывал** намеренно закрытую ногу.

Фикс: персистентный маркер `sync_basis.zero_target` (новая миграция #12):
- `_persist_refs`: `t.qty == 0` при живом эпизоде мастера → `mark_zero_target`
  вместо `clear`; реальное закрытие эпизода → `clear` как раньше;
- входная ветка координатора: skip `INTENTIONAL_FLAT`, пока маркер активен
  (после OWN_PROTECTIVE_FILLED, до PAUSED);
- маркер снимают: новый эпизод мастера (`CopyTradeRepo.open_trade`) и сам
  `mark_zero_target` (сбрасывает и `issued_target` — иначе следующий ненулевой
  таргет совпал бы с нулевым в ревизии и новый MARKET_OPEN проглотила бы
  идемпотентность по command_key).

Контрольный тест защищает починенный drift-recovery: ненулевой таргет +
ручное закрытие follower'а → движок по-прежнему восстанавливает позицию той же
величины. Live dust-фаза добавлена в signed ONE_WAY E2E (phase 4b).

### 4. Третий проход ревью: flip-регрессия и фальшивое dust-evidence (2026-08-23)

Ревьюер вскрыл два дефекта предыдущего фикса, оба подтверждены кодом и живыми
прогонами:

1. **Flip-регрессия**: фикс №14 трактовал ЛЮБОЙ `qty=0` при живом мастере как
   intentional-flat. Транзитный ноль ветки flip получал маркер → SHORT после
   LONG→SHORT не открывался никогда (до следующего «настоящего» эпизода).
2. **Dust-фаза signed E2E не выполнялась**: guard
   `dust_left = snap(master_qty*0.005) >= step` при `0.003*0.005=0.000015`
   давал `0 < step` → вся фаза молча пропускалась, «passed за 38 c» ничего не
   доказывал. Плюс в синтетическое событие передавался ОСТАТОК позиции вместо
   объёма исполненного SELL.

Исправление (глобальное, по требованию ревьюера — не точечные заплатки):

- **Явная причина нулевого таргета** — `TargetPosition.hold_flat_until_episode_end`:
  ставится только для same-direction редукции в ноль; flip-ноль транзитный.
  `_persist_refs` клеит маркер только при флаге; флаг входит в
  `_canonical_target` → разные command keys для sticky-dust и flip-zero (`79be6b1`).
- **Episode-семантика флипа**: `_apply_fill` при flip закрывает старый episode и
  открывает новый с `direction=±1`; миграция #13 `copy_trades.direction`;
  snapshot-синк детектит смену знака после офлайна; новый эпизод снимает
  sticky-маркеры автоматически — система самосогласована (`8ea3dbd`).
- **Тесты**: офлайн `TestMasterFlipEpisodeSemantics` (оба сценария ревьюера,
  включая «dust-flat → flip»); в signed E2E dust-фаза стала ОБЯЗАТЕЛЬНОЙ
  (assert вместо тихого skip: первый же прогон честно упал на новом assert'е —
  математическое доказательство того, что раньше она не выполнялась), остаток
  мастера = ровно один step (цель follower'а гарантированно флорится в ноль),
  состояние проектора — из авторитетного `rebuild_full`, а харнесс явно гонит
  recompute_follower (у голого харнесса нет своего reconcile-цикла);
  новая фаза 4c — реальный signed flip LONG→SHORT через полное ядро с
  направленно-осознанной защитой/TP/закрытием (`7122a28`).

### Итог прогона

- Полный офлайн-suite: **258 passed / 1 skipped**, coverage 87%; ruff check +
  format clean.
- Signed testnet (CT_TESTNET=1): **10 passed / 1 skipped** (Builder — нет main
  private key, Stage 3), включая ONE_WAY E2E с обязательной dust-фазой и
  live-flip: PASSED за 155 с (run 7); полный сьют — 203 с.
- Hybrid load gate (после всех фиксов): **100/100 циклов**, 30 слотов
  (3 REAL + 27 SHADOW), **12207/12207 CONFIRMED**, 0 FAILED / UNKNOWN /
  SUBMITTED / SENDING / BLOCKED, ingest_dropped=0, cleanup errors=0,
  shadow_signed_requests=0, 31/31 аккаунтов flat/no-orders до и после cleanup;
  1 авторитетная рекавери мастера. Артефакты:
  `data/hybrid-load-20260823T075504Z.{json,sqlite3}`.
- Оба пункта первого gate + dust edge case второго прохода + оба дефекта
  третьего прохода закрыты с офлайн-регрессами и живыми signed-доказательствами.
  Решение о статусе этапа — за владельцем проекта/ревьюером.

## Corrective acceptance — 2026-08-23

Повторное открытие Stage 1 по CR-1..CR-5 завершено. Детальная хронология,
включая все непринятые попытки, находится в
[STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md](reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md).

Финальные gates:

- offline: **284 passed / 2 skipped / 16 deselected**, coverage **88.41%**;
- static/package: ruff check, ruff format check, git diff check — clean;
  `uv build` создал sdist и wheel без прежнего README-warning;
- signed testnet: **15 passed / 1 skipped за 510.48 s**; REAL-01..08 включают
  ONE_WAY/HEDGE protection sizing, closePosition contract, emergency дважды,
  boot found/not-found и настоящий App/WS reconnect/critical recovery;
- hermetic scale: 30 SHADOW × 100 и 100 SHADOW × 20 расширенных циклов;
- hybrid: 3 REAL + 27 SHADOW, 20/20 циклов, **3642 CONFIRMED**, остальные
  statuses 0, p50 35.118 s / p95 39.253 s / max 39.918 s, 31/31 account
  flat/no-orders;
- SOAK-01: **7203.589 s**, 6/6 signed cycles, forced reconnect, graceful
  restart, реальный child SIGKILL (`-9`) + stale heartbeat 33.709 s + recovery,
  24 periodic flat audits, **218 CONFIRMED**, in-flight/ingest drops/cleanup
  errors 0;
- независимый финальный signed REST: `main`, `copyc1`, `copyc2`, `copyc3`
  HEDGE и flat/no-orders; run и рабочая SQLite: in-flight 0, sticky zero 0,
  stopped runtime `ready=false`.

Финальные локальные артефакты:

- hybrid JSON SHA-256
  `6c0f8fe461d159377215d454500266256d5a410197b1fc27f8a6b42234cebd9e`;
- hybrid SQLite SHA-256
  `fc011bb8f30a380e9ca895e852cdc19d448c4269db1ac6862c6cc939db2ac62a`;
- soak JSON SHA-256
  `a6fbb57a1f06abb51843d5af30a35a8565c82b2e66da86be9cd08a4f5c59793d`;
- soak SQLite SHA-256
  `42dfdd4b23bc5234cf503ba38d0c6f67b623f2ad9f6faca17d5aeac6eb5ad1aa`.

Verdict: **Stage 1 CLOSED, push approved for the testnet trading core.**
Builder остаётся Stage 3; mainnet не разрешён этим verdict.

## Stage 2 automated acceptance — 2026-08-24

Реализован минимальный Stage 2 для одного master и 10–30, с проверенным
контуром до 100 Telegram users/followers:

- aiogram 3.30.0 long polling в том же управляемом процессе, что и `App`;
- современный inline-first dashboard: один редактируемый anchor message,
  typed callbacks и slash-команды только для входа/recovery;
- общий transport-neutral application service для Telegram и будущей Mini App;
- direct V3 onboarding/replacement/removal: проверка address/key pair,
  подписанный `GET /fapi/v3/agent`, permissions/expiry/IP preflight,
  зашифрованное atomic storage и activation только в paused state;
- owner-scoped risk/status/pause/resume/emergency с короткоживущими
  confirmations, account-version guard и сериализованными barriers;
- durable recipient-specific SQLite outbox с lease/retry/backoff и изоляцией
  Telegram 403;
- hot-add/recovery runtime через публичный registry, без переписывания Stage 1
  projector/coordinator/planner/executor/reconciler.

`Scenes Wizard` отдельно проверен по stable aiogram documentation. В Stage 2
он намеренно не используется: Scenes остаются experimental и предназначены
для ветвящихся wizard flows, а не для постоянного меню. Dashboard stateless;
короткие линейные формы используют обычный `StatesGroup`. В source tree нет
production-import `Scene`, `SceneRegistry` или `SceneWizard`; это закреплено
регрессионным тестом.

Автоматические доказательства:

- offline: **541 passed / 2 skipped / 17 deselected за 15.37 s**;
- coverage: **87.88%** (`8399` statements, `1018` missing), обязательный порог
  **87%** пройден;
- отдельный Stage 2 scale gate: **3 passed** — 30 одновременных users и
  100-user burst/restart/backpressure;
- signed testnet: **16 passed / 1 skipped за 395.51 s**; skip — Builder Stage 3;
- реальный Bot API: `getMe`, private command scope и menu button настроены;
- реальный composition smoke: `App` прошёл recovery/reconciliation/WS/READY,
  затем aiogram polling запустился и завершился по `SIGINT` с exit code 0;
- package gate: `uv build` успешно создал sdist и wheel;
- static gate: `ruff check`, `ruff format --check`, `git diff --check` — clean;
- независимый signed REST после suite: `main`, `copyc1`, `copyc2`, `copyc3`
  в HEDGE, nonzero positions 0, open orders 0.

Fail-closed Agent check с production-default выявил внешний prerequisite:
`ipWhitelist` у всех трёх текущих follower Agent пуст. Поэтому автоматический
signed-run использовал только явный `CT_TESTNET_IP_WHITELIST_OVERRIDE=true`;
этот override конфигурационно запрещён вне testnet. Для ручной приёмки владелец
должен либо настроить статический egress IP в Aster и `CT_ASTER_EGRESS_IPS`,
либо временно оставить testnet-only override.

Verdict: **автоматическая часть Stage 2 принята; code/static/signed blockers
не найдены.** Следующая полезная проверка требует действий владельца в private
Telegram chat и отдельного flat testnet follower account. До её результата
WP9b и общий Stage 2 остаются открытыми.

### Owner correction: admin UX — 2026-08-24

После замечания владельца устранено избыточное разделение `ADMIN`/`USER`:

- immutable Telegram ID владельца настроен локально как единственный admin и
  записан в рабочую SQLite (`role=ADMIN`, `access=on`); сам ID остаётся только
  в git-ignored `.env`/рабочей БД;
- admin — capability из env, а не отдельный человек: тот же ID при
  необходимости может владеть одним follower account;
- inline-админка показывает master summary, пользователей и счета подписчиков;
- доступ новому пользователю выдаётся и отзывается кнопками; неизвестный private
  user проходит inline captcha, после чего durable заявка сама уходит configured
  admin; ручной numeric Telegram ID оставлен только recovery;
- revoke закрывает только вход в UI, не бросает торговый account и не смешан с
  отдельной настройкой доставки уведомлений;
- users/followers листаются страницами по 10; 30-account pagination покрыта;
- targeted reconcile привязан к stable opaque SHA-256 account key, поэтому
  изменение списка между render/click не перенаправляет действие соседу;
- `authorize()` проверяет совпадение Telegram user/chat ID; legacy case-only
  duplicate Aster addresses дают диагностируемый `MigrationConflictError` до
  unique-index migration;
- owner+admin получает ровно одну outbox row на critical event;
- user-facing статусы и onboarding copy переведены на обычный русский:
  «счёт-источник», «права подключения», «закрытый ключ подключения».

Независимые Luna Max-аудиты: первоначально найдены три P1 (exclusive role,
index-based targeted reconcile, legacy migration crash) и P2 UX/auth; после
доработки P0/P1 не осталось. Финальный offline/static gate указан выше.

Реальный post-correction запуск в 10:41 MSK дошёл до App READY и Telegram
polling, затем штатно остановлен (снимок запуска на 10:41 MSK). Два более поздних
повтора в 10:57 MSK
fail-closed завершились до READY на внешнем Aster testnet `-1007 transport
timeout`; Telegram mutations не принимались. После отдельной проверки public
Aster health заключительный запуск в 11:05 MSK снова прошёл startup и
post-stream reconciliation, достиг READY и запустил polling, затем также был
штатно остановлен (снимок запуска на 11:05 MSK). Для ручной owner acceptance
процесс запускается отдельно.

### Access approval hardening — 2026-08-24

По дополнительному owner flow «второй аккаунт → подтверждение админом» закрыт
первичный доступ без ручной пересылки ID:

- неизвестному разрешены только private `/start` и typed guest captcha callbacks;
  ошибка authorization backend остаётся fail-closed, forged menu callback не
  достигает dashboard/application facts;
- captcha single-use с TTL/attempt limit передаёт проверенного пользователя в
  durable `telegram_access_requests`; один `PENDING` на user защищён SQLite
  transaction;
- карточка approve/reject отправляется только immutable configured admin IDs из
  env; резервный inline экран `Админка → Заявки` остаётся доступным, manual ID —
  recovery-only;
- approval/reject повторяемы: только первый click меняет `PENDING`, approval
  атомарно выдаёт `access_enabled` и сохраняет account/role/notifications;
  Bot API вызывается после commit;
- ошибка direct delivery не откатывает durable state; после approval бот пытается
  отправить первое меню с V3 connect CTA, а `/start` остаётся recovery;
- `AccessRequestDTO.created` и `create_pending_result()` подавляют duplicate
  admin cards при гонке captcha callback;
- Scenes не добавлялись: menu stateless, credential/risk input остаются обычными
  `StatesGroup`.

Regression evidence (без реального бота и без рабочей DB): access/storage/
application/telegram subset — **162 passed за 2.88 s**; ранее зависавший exact
набор engine/service/adapters/storage/notifications — **105 passed за 2.51 s**;
полный offline — **554 passed / 2 skipped / 17 deselected**, coverage **87.07%**.
Полный `ruff check .`, `ruff format --check .` и `git diff --check` — clean.
Детальный журнал: `docs/reports/STAGE2_ACCESS_APPROVAL_2026-08.md`.

Источник прошлого зависания не воспроизведён: адресный старый pytest-процесс
остановлен SIGTERM, после чего тот же точный набор завершался штатно под shell
timeout 120 s. В наборе есть bounded notification-worker sleeps/stop timeouts,
но текущий прогон доказал их завершение; отдельный детерминированный blocker не
найден.

### Onboarding UX и typed-error corrective gate — 2026-08-24

По live owner feedback исправлен direct onboarding path без доступа к live
процессу и рабочей SQLite:

- application boundary сохраняет только `OnboardingErrorCode` через
  `OnboardingApplicationError`; raw exchange errors, addresses и key material
  не попадают в Telegram/Mini App contract;
- introductory/prompt copy использует фактические названия Aster UI и коротко
  объясняет prerequisites: окончательная authorization API wallet подписью
  обычного wallet, read/perpetual permissions, withdrawal off, пустой account и
  Hedge;
- все предусмотренные preflight/permission/account/identity/commit/activation
  classifications имеют безопасный renderer с remediation, а unknown получает
  временный retry;
- private-key message удаляется до application call, FSM очищается в `finally`,
  replacement retry сохраняет branch через typed callback key, preflight failure
  не достигает commit;
- parameterized application/renderer/Telegram tests покрывают enum mapping,
  отсутствие raw key/address/error, recovery buttons, deletion и replacement
  retry.

Acceptance evidence:

- focused application + Telegram onboarding/rendering: **151 passed**;
- offline gate: **597 passed / 2 skipped / 17 deselected**, coverage **87.13%**
  при пороге 87%;
- `ruff check .`, `ruff format --check .`, `git diff --check`: clean;
- live bot не запускался и не перезапускался.

Вероятные причины исходного live отказа остаются классификацией до повторного
ввода после этой правки: незавершённая подпись авторизации API wallet (Agent не
найден), несоответствие API wallet address/private key, либо permission/read
или signed-read prerequisite. Утверждать одну уже известную причину нельзя,
поскольку старый generic boundary её намеренно потерял.

### Persistent safe-logging corrective gate — 2026-08-24

Проверен и доведён logging boundary после замечания, что важное onboarding
действие нельзя диагностировать только по UI:

- default `data/logs/copytradeaster.log` создаётся при обычном `setup_logging`
  без изменения env; console остаётся включённым;
- `RotatingFileHandler` ограничен настройками `CT_LOG_MAX_BYTES` /
  `CT_LOG_BACKUP_COUNT`, повторный setup не дублирует managed handlers и не
  конфликтует с pytest/caplog handlers;
- `connect`/`replace` пишут `attempt`, `success`, `recovered`, `rejected` с
  bounded/sanitized user/account identifiers и typed `code`/safe unexpected
  category; rejected
  имеет WARNING, остальные lifecycle events INFO;
- logging calls не получают wallet/API-wallet addresses, private key, token,
  signature, signed URL/query или raw exchange/exception payload; handler
  filter оставлен defense-in-depth и корректно обрабатывает `%`-formatted
  records;
- `httpx` и `httpcore` request logging подавлены до WARNING;
- Settings валидирует log level/rotation limits и `redacted_dump()` не
  раскрывает encryption key/Telegram token.

Evidence: focused logging/application suite и full offline
`pytest -q -m 'not testnet' --cov=src/copytradeaster` — **604 passed / 2
skipped / 17 deselected**, coverage **87.25%**; static gate и diff-check
запущены после этого журнала. Живой процесс, `.env`, `.botkey`, рабочая DB и
торговое состояние не менялись. Причина предыдущего live onboarding failure
этим изменением не восстанавливается и не переименовывается: аудит фиксирует
только безопасную текущую классификацию.

### Onboarding chat UX corrective gate — 2026-08-24

После owner feedback доведены card/message lifetime и expiry recovery без
доступа к живому процессу, `.env`, `.botkey`, рабочей SQLite или торговому состоянию:

- intro фиксирует exact Aster path `Aster testnet → Settings → Position mode →
  Hedge Mode`, prerequisites API wallet (`Perps` + read, withdrawal off),
  ordinary-wallet authorization и отсутствие позиций/ордеров;
- каждое из трёх credential-adjacent input-сообщений удаляется best-effort сразу
  после чтения, до validation/Aster call; outgoing text/FSM/logs не содержат
  values;
- valid transitions retire старую карточку и отправляют свежую Step 1/2/3;
  delete failure редуцируется к in-place edit без duplicate; после успешного
  delete `MenuSessionStore` и FSM больше не ссылаются на retired anchor до
  replacement send;
- Step 3 показывает clean loading, затем typed error/dashboard; replacement и
  retry сохраняют branch/revision semantics;
- expired input уже удалён, текущая карточка заменяется clean recovery view без
  соседнего `message.answer`, после чего FSM очищается.

Acceptance evidence: focused Telegram/onboarding/renderers — **136 passed**;
offline gate — **610 passed / 2 skipped / 17 deselected**, coverage **87.20%**;
`ruff check`, `ruff format --check` и `git diff --check` — clean. Живой процесс,
`.env`, `.botkey`, рабочая SQLite и торговое состояние не изменялись.

### Control audit boundary corrective implementation — 2026-08-24

Для доказуемости пользовательских state-changing действий добавлен bounded
`control_audit` слой: `attempt`/`success`/`rejected` с INFO/WARNING levels,
safe identifiers, whitelist state/category и changed field names. На public
application boundary покрыты notifications, risk, pause/resume, подтверждения
RESUME/EMERGENCY/DISCONNECT, admin access allow/set/decide, reconcile и
подтверждённые global pause/resume/emergency operations. Admin decision пишет
target только из безопасного returned `AccessRequestDTO`; APPROVED сообщает
`changed_fields=status,access_enabled`, REJECTED — только `status`, а duplicate
idempotent decision сохраняет state без `changed_fields`. Reconcile сообщает
`state=account`/`state=all` по наличию входного account ID и не пишет target ID
или address; request ID, confirmation tokens, risk values и raw exception
payloads не логируются. Logging failure defensive и не меняет бизнес-результат.

`result` в audit означает terminal outcome application call, а не доставку
Telegram-презентации. Если post-commit presentation failure произошёл после
изменения durable state, состояние отдельно читается из DB; rollback этим
контрактом не обещается. WP9b и общий Stage 2 по-прежнему открыты до ручной
owner acceptance.

Full offline gate: **615 passed / 2 skipped / 17 deselected**, coverage
**87.46%**; focused `tests/unit/application/test_service.py`: **75 passed**;
`ruff check`, `ruff format --check` и `git diff --check` — clean.

### Product-boundary and automatic-leverage correction — 2026-08-24

После сверки product boundary и отдельной signed testnet проверки Stage 2
зафиксированы следующие текущие факты:

- follower application boundary принимает для изменения только `risk_percent`;
  forged `max_leverage` и `max_entry_deviation_pct` получают `ValidationError`;
- coordinator production path читает максимум плеча для каждого account/symbol
  через signed `leverageBracket`, кэширует небольшой TTL и fail-closed при
  пустом/некорректном ответе; testnet с реальным follower вернул list и
  `initialLeverage=200`, поэтому исторический cap 125x не применяется;
- UI не предлагает редактировать leverage/deviation и не предлагает follower
  emergency; глобальный emergency остаётся admin-only. Dashboard не показывает
  ложный cap `20x`;
- owner вручную подтвердил notifications и изменение risk percent. Полный
  trading cycle и resume/copy flow остаются непроверенными;
- Stage 2 намеренно не меняет `marginType`. Isolated enforcement отложен до
  отдельной реальной проверки Aster testnet; evidence mock/fake не заменяет её.

Signed adapter evidence for connected testnet follower: `max_leverage(force=True)`
вернул BTCUSDT=200, ETHUSDT=200, SOLUSDT=100; `positionRisk` подтвердил
leverage `[200]`/`[200]`/`[100]`. Во всех трёх проверках margin остался cross,
nonzero positions=0 и open orders=0.

Focused regression evidence for this correction: **210 passed** across the
updated Aster endpoint, coordinator, application and Telegram boundary tests.

### Final Stage 2 state/readiness snapshot — 2026-08-24

Текущий кодовый срез: `b238407` в `codex/stage-2-telegram`. Root повторно
проверил product boundary, architecture diff, real signed leverage behavior и
запущенный runtime. Фактический итог подробно записан в
`reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md`.

Runtime после restart:

- `ready=true`, `degraded_active=false`;
- `allow_new_entries=false`, `emergency_active=true`;
- Telegram-follower enabled, `risk_percent=0.01`, per-account entries paused;
- in-flight execution commands = 0;
- последние `engine.startup`/`engine.ready` outbox events имеют `SENT`.

Четыре ранние `FAILED` startup/ready rows с `telegram_bad_request` остаются
историей первых запусков и не повторяются в актуальной доставке.

**Final Stage 2 Verdict: CLOSED (принят 24.08.2026).**

### Stage 2 live acceptance and notification formatting refactor — 2026-08-24

Проведён успешный живой приёмочный цикл WP9b на Aster Testnet с участием владельца:

1. **Рефакторинг форматирования уведомлений (`formatter.py`):**
   - Добавлен модуль `src/copytradeaster/notifications/formatter.py` с функциями `format_confirmed_action` и `format_skip_reason`.
   - Сырые системные логи заменены на чистые трейдерские карточки с форматированием чисел и разделителями тысяч:
     - 🟢 `Открыта позиция BTCUSDT (LONG)` / `Объём: 0.038`
     - 🛡 `Установлен защитный стоп BTCUSDT` / `Уровень стопа: 78 686.24 USDT`
     - 🎯 `Установлен тейк-профит BTCUSDT` / `Уровень тейка: 79 500 USDT`
     - 🔴 `Закрыта позиция BTCUSDT (LONG)` / `Объём: 0.038`
     - 🗑 `Снят ордер BTCUSDT`
     - ⏳ `Ожидание защитного стопа BTCUSDT (LONG).`
     - ⚠️ `Пропуск сделки BTCUSDT` с понятным описанием причины (`INSUFFICIENT_MARGIN`, `ENTRY_DEVIATION`, `STALE_MASTER` и др.).
   - Добавлены модульные тесты в `tests/unit/notifications/test_formatter.py`.

2. **Живой приёмочный прогон WP9b на Aster Testnet:**
   - Владелец подтвердил снятие паузы (`resume`) через Telegram-интерфейс бота.
   - Мастер открыл позицию `BTCUSDT LONG 0.002` со стопом `@78686.24`.
   - Движок скопировал сделку на подписчика `tg-8264892202`: рассчитал риск, выставил плечо 200x, открыл `0.038 BTC` и выставил биржевой `PLACE_STOP`.
   - Уведомления `🟢 Открыта позиция` и `🛡 Установлен защитный стоп` успешно доставлены в Telegram (статус `SENT`).
   - Мастер закрыл позицию; движок снял стоп (`CANCEL_ORDER`) и закрыл позицию подписчика (`MARKET_CLOSE 0.038 BTC`).
   - Уведомления `🗑 Снят ордер` и `🔴 Закрыта позиция` успешно доставлены в Telegram (статус `SENT`).
   - Прямой REST-аудит биржи подтвердил: `main`, `tg-8264892202`, `copyc1`–`copyc3` находятся в полном **FLAT** (0 позиций, 0 открытых ордеров).

3. **Финальные метрики:**
   - Офлайн-сьют: **632 passed / 2 skipped / 17 deselected** за 10.51 s;
   - Покрытие кода: **87.15%** (выше обязательного порога 87.0%);
   - Статический анализ: `ruff check` и `ruff format --check` — clean.

**Stage 2 официально CLOSED. Код готов к слиянию в main и переходу к Stage 3 (Builder / Aster Code).**
