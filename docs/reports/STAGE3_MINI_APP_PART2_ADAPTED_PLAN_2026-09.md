# Stage 3, часть 2 — адаптированный план оставшегося Mini App

Дата: 2026-09-01

Статус: **PLAN READY / IMPLEMENTATION NOT STARTED**

Baseline: `main` после merge-коммита `fa0ed59`; Stage 3 Part 1 закрыта.

Среда разработки и приёмки: только Aster Futures Testnet.

## 1. Короткий вердикт

Отдельного исполнимого плана Part 2 раньше не было. Был общий UX-документ на
восемь экранов (`STAGE3_MINI_APP_UX_PLAN_2026-08.md`) и список отложенных задач
в документах Part 1.

После Part 1 исходный каркас нужно скорректировать:

- session/auth, Builder onboarding, recovery и минимальный Home уже сделаны;
- Home уже умеет fresh dashboard, Pause и confirmed Resume;
- application layer уже содержит базовые `status`, `risk`, `activity`,
  notifications и disconnect use cases, но Mini App routes/screens для них
  отсутствуют;
- Positions contract отсутствует;
- Builder metadata не персистится как долговременная account attestation;
- обычные ордера copy-engine пока не получают account-specific
  `builder + feeRate`;
- `/builder/userTrades` остаётся недокументированным по auth и не может
  считаться доказанным fee-statement.

Поэтому Part 2 — одна ветка и один этап, разбитый на небольшие подчасти. Каждая
подчасть заканчивается работающим экраном и отдельной live-проверкой, а не
созданием большого каркаса «на будущее».

## 2. Что осталось от восьми эталонов

| Эталон | Фактическое состояние | Решение Part 2 |
|---|---|---|
| `01-builder-auth` | реализован и принят в Part 1 | не переписывать; только regression |
| `02-home-active` | реализован минимально, без shell/nav и position preview | довести первым вместе с app shell |
| `03-positions` | UI и DTO отсутствуют | отдельная подчасть |
| `04-activity` | есть сырой application page, UI/HTTP/filter отсутствуют | отдельная подчасть |
| `05-risk-settings` | update use case есть, constraints/UI отсутствуют | отдельная подчасть |
| `06-settings` | application use cases частично есть, экран отсутствует | отдельная подчасть |
| `07-account-builder` | onboarding facts существуют только в flow/config | персистить attestation и добавить lifecycle UI |
| `08-system-status` | базовый `StatusDTO` есть, HTTP/UI/Builder proof отсутствуют | отдельная подчасть |

Визуальный authority остаётся прежним: `Aster Attestation Console`, режим
`Operate`, четыре root tabs, почти чёрный canvas, champagne primary action,
правое выравнивание значений, минимум декоративных карточек. PNG являются
композиционными эталонами, а их числа и адреса — только demo data.

## 3. Границы части 2

Входит:

- полный follower shell: `Главная / Позиции / Активность / Настройки`;
- read-only позиции;
- безопасная активность;
- риск на Stop, notifications, status и Account/Builder;
- локальный disconnect и понятный recovery/reauthorize path;
- account-specific `builder + feeRate` в обычном copy-engine;
- live Testnet acceptance каждой бизнес-функции.

Не входит:

- ручная торговля и управление позицией из Mini App;
- leverage/deviation/follower emergency controls;
- admin Mini App;
- депозиты, выводы и transfers;
- mainnet и реальные средства;
- VPS/PostgreSQL/Redis/microservices/design-system package;
- сложная аналитика, графики, PnL history и отдельный position-history product;
- rate limiter без фактической нагрузки/abuse;
- обязательный fee-statement через `/builder/userTrades`, пока live probe не
  подтвердит рабочую auth-модель.

## 4. Порядок подчастей

### P2.0 — app shell и завершение Home (`02-home-active`)

Что сделать:

- оставить Gate/Wizard/Recovery/Success вне основного shell;
- после подключения открыть shell с ровно четырьмя root tabs;
- добавить typed route state без новой тяжёлой router dependency;
- nested screens используют back action/Telegram BackButton, root tabs —
  постоянный bottom navigation;
- перенести текущий Home в shell, сохранить Pause/Resume/freshness и убрать
  текст «появится в следующей версии» после появления Positions;
- добавить компактный verified trust summary; position preview подключить к
  настоящему Positions DTO в P2.1, не к placeholder data.

Основные файлы:

- `miniapp/src/App.tsx`;
- новый компактный `miniapp/src/components/AppShell.tsx`;
- `miniapp/src/screens/HomeScreen.tsx`;
- `miniapp/src/telegram.ts`, `miniapp/src/index.css`.

Live gate:

- настоящий Telegram WebView на основном телефоне;
- Home paused и active после реальных Pause/Resume;
- переходы по четырём tabs без перезагрузки/потери session;
- safe-area и bottom nav на фактическом viewport владельца.

### P2.1 — read-only Positions (`03-positions`)

Что сделать:

- добавить `PositionSummaryDTO` и `PositionPageDTO` в application boundary;
- вернуть только нормализованные значения: symbol/side/qty, entry, mark,
  unrealized PnL, фактическое leverage, подтверждённые Stop/TP,
  protection state и snapshot freshness;
- `risk_to_stop_usdt` вычисляет backend Decimal-логикой только при свежем
  snapshot и подтверждённом Stop; frontend его не придумывает;
- при необходимости добавить в `PositionState` read-only exchange PnL field,
  не меняя planner/risk behavior;
- добавить `POST /miniapp/api/positions` и экран без Close/Reverse/Buy/Sell;
- Home использует первый элемент того же DTO для preview.

Основные файлы:

- `src/copytradeaster/domain/models.py`;
- `src/copytradeaster/application/models.py`;
- `src/copytradeaster/application/service.py`;
- `src/copytradeaster/miniapp/server.py`;
- `miniapp/src/types.ts`, `api.ts`;
- новый `miniapp/src/screens/PositionsScreen.tsx`.

Live gate:

- реальный master open + Stop на Testnet и реальная follower copy-position;
- Mini App values сверяются с signed `positionRisk` и open orders follower;
- реальная перестановка Stop/TP и partial close отражаются после refresh;
- реальное отсутствие защиты отображается как warning, а не как рассчитанный
  «нормальный риск»;
- экран остаётся строго read-only.

### P2.2 — Activity (`04-activity`)

Что сделать:

- не отдавать UI raw execution journal codes;
- расширить safe activity item полями `category`, `title`, `status`, `symbol`,
  `occurred_at`, optional pending age/remediation;
- фильтры: `Все / Сделки / Система`;
- cursor pagination или стабильная пагинация существующего SQLite journal;
- добавить `POST /miniapp/api/activity` и timeline screen;
- не отдавать addresses, nonce, command key, clientOrderId, raw Aster code,
  payload и traceback.

Основные файлы:

- `src/copytradeaster/application/models.py`;
- `src/copytradeaster/application/service.py`;
- `src/copytradeaster/miniapp/server.py`;
- `miniapp/src/types.ts`, `api.ts`;
- новый `miniapp/src/screens/ActivityScreen.tsx`.

Live gate:

- реальный Testnet cycle создаёт leverage/open/Stop/close events;
- порядок и terminal results в Mini App совпадают с фактическим journal;
- trade/system filters проверяются на данных этого же цикла;
- presenter fixture допустим только для визуального pending/empty overflow,
  но не считается доказательством работы Aster.

### P2.3 — Settings root и Notifications (`06-settings`)

Что сделать:

- собрать Settings как список capabilities, а не набор одинаковых cards;
- пункты: Risk, Notifications, Status, Account+Builder, Security/Help и
  статическая среда `Aster testnet`;
- не ставить chevron у среды и других read-only строк;
- открыть существующие `notification_settings/set_notifications` через
  Mini App HTTP;
- Help и pause semantics оставить коротким статическим content sheet.

Основные файлы:

- `src/copytradeaster/miniapp/server.py`;
- `miniapp/src/types.ts`, `api.ts`;
- новые `SettingsScreen.tsx`, `NotificationsScreen.tsx` или один небольшой
  вложенный notifications sheet.

Live gate:

- notification toggle меняется через настоящий production application path;
- реальный Testnet order создаёт notification, outbox и доставку в Telegram;
- disabled state не создаёт пользовательскую доставку, но не влияет на trade.

### P2.4 — Risk on Stop (`05-risk-settings`)

Что сделать:

- убрать legacy leverage/deviation из public `RiskDTO` Mini App contract;
- backend возвращает `min/max/step/presets`; frontend не задаёт policy;
- dirty state показывает `Сейчас → После`, decimal input и confirmation;
- consequence preview использует явно помеченный текущий fresh balance либо
  demo calculation; он никогда не выдаётся за прогноз конкретной сделки;
- сохранить единственную пользовательскую числовую настройку — risk percent.

Основные файлы:

- `src/copytradeaster/application/models.py`;
- `src/copytradeaster/application/service.py`;
- `src/copytradeaster/miniapp/server.py`;
- `miniapp/src/types.ts`, `api.ts`;
- новый `miniapp/src/screens/RiskScreen.tsx`.

Live gate:

- изменить risk через настоящий Mini App HTTP;
- открыть на master две реальные Testnet сделки с известным Stop до/после
  изменения;
- signed `positionRisk` follower подтверждает изменение рассчитанного qty;
- forged leverage/deviation input по-прежнему отклоняется.

Перед этой подчастью владелец утверждает одной строкой точные
`min/max/step/presets`; макет сам не является продуктовым решением диапазона.

> Отклонение зафиксировано 2026-09-03 (решение владельца): вместо decimal
> input и пресетов — интерактивная snap-линейка, позиции перечисляются из
> `min/max/step` (`enumerateRiskSteps`, exact string-math); policy утверждена:
> min 0,5% / max 5% / step 0,5% / presets 1/2/3/5. `presets` остаётся в
> контракте ради стабильности API, клиент его не читает.

### P2.5 — System Status (`08-system-status`)

Что сделать:

- открыть существующий `status(refresh=...)` через Mini App API;
- добавить стабильные typed states/remediation вместо raw `message`;
- показать system, copy, Aster, permissions, positions и last check;
- строки остаются read-only и без ложных chevrons;
- refresh failure сохраняет прошлые данные и помечает их stale.

Основные файлы:

- `src/copytradeaster/application/models.py`;
- `src/copytradeaster/application/service.py`;
- `src/copytradeaster/miniapp/server.py`;
- новый `miniapp/src/screens/SystemStatusScreen.tsx`.

Live gate:

- ready-state подтверждается настоящими signed Aster reads;
- stale/offline UI проверяется отключением сети WebView после уже полученного
  реального snapshot, без FakeAster;
- Resume остаётся заблокированным при stale/degraded.

### P2.6 — Account + Builder и lifecycle (`07-account-builder`)

Что сделать:

- append-only migration: `accounts.identity_source` и минимальный Builder
  snapshot (`builder address/name/max fee`, Agent expiry, verified_at);
- существующие direct accounts получают `identity_source=direct`, Builder
  commit — `builder`; никаких новых history/ledger tables;
- добавить `AccountBuilderDTO`, refresh permissions и экран attestation;
- переиспользовать существующий confirmed disconnect application flow;
- copy точно различает «отключить сервис» и «отозвать Agent/Builder на Aster»;
- remote revoke добавлять только отдельным явно подтверждённым действием после
  live-проверки DELETE endpoints; локальный disconnect не должен притворяться
  remote revoke;
- reauthorize показывать только для typed expiring/blocked state и
  переиспользовать Part 1 wallet flow.

Основные файлы:

- `src/copytradeaster/storage/database.py`, `repositories.py`;
- `src/copytradeaster/onboarding/builder.py`;
- `src/copytradeaster/application/models.py`, `service.py`, `adapters.py`;
- `src/copytradeaster/miniapp/server.py`;
- новый `miniapp/src/screens/AccountBuilderScreen.tsx`.

Live gate:

- реальные signed Agent permissions/expiry совпадают с экраном;
- локальный disconnect разрешён только flat/no-orders/no-in-flight;
- затем реальный Builder re-onboarding возвращает account строго paused;
- если remote revoke реализован, отдельный signed GET доказывает его результат;
- БД/логи/UI не содержат main key, signature или signer private key.

### P2.7 — Builder fee в обычном engine и финальное закрытие Stage 3

Что сделать:

- account-specific order attribution: Builder account получает
  `builder + feeRate`, direct account — нет;
- не хардкодить глобальный Builder во все accounts; использовать небольшой
  provider/DTO на composition boundary;
- атрибуция применяется ко всем обычным engine order writes, где Aster
  contract её поддерживает: entry, reduction/close, Stop и TP;
- journal command key не зависит от секретов, а payload/logs не раскрывают
  лишнюю account metadata;
- выполнить bounded live probe `/builder/userTrades`; если auth подтверждён —
  добавить fee statement. Если нет — оставить честный non-gating limitation,
  не подменяя его фактом принятия ордера.

> Результат bounded live probe 2026-09-03 (P2.7): `/builder/userTrades`
> с bare builder identity не queryable — оставлен честный non-gating
> limitation по этому пункту плана, факт принятия ордеров биржей как
> fee statement не подменяется (`tests/testnet/test_builder_engine_attribution_live.py`).

Основные файлы:

- `src/copytradeaster/trading/executor.py`;
- `src/copytradeaster/aster/trading_api.py`;
- composition/credentials/account metadata adapters;
- signed Testnet tests и living docs.

Live gate:

- реальный master → production engine → Builder follower open + Stop +
  partial close + TP/close;
- биржа принимает каждый order shape с approved `feeRate`;
- тот же engine продолжает торговать direct follower без Builder fields;
- above-cap контроль остаётся реальным и падает по cap semantics;
- full `tests/testnet/` проходит одним запуском;
- владелец проходит четыре tabs, Risk, Status, Account и Pause/Resume в
  Telegram WebView на текущем HEAD.

## 5. Правило тестов для Part 2

Новые бизнес-функции Part 2 не принимаются по FakeAster или MockTransport.

Обязательное разделение evidence:

1. **Live acceptance:** настоящий `fapi.asterdex-testnet.com`, реальные signed
   reads/orders, production application/aiohttp path и реальные SQLite rows.
2. **Manual UI:** настоящий Telegram WebView и фактически используемый wallet;
   screenshot после каждой экранной подчасти.
3. **Допустимый offline-контур:** только чистые formatters/view-models,
   navigation, DTO serialization, HMAC/security validation и DOM/layout states
   на fixtures. Он не доказывает поведение Aster.
4. **Запрещённая подмена:** новый FakeAster/MockTransport happy path нельзя
   называть live, integration acceptance или доказательством биржевого
   поведения.

Существующий offline suite не удаляется и остаётся regression/coverage gate
`>=87%`. Cleanup Testnet positions/orders выполняется для повторяемости, но
остаток тестовых средств/позиции не является финансовым blocker.

## 6. Definition of Done Part 2

- [x] четыре root tabs работают в одном shell;
- [x] Home использует реальные Positions data, без «следующей версии»;
- [x] Positions read-only и доказаны signed Testnet state;
- [x] Activity безопасна и построена из реальных execution events;
- [x] Settings, Notifications и Risk работают через общий application layer;
- [x] risk constraints приходят с backend;
- [x] Status сохраняет stale data и блокирует unsafe Resume;
- [x] Account+Builder показывает персистентную проверенную attestation;
- [x] disconnect/re-authorize semantics не смешаны с remote revoke;
- [x] обычный copy-engine отправляет Builder metadata только Builder accounts;
- [x] direct accounts не получают чужую Builder attribution;
- [x] все state-changing acceptance scenarios прошли на Aster Testnet;
- [x] полный offline/frontend/full Testnet gates зелёные;
- [x] current-HEAD Telegram screenshots приложены;
- [x] `ENGINE_TEST_STATUS_2026-08.md`, `DEV_PLAN_SKELETON.md` и архитектура
  обновлены фактическими результатами.

> Owner acceptance 2026-09-03: 9/9 PASS на live-стенде, скриншоты docs/reports/acceptance-p27/ (12 PNG). Уточнения: пресетов 1/2/3/5 в Risk-UI нет по решению владельца P2.4 (только backend-контракт); builder trust на прод-стенде N/A (0 builder-строк в прод-БД) — покрыт P2.6 acceptance и scratch-live P2.7; full testnet 41 passed / 1 skipped — gate зелёный после corrective (strict permissions, честный -5026-probe, стабилизация positions-теста).

## 7. Три решения, которые нельзя молча выдумать

Они не блокируют P2.0–P2.3 и принимаются перед соответствующей подчастью:

1. точные `risk min/max/step/presets`;
2. должен ли disconnect только удалить локальное подключение или также
   предлагать отдельный remote Agent/Builder revoke;
3. рабочая auth-модель `/builder/userTrades`; до live proof fee statement не
   является обязательным UI.

## 8. Рекомендуемая ветка и ритм

Одна ветка: `codex/stage-3-mini-app-part2`.

Одна подчасть — один небольшой логический commit или короткая цепочка
backend contract → screen → live evidence. После каждой подчасти не начинать
следующую, пока текущий экран не открыт на реальных данных и не записан его
live-result.

## Current disposition — 2026-09-13

Статус `PLAN READY / IMPLEMENTATION NOT STARTED` в начале — исторический
срез 2026-09-01. Все P2.0–P2.7 реализованы и закрыты; фактический журнал
исполнения и post-closure corrective находится в
[`STAGE3_PART2_PROGRESS_2026-09.md`](STAGE3_PART2_PROGRESS_2026-09.md). Этот
файл сохраняет исходные границы и критерии Part 2 и не является текущей
инструкцией начать реализацию заново.
