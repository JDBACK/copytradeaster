# Каркас плана разработки копитрейд-бота на AsterDex

Статус: Stage 1 и Stage 2 закрыты; автоматическая и живая приёмка Stage 2 в
Telegram (включая resume/copy cycle на реальном тестнете) успешно завершена
24.08.2026. Видение — три этапа (раздел 4). Полный фактологический бэкграунд — в
[ASTER_API_RESEARCH_2026-08.md](ASTER_API_RESEARCH_2026-08.md).
## 1. Стек

```text
Python 3.12
aiogram 3.30.0          — Telegram-бот (онбординг, команды, уведомления)
httpx (async)          — REST к fapi.asterdex.com / fapi.asterdex-testnet.com
websockets             — user-data stream мастера + market stream
eth-account            — EIP-712 подписи (ApproveAgent/ApproveBuilder, ордера)
SQLite (aiosqlite)     — MVP: один файл, WAL, без инфраструктуры
PostgreSQL (asyncpg)   — продакшн: переезд на тонкие репозитории, без ORM
pydantic v2            — схемы ордеров/событий/конфигов, валидация
```

Приватные ключи signer-ов — только зашифрованное хранилище (мастер-ключ в env), в SQLite/PG — только следы и торговые данные.

## 2. Что за бот и как должен работать

### Роли

- **Мастер (ты):** один аккаунт, на котором открываются/закрываются сделки, переставляются стопы/тейки.
- **Фолловеры:** подписчики, аккаунты которых копируют действия мастера.

### Поток работы

1. Бот подключается к user-data WebSocket мастера (listenKey).
2. Мастер открыл/закрыл/переставил SL/TP/сократил позицию → бот получает событие (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, MARGIN_CALL).
3. Бот пересчитывает целевое состояние по каждому фолловеру: размер депозита + процент риска на стоп → риск-ревард → размер позиции.
4. Бот выставляет ордера на аккаунтах фолловеров (открытие, закрытие, частичное закрытие, SL/TP, автоматический максимум плеча для symbol).
5. Уведомления о действиях и статусах — в Telegram.

Подробности по API-возможностям — разделы 2–6 ресерча.

## 3. Что собрали по Астеру (со сносками на ресерч)

| Тема | Суть | Сноска в ресерче | Ссылка (URL) |
|---|---|---|---|
| Версия API | V3 — Recommended; V1 — Legacy (новые V1 не выдаются с 25.03.2026) | §1 | [API Docs index](https://github.com/asterdex/api-docs/blob/master/README.md) |
| Ключи V3 | user + signer + private key + nonce + EIP-712; два signing context (trading 1666/714, registration 56/101) | §1, §5 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md), [API Wallet](https://www.asterdex.com/en/api-wallet) |
| Права агента | canSpotTrade / canPerpTrade / canWithdraw / expired / ipWhitelist; canWithdraw=false для копитрейда | §1 | [Agent endpoints](https://asterdex.github.io/aster-api-website/asterCode/endpoints/) |
| Торговля | balance, positionRisk, leverage, marginType, open/close, partial close, SL/TP, trailing | §2, §4 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) |
| Перестановка SL/TP | Обычный — cancel+new; Strategy OCO/OTOCO — API умеет, WS-поведение при ручной смене требует теста | §2, §3 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md), [User Data Streams](https://asterdex.github.io/aster-api-website/futures-v3/user-data-streams/) |
| Слежение за мастером | user-data WebSocket: ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, ACCOUNT_CONFIG_UPDATE, MARGIN_CALL, listenKey 60 мин + keepalive | §3 | [User Data Streams](https://asterdex.github.io/aster-api-website/futures-v3/user-data-streams/) |
| Копирование | По состоянию/филлам, не по командам; partial fill → target-state reconciliation | §3 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) |
| Риск от стопа | % от депозита на стоп → размер позиции; расчёт делает бот, не Aster | §4 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) |
| Sub-account | Изоляция средств, V3 keys, withdrawal постоянно off; лимиты 10–50 шт | §1 | [Sub Accounts](https://docs.asterdex.com/trading/sub-accounts) |
| Testnet | fapi.asterdex-testnet.com, chainId 714, faucet 1000 USDT + 1000 ASTER/день | §7 | [Testnet API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-testnet.md), [TestNet docs](https://docs.asterdex.com/aster-chain/testnet), [Faucet](https://www.asterdex-testnet.com/en/faucet), [Testnet API Wallet](https://www.asterdex-testnet.com/en/api-wallet) |
| Builder (Aster Code) | Онбординг: юзер подписывает ApproveAgent+ApproveBuilder, signer создаёт билдер; feeRate кап 0.001; на тестнете маршруты живут, веб-вкладки нет | §9 | [Aster Code docs](https://docs.asterdex.com/program-and-rewards/aster-code), [Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/integration-flow/), [Futures Auth](https://asterdex.github.io/aster-api-website/asterCode/authentication/), [API demo](https://github.com/asterdex/API-demo/tree/main/aster-code-demo) |

Прочие справочные URL:

- [Aster API Changelog](https://github.com/asterdex/api-docs/blob/master/CHANGELOG.md)
- [Aster V3 Deposit & Withdrawal](https://asterdex.github.io/aster-api-website/futures-v3/deposit%26withdrawal/)
- [Aster legacy API management](https://www.asterdex.com/en/api-management)
- [Aster Code Spot Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/spot-integration-flow/)
- [Aster Code Builder frontend demo](https://github.com/jupiter-hongc/aster-code-builder-demo)
- [Live mainnet exchangeInfo](https://fapi.asterdex.com/fapi/v3/exchangeInfo)
- [Live testnet exchangeInfo](https://fapi.asterdex-testnet.com/fapi/v3/exchangeInfo)
- [Aster Skills Hub](https://github.com/asterdex/aster-skills-hub)

Подробная аргументация и полная матрица доказательств — [ASTER_API_RESEARCH_2026-08.md](ASTER_API_RESEARCH_2026-08.md).
## 4. Видение: три этапа

Функционал не завязан на бота: бот — обвязка (онбординг + уведомления), всё ядро гоняется скриптами. Порядок: скрипты → бот с прямым вводом ключей → Builder.

### Этап 1 — Скрипты, без бота (весь бэкенд)

- Стрим мастера: user-data WS (открытие, закрытие, перестановка SL/TP, частичное закрытие, MARGIN_CALL), listenKey keepalive, reconnect + snapshot.
- Риск: % депозита на стоп → размер позиции, риск-ревард.
- Исполнение на копиках: MARKET, SL/TP, partial close, плечо.
- Reconciliation: target-state, идемпотентность повторов.
- Builder-подписи скриптом: approveAgent + builder одной подписью, feeRate (тестнет).
- Уведомления — в консоль/лог. Всё на тестнете; ключи из wallet.txt (main + 4 копика).

### Этап 2 — Бот + прямой ввод ключей

**Текущий verdict:** Stage 2 **CLOSED (принят 24.08.2026)**. Все автоматические
и живые проверки, включая прямой ввод ключей, Telegram UI, управление доступом,
автоматическое плечо, уведомления и полный живой resume/copy trading cycle на
Aster Testnet выполнены и доказаны. Подробный фактический срез:

- aiogram: приём V3-набора (адрес обычного testnet-кошелька + `API wallet address` + `private key` под ним + права), проверка подключения копика (валидация canPerpTrade / ipWhitelist), настройка только процента риска на Stop.
- Торговое ядро этапа 1 подключается как библиотека; креды пользователя отделены от торговли.
- Уведомления в Telegram.

Текущий полный план:
[STAGE2_IMPLEMENTATION_PLAN_2026-08.md](reports/STAGE2_IMPLEMENTATION_PLAN_2026-08.md).
Фактологическая площадка по stable aiogram 3.30.0:
[STAGE2_AIOGRAM_RESEARCH_2026-08.md](reports/STAGE2_AIOGRAM_RESEARCH_2026-08.md).
Inline-first UX brief и решение по Scenes Wizard:
[STAGE2_TELEGRAM_UX_BRIEF_2026-08.md](reports/STAGE2_TELEGRAM_UX_BRIEF_2026-08.md).

- [x] Создать рабочую ветку `codex/stage-2-telegram` от accepted `main`.
- [x] Изучить фактические границы `App`/CLI/credentials/storage/notifications и
  trading core.
- [x] Сверить актуальный aiogram через Context7 и официальные источники.
- [x] Зафиксировать minimal one-process architecture и Stage 2 acceptance plan.
- [x] Спроектировать inline-first menu, общий Bot/Mini App application layer и
  принять решение не использовать experimental Scenes в Stage 2.
- [x] WP0: dependency/config/documented boundaries.
- [x] WP1: Telegram ownership, notification outbox и atomic credential storage.
- [x] WP2: signed `GET /fapi/v3/agent` + permission/expiry/ipWhitelist preflight.
- [x] WP3: aiogram-independent direct onboarding service.
- [x] WP4: public follower runtime registry и общие control/emergency services.
- [x] WP5: aiogram runtime, private-chat authorization, menu и lifecycle.
- [x] WP6: secure direct-V3 FSM без private key в state/logs.
- [x] WP7: risk/status/pause/resume/disconnect flows; per-account emergency оставлен только внутреннему control-core, global emergency — admin-only.
- [x] WP8: non-blocking Telegram notifications через SQLite outbox.
- [x] WP9a: offline, signed testnet, real Bot API/polling и 30/100-user
  automated gates.
- [x] Owner correction: один Telegram ID совмещает admin capability и owner;
  inline-админка содержит users/followers, выдачу/отзыв доступа и targeted
  reconcile.
- [x] Автоматический первичный доступ: `/start` → inline captcha → durable
  заявка → прямая карточка configured admin → idempotent approve/reject;
  approval сразу пытается открыть первое меню подключения, а админка содержит
  резервный список заявок. Ручной ID оставлен только для recovery.
- [x] Owner feedback corrective gate: onboarding copy использует названия и
  prerequisites из Aster UI (обычный testnet wallet, `API wallet address`,
  `private key`, авторизация подписью, permissions, пустой счёт, Hedge), а
  typed `OnboardingErrorCode` проходит application boundary без raw exchange
  payloads.
- [x] Typed onboarding failures: Telegram показывает причину и remediation,
  удаляет secret-message, даёт `🔄 Попробовать снова`/`⌂ На главную`, а retry
  сохраняет connect/replacement branch; preflight failure не достигает commit.
- [x] Onboarding chat UX correction: все три входных сообщения удаляются до
  проверки; шаги переходят на свежие карточки с edit-fallback без дубликатов,
  истёкший ввод заменяет stale-card recovery-экраном, а stale anchor не
  сохраняется в FSM между удалением и отправкой.
- [x] Операционный audit logging: persistent rotating file по умолчанию
  `data/logs/copytradeaster.log` + console, idempotent handlers, safe
  connect/replace lifecycle events и suppression `httpx`/`httpcore` request
  URLs; owner-only `0600` для текущего и rollover-файлов; попытка/отказ не
  утверждают `paused`, а success/recovered подтверждают `paused=true`; секреты,
  адреса и raw exchange payloads не являются logging arguments.
- [x] Control audit boundary для пользовательских state-changing действий:
  bounded `control_audit` attempt/success/rejected для notifications, risk,
  pause/resume, RESUME/DISCONNECT confirmations и admin
  access/reconcile/global controls; risk success перечисляет только имена
  изменённых полей, notification success различает enabled/disabled, admin
  decisions используют только target из безопасного returned DTO: APPROVED
  меняет `status,access_enabled`, REJECTED — только `status`, duplicate decision
  сохраняет state без `changed_fields`; reconcile различает `account`/`all` и
  не пишет target account ID/address. Rejected сохраняет только safe
  category/code/type. `result` — terminal outcome application call;
  post-commit presentation failure не обещает rollback: durable state при
  необходимости отдельно читается из DB.

- [x] Product-boundary correction: follower меняет только `risk_percent`;
  leverage автоматически выбирается по signed `leverageBracket` для каждого
  symbol без локального потолка; master leverage и legacy policy caps не
  показываются пользователю. Stage 2 пока не меняет `marginType`; isolated
  enforcement отложен до отдельной реальной проверки Aster testnet. Emergency
  для follower запрещён на application/UI boundary, global emergency остаётся
  admin-only.
#### WP9b — ручная приёмка владельца в private Telegram chat (принята 24.08.2026)

Все пункты ручной приёмки владельцем успешно завершены:

Безопасные проверки интерфейса и доступа (без торговых ордеров):

- [x] captcha проходит в private chat;
- [x] заявка на доступ создаётся и доступ выдаётся пользователю;
- [x] admin approval проходит через inline-карточку;
- [x] direct onboarding/подключение подтверждено владельцем;
- [x] переключатель notifications в Telegram подтверждён владельцем вручную;
- [x] изменение risk policy (risk %) в Telegram подтверждено владельцем вручную;
- [x] итоговое упрощённое inline-меню без follower leverage/deviation/emergency
  просмотрено владельцем; оценка владельца: «вроде всё пашет».

Торговые действия (подтверждены на живом тестнете 24.08.2026):

- [x] resume/copy cycle: выполнен полный цикл на живом Aster Testnet: снятие паузы
  в Telegram, открытие мастером BTCUSDT LONG 0.002 со стопом 78646.16,
  автоматический расчет риска, выбор плеча 200x, открытие 0.039 BTC на фолловере
  и выставление защитного стопа, доставка Telegram-уведомлений `MARKET_OPEN` и
  `PLACE_STOP`, закрытие мастером, полное закрытие фолловера во flat, отмена
  стопа и доставка уведомлений `CANCEL_ORDER` и `MARKET_CLOSE`.

### Leverage control-plane и финальная приёмка (2026-08-25)

Внешний acceptance `69fa148` (ревью `docs/reviews/review_leverage_control_plane.md`):

- [x] **P0 (#25) Dependency gating:** `ExecutionAction.action_id` и
  `depends_on_action_id` связывают `SET_LEVERAGE` с входом `MARKET_OPEN` и
  защитными ордерами; неуспешный SET блокирует зависимые действия (`BLOCKED`),
  но никогда не закрытия и отмены.
- [x] **P1 (#26) Flat leverage:** `AccountState.symbol_leverages` собирает плечи
  всех строк `positionRisk` (включая flat-символы); planner эмитит SET только
  при расхождении, hot path сделки свободен от REST-запросов плеча.
- [x] **`LeverageManager` (`trading/leverage.py`):** RAM control-plane с
  глобальным каталогом кандидатов (stale-while-revalidate) и
  account-confirmed состояниями, singleflight-подготовкой, fallback на
  подписанный bracket GET при отклонении биржей и фоновым worker'ом.
- [x] **UNKNOWN & Boot recovery:** `POST /leverage` unknown outcomes
  (`-1000/-1006/-1007`, 503/504) проверяются через `positionRisk` до fallback;
  boot recovery для `SET_LEVERAGE` без coid восстанавливает исход через
  `positionRisk` (ambiguous/unreadable → fail-closed in-flight).
- [x] **Signed live coverage & offline gate:** 4 signed live теста
  `tests/testnet/test_leverage_control_plane_live.py` (отклонение -4028,
  unready block, execution block, downshift); полный offline gate:
  **666 passed / 2 skipped / 21 deselected**, coverage **87.29%** (87%).

Stage 2 полностью закрыт для testnet scope.
### Этап 3 — Builder (Aster Code)

- Онбординг через ApproveAgent + ApproveBuilder вместо прямого ввода ключей; signer-ы создаёт и хранит билдер.
- Монетизация: builder fee (кап 0.001 futures).
- Торговое ядро и стрим не меняются — меняется только источник кредов.

#### UI/UX preparation Mini App (2026-08-26)

Дизайн-анализ и восемь screen concepts зафиксированы в
[STAGE3_MINI_APP_UX_PLAN_2026-08.md](reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md).
Этот gate не является началом frontend/backend реализации.

- [x] Изучить product truth, roadmap, architecture, Stage 2 UX/readiness и
  Builder/Aster Code research.
- [x] Зафиксировать follower Mini App IA, visual direction, экраны, состояния,
  safety boundaries и accessibility floor.
- [x] Сгенерировать и проанализировать отдельные Aster-style concepts для
  Builder auth, Home, Positions, Activity, Risk, Settings, Account/Builder и
  System Status.
- [x] Зафиксировать недостающие contracts: Builder onboarding/session,
  read-only positions, risk constraints и Builder attestation в status.
- [x] Утвердить wallet provider/chain scope, signature plan, фактические
  Builder metadata/fee, risk limits и disconnect/revoke semantics (реестр
  18 решений владельца — `STAGE3_BUILDER_MINI_APP_PART1_2026-08.md` §2).
- [x] Спроектировать технический Stage 3 implementation/acceptance plan
  (`STAGE3_BUILDER_MINI_APP_PART1_2026-08.md`, WP0–WP5).
- [x] Реализовать `onboarding/builder`, Mini App presenter/transport и signed
  testnet acceptance; торговое ядро не переписывать.
  - [x] WP1 `onboarding/builder` (e050965); WP2 application use cases (747c6f5);
    WP3 aiohttp-транспорт (747c6f5 + 72452cf: TTL `auth_date`, home-эндпоинты,
    dev-сессия, авто WebApp-кнопка); WP4 фронтенд (gate → wizard → success →
    минимальный home; injected+WalletConnect-шов) + vitest; интеграция
    полного flow через MockTransport + фикс булевых в signed query.
  - [x] Signed testnet e2e части 1 — **3 passed (живой тестнет)**: билдер
    зарегистрирован владельцем через Builder Center, faucet ASTER на perps;
    полный онбординг с подписью кошелька → paused-commit → сделка
    `builder+feeRate` → flat → feeRate выше капа отклонён
    (`test_builder_mini_app_e2e.py`).
  - [x] Ручная приёмка владельцем из Telegram: визар (WalletConnect →
    TokenPocket → подпись) → success → home; Возобновить/Пауза работают
    вживую (`8ce79f1`, `allow_new_entries=1`). Fee-стейтмент `/builder/userTrades`
    и полный 4-табовый shell — часть 2.

#### Corrective gate Stage 3 Part 1 после независимого review (2026-08-31)

Статус части 1: **CLOSED (2026-09-01)**. Кодовые пункты R1–R9 финального
аудита реализованы, полный signed Testnet suite повторён успешно, а владелец
подтвердил current-HEAD Pause/Resume в Telegram Mini App. Актуальные counters
и live evidence находятся только в `ENGINE_TEST_STATUS_2026-08.md`; план и
аудит:
[STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md](reports/STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md),
[STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md](reports/STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md).

- [x] Durable/idempotent Builder attempt lifecycle с encrypted signer recovery (`builder_onboarding_attempts`, 4 состояния: `PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`).
- [x] Recovery remote-success/local-failure и committed/runtime activation (`miniapp_recover_runtime`, session connection state: `empty | onboarding | recovery_required | connected`).
- [x] Исправить risk percent (масштабирование ×100 во frontend formatter), fresh dashboard (`refresh=True`, реальный `snapshot_captured_at`) и fail-closed confirmed Resume (всегда требует confirmation token, запрет при не-READY).
- [x] Исправить signed fee-cap proof (`test_exchange_rejects_fee_rate_above_approved_cap`: валидный order shape, `positionSide` в HEDGE, feeRate 0.0005 > 0.0001 cap, assertion на биржевое отклонение и flat verification).
- [x] Закрыть обязательные frontend session/config/copy gaps; timeout 15s с `AbortController`, безопасная клиентская диагностика, валидация конфигурации, базовые security-заголовки.
- [x] Довести UI fidelity, progress rail connector lines, inline SVG иконки вместо emoji, `:focus-visible`, доступность `Sheet` (`role="dialog"`, `aria-modal`, `aria-labelledby`, auto-focus).
- [x] Исправить ambiguous `approveAgent` outcome: сохранить `APPROVAL_SENT`,
  восстанавливать через signed GET и не повторять POST вслепую.
- [x] Добавить signatureless recovery только для server-owned
  `APPROVAL_SENT`/`APPROVED`, реально использовать `COMMITTED`, санировать логи,
  исправить initial Home freshness и modal Sheet.
- [x] Заменить псевдо-live L3/L7/L8 настоящими Testnet/HTTP сценариями и
  усилить signed read/order outcome/fee-cap helpers.
- [x] Пройти полный offline/frontend gate и записать фактический красный
  Testnet-прогон в living docs без fake/skip-подмены.
- [x] Повторить полный `tests/testnet/` после восстановления signed account
  endpoints Aster и получить зелёный результат.
- [x] Выполнить ручную Telegram + WalletConnect/TokenPocket приёмку и получить
  current-HEAD evidence состояний из R9.

#### Stage 3 Part 2 — оставшийся follower Mini App (план 2026-09-01)

Отдельного исполнимого Part 2 до этого не существовало: общий design-only
документ описывал восемь экранов, но не учитывал уже закрытый Part 1 и
имеющиеся application use cases. Адаптированный scope, точные изменения по
файлам, live Testnet gates и anti-overengineering boundaries зафиксированы в
[STAGE3_MINI_APP_PART2_ADAPTED_PLAN_2026-09.md](reports/STAGE3_MINI_APP_PART2_ADAPTED_PLAN_2026-09.md).

- [x] P2.0: app shell с четырьмя root tabs и завершённый Home (2026-09-01;
  implementation и automated browser acceptance завершены на полном engine
  boot с реальными testnet-данными; owner walkthrough в Telegram WebView
  закрыт 2026-09-02 на current HEAD; журнал:
  [STAGE3_PART2_PROGRESS_2026-09.md](reports/STAGE3_PART2_PROGRESS_2026-09.md)).
- [x] P2.1: read-only Positions из authoritative follower state (закрыт 2026-09-02; DTO, API, PositionsScreen, live acceptance 8/8 фаз через реальный production copy engine на Aster Testnet, corrective freshness/protection gates и owner Telegram WebView screenshot реальной позиции на Builder-follower закрыты).
- [x] P2.2: безопасная Activity timeline без внутренних payload/secrets (закрыт 2026-09-02; safe DTO `category/title/status/symbol/occurred_at` + optional `pending_age`/`remediation`, `POST /miniapp/api/activity` с Telegram initData boundary, `ActivityScreen` по `04-activity.png` с фильтрами Все/Сделки/Система, server-side category predicate до `LIMIT/OFFSET`, стабильной пагинацией и read-only состояниями; targeted unit + HTTP + frontend tests, live acceptance на Aster Testnet через production copy engine — open / Stop / close / partial close и реальный `SET_LEVERAGE` отражены в Mini App с safe DTO shape и непустыми trade/system filters; owner Telegram WebView screenshot safe Open/Stop timeline на Builder-follower закрыт; live evidence — `tests/testnet/test_live_activity_acceptance.py`; журнал — `STAGE3_PART2_PROGRESS_2026-09.md`).
- [x] P2.3: Settings root и существующие Notifications use cases
  (закрыт 2026-09-02: implementation/live complete и owner acceptance в
  реальном Telegram WebView на commit `fe84d2c`;
  `POST /miniapp/api/notifications` + `/notifications/enabled` поверх
  существующих application use cases со строгим boolean на HTTP boundary;
  SettingsScreen по `06-settings.png` — capability-список с секциями,
  chevron только у реально открывающихся строк, нейтральный Builder до
  attestation; вложенный NotificationsScreen с role=switch,
  серверным DTO как источником UI и блокировкой double-tap; live
  acceptance на Aster Testnet: реальный trade скопирован при
  отключённых уведомлениях без пользовательской доставки, после
  включения — durable outbox row доставлена настоящим Telegram Bot API
  до SENT; corrective pass 2026-09-02: live-гейт на throwaway-копии БД
  (`sqlite3.backup`, прод-БД нетронута), синхронный `createToggleGate`
  double-tap lock с regression-тестом и browser-проверкой (ровно 1 POST
  на два клика), «Всё работает» только при ready + snapshot_captured_at
  («Ожидание данных» без snapshot); журнал —
  `STAGE3_PART2_PROGRESS_2026-09.md`).
- [x] P2.4: единственная числовая настройка Risk on Stop с backend constraints
  (закрыт 2026-09-03: implementation/live complete; policy утверждена
  владельцем: min 0,5% / max 5% / step 0,5% / presets 1/2/3/5%;
  `POST /miniapp/api/risk` + `/risk/percent`, RiskScreen по `05-risk-settings`,
  live acceptance на Aster Testnet через production path на `sqlite3.backup`
  копии БД — follower qty 0.004 @1% и 0.012 @3% точно по Decimal-расчёту,
  прод-БД нетронута; owner Telegram acceptance закрыт 2026-09-03 (боевой бот
  + туннель, проверка на iPhone: линейка, fish-eye, края, тост); external
  review corrective: legacy off-grid proof + banner, ruler freeze mid-save,
  4 behavioral + 3 `nearestRiskStep` теста; журнал —
  `STAGE3_PART2_PROGRESS_2026-09.md`).
- [x] P2.5: System Status с typed remediation и сохранением stale snapshot
  (ACCEPTED / CLOSED 2026-09-03: implementation/live/review/owner
  acceptance complete; `POST /miniapp/api/status`, SystemStatusScreen,
  Telegram presenter на новом контракте; журнал —
  `STAGE3_PART2_PROGRESS_2026-09.md`).
- [x] P2.6: Account + Builder attestation, disconnect и reauthorize lifecycle
  (IMPLEMENTED 2026-09-03, ждёт review + owner acceptance: persistent
  `identity_source` + Builder snapshot, authoritative signed Agent+Builder
  verify, `AccountBuilderDTO`, same-signer reauthorization, local-only
  disconnect, единый trust source Account/Home/Status; live gate complete —
  `source=builder state=verified`; журнал —
  `STAGE3_PART2_PROGRESS_2026-09.md`).
- [ ] P2.7: account-specific `builder + feeRate` в обычном copy-engine и
  финальное закрытие Stage 3.

Новые бизнес-функции принимаются по настоящему Aster Futures Testnet через
production application/aiohttp path. Fake/fixtures допустимы для чистых
formatters, navigation, DOM/layout и security regression, но не заменяют live
биржевое доказательство.

**Исторический статус (2026-08-22): этап 1 выполнен и проверен на testnet.**
Реализовано всё перечисленное выше; детали и журнал полной проверки движка
(12 сценариев: long/short/тейк/стоп/переносы/мультипозиции/казусы/лимитный вход,
биржевая сверка каждого ордера по clientOrderId) — в
`ENGINE_TEST_STATUS_2026-08.md`. Найденные живым прогоном баги исправлены
(фантомное переоткрытие позиции из-за направления филла, двойной учёт
ACCOUNT_UPDATE, отсутствие копирования TP, STALE-guard против гонок,
лимитные флейворы STOP/TAKE_PROFIT).
Builder-e2e (approveBuilder ждёт `main private key` в wallet.txt) оставлен для
Stage 3; зеркало висящих лимиток мастера — кандидат в этап 2.

Критерий перехода: этап 1 закрыт, когда проходят все signed-сценарии раздела «Как проводить тесты»; этап 2 — когда онбординг и уведомления работают на тестнете с ключами, введёнными через бота.

### Corrective gate после независимого аудита (2026-08-23)

Corrective gate завершён 2026-08-23: найденные P0/P1 исправлены и доказаны
offline, signed testnet, load и двухчасовым soak. Stage 1 снова `CLOSED`,
переход к Stage 2 разрешён; mainnet в этот verdict не входит.

Полный обязательный scope, реальные testnet-сценарии, load/soak matrix, стек и
Definition of Done вынесены в
[STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md](reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md).

- [x] Quantity/`closePosition`-aware Stop/TP convergence и строгие Fake/Shadow adapters.
- [x] Повторяемый emergency с persisted pause, operation generation и REST verification.
- [x] Fail-closed boot при любом неразрешённом `SENDING/SUBMITTED/UNKNOWN`.
- [x] Heartbeat READY/degraded lifecycle и второй snapshot после WS handshake.
- [x] Полный offline, signed ONE_WAY/HEDGE, emergency/recovery и hybrid/load gate.
- [x] Двухчасовой SOAK-01 и завершающий независимый REST/SQLite audit.
- [x] Новый независимый verdict `Stage 1 = CLOSED`.

### Финальный Stage 1 gate — follow-up по `review_stage1_new.md` (2026-08-22)

- [x] Safety sequencing: при неуспешном `PLACE_STOP/PLACE_TP` не выполнять
  зависимый `CANCEL_ORDER` старой защиты.
- [x] Reconciliation generation: при неизменном target, но свежем
  `actual != target`, не переиспользовать исторический `CONFIRMED` command.
- [x] HEDGE REST recovery: убрать дубликат `_pick_protective` и покрыть защиту
  `positionSide=LONG/SHORT` без `reduceOnly/closePosition`.
- [x] Fresh market reference: перед stop/deviation decisions обновлять mark
  price затронутых master symbols через authoritative REST `positionRisk`.
- [x] Liquidation recovery: `MARGIN_CALL`, `CALCULATED`, autoclose/ADL и
  неизвестные position-changing execution types запускают немедленный
  authoritative master resnapshot и reconciliation.
- [x] Nonce/margin hardening: атомарно резервировать nonce в SQLite и проверять
  entry margin по фактически выбранному leverage.
- [x] Signed proof: выполнить и записать полноценный ONE_WAY цикл на testnet с
  локальными кошельками, включая cleanup; mainnet не используется.

Builder signed e2e не является блокером торгового ядра Stage 1 и остаётся в
контуре Stage 3, где реализуется Aster Code onboarding.

### Финальный hybrid load gate — 30 follower slots (2026-08-23)

- [x] Добавлен явный `execution_mode=REAL|SHADOW`; shadow rows не получают
  credentials и разрешены только на testnet при opt-in.
- [x] Production `App`/coordinator/planner/executor/journal прогнаны на 30
  follower slots: 3 signed REAL testnet followers + 27 SHADOW.
- [x] Выполнено 100 циклов open → protection → close; финал — 12 302/12 302
  `CONFIRMED`, ingest drops 0, cleanup и REST audit flat/no-orders.
- [x] SQLite write path усилен autocommit, cursor lifecycle и общим write lock;
  nonce allocation атомарен на том же aiosqlite connection.
- [x] Полный suite: 247 passed / 11 skipped, coverage 87%, ruff и diff-check
  clean. (Счётчик той кампании; актуальный — в блоке третьего прохода ниже.)

Подробности, включая неуспешные промежуточные прогоны и ограничения shadow
evidence: [HYBRID_LOAD_TEST_REPORT_2026-08.md](reports/HYBRID_LOAD_TEST_REPORT_2026-08.md)
и [HYBRID_LOAD_TEST_JOURNAL_2026-08.md](reports/HYBRID_LOAD_TEST_JOURNAL_2026-08.md).
27 shadow slots подтверждают масштаб внутреннего pipeline, но не заменяют 27
независимых signed Aster accounts для 30-real acceptance.

### Финальный gate по внешнему код-ревью (2026-08-23)

Два пункта последнего независимого ревью (см. `REVIEW_STAGE1_FINAL_2026-08.md`):

- [x] **P1 — защитная конвергенция.** Инвариант «одна нога = один bot-owned
  Stop + один TP»: снапшот координатора отдаёт дубликатные условные ордера
  слота (`extra_stops`/`extra_tps`), planner-правило 9 всегда их отменяет
  (приоритет 1, без зависимостей → гарантированный ретрай), включая flat-ветку.
  Попутно закрыт латентный баг: ретрай `CANCEL_ORDER`/`SET_LEVERAGE` после
  FAILED падал на `UNIQUE(command_key)` и физически не мог повториться.
- [x] **ONE_WAY E2E через ядро.** Новый signed-сценарий
  `tests/testnet/test_one_way_engine_e2e.py`: master open → stop → stop move →
  partial close → TP → close через Projector→Coordinator→Risk→Planner→Executor→
  journal→REAL follower (`copyc1`), с аудитом журнала (0 FAILED/UNKNOWN/BLOCKED,
  `reduceOnly=true`, без `positionSide`) и восстановлением режима.
- [x] Полный офлайн-suite на момент того прохода: 256 passed / 12 skipped,
  coverage 87%; ruff check/format clean. (Текущий счётчик — см. ниже,
  третий проход.)

### Dust zero-target edge case — второй проход ревью (2026-08-23)

- [x] **Dust-сокращение до нуля больше не переоткрывает позицию.**
  `sync_basis.zero_target` (миграция #12) персистит намеренный флэт внутри
  живого эпизода мастера; входная ветка skip'ает `INTENTIONAL_FLAT`; маркер
  снимают новый эпизод мастера и `mark_zero_target` (со сбросом
  `issued_target` против глотания идемпотентностью).
- [x] Контрольный тест: ненулевой таргет + ручное закрытие follower'а →
  drift-recovery по-прежнему восстанавливает позицию.
- [x] Live dust-фаза в signed ONE_WAY E2E (phase 4b).

### Третий проход ревью: flip-регрессия + честное dust-evidence (2026-08-23)

Ревьюер вскрыл: (1) фикс №14 клеймил ЛЮБОЙ ноль при живом мастере —
транзитный ноль flip'а блокировал открытие обратной стороны навсегда;
(2) dust-фаза signed E2E молча пропускалась (`0.003*0.005 < step`) и
передавала остаток вместо fill-qty — «live proof» ничего не доказывал.

- [x] **`TargetPosition.hold_flat_until_episode_end`** — явная причина нулевого
  таргета; sticky только для same-direction редукции в ноль, flip-ноль
  транзитный; флаг в `_canonical_target` (разные command keys) (`79be6b1`).
- [x] **Episode-семантика флипа**: flip = close старого episode + open нового с
  `direction=±1`; миграция #13 `copy_trades.direction`; snapshot-синк детектит
  смену знака после офлайна; новый эпизод снимает sticky-маркеры (`8ea3dbd`).
- [x] Офлайн `TestMasterFlipEpisodeSemantics`: plain flip + «dust-flat → flip».
- [x] Signed E2E: dust-фаза ОБЯЗАТЕЛЬНА (assert вместо skip; первый прогон
  честно упал на новом assert'е), остаток мастера = ровно один step,
  `rebuild_full` вместо синтетики, явный recompute_follower в харнессе,
  фаза 4c — реальный signed flip LONG→SHORT через полное ядро (`7122a28`).
- [x] Прогон после фиксов: офлайн 258 passed / 1 skipped (87%); signed testnet
  10 passed / 1 skipped (E2E PASSED 155 c); hybrid gate 100/100 циклов ×
  30 слотов — 12207/12207 CONFIRMED, 0 не-ConFIRMED статусов, 31/31 flat.


## 5. MVP vs Продакшн

### MVP (проверить весь бэкенд)

- Telegram-бот: подключение ключей (прямой ввод V3-набора), настройка риска (% депозита на стоп), уведомления.
- Слежение за мастером через user-data WS (события ордеров/позиций/баланса).
- Исполнение на аккаунтах фолловеров: открытие, закрытие, частичное закрытие, SL/TP, плечо.
- Расчёт размера позиции от риска на стоп; риск-ревард.
- Хранение: SQLite (WAL) + зашифрованное хранилище ключей.
- Вся работа на тестнете, затем малый реальный объём.

### Продакшн (обвязка)

- Онбординг через Aster Code / Builder: мини-апп/страница с кошельком, подпись ApproveAgent+ApproveBuilder.
- Монетизация: builder fee, вывод комиссий (Builder Center) — при доступности на mainnet.
- Sub-accounts для изоляции copy-средств.
- PostgreSQL, бэкапы, мониторинг, rate-limit менеджмент, авто-переподключение со snapshot-recovery.
- Масштаб: лимиты весовых rate limits Астера → пулы/очереди команд.

## 6. Как проводить тесты

### Принцип

Тесты делятся на: (а) без подписи — только проверка маршрутов (400/404), (б) с подписью — полный сценарий на тестнете. Всё гоняется на futures testnet с faucet-средствами, без реальных денег.

### Базовый signed testnet-план (из §8 ресерча, ключевые пункты)

1. Создать master и follower agents (canPerpTrade=true, canWithdraw=false).
2. Прочитать balance/position/open-orders обоих.
3. Открыть master MARKET → скопировать фолловеру MARKET.
4. Проверить фактический fill фолловера и расхождение риска.
5. Частичное закрытие 25/30% (One-way reduceOnly; Hedge — positionSide).
6. SL/TP: создать STOP_MARKET/TAKE_PROFIT_MARKET, переставить cancel+new.
7. OTOCO: placeStrategyOrder → updateStrategyOrder → ручная смена в UI Астера, записать WS payloads.
8. listenKey 60 мин, принудительный disconnect, restore snapshot.
9. Идемпотентность повторов, нехватка маржи, minQty/minNotional, leverage brackets.
10. Builder end-to-end: approveAgent+builder одной подписью → order с feeRate → builderFee в userTrades.

### Критерий прохождения

После каждого сценария фактические позиции и защитные ордера фолловера совпадают с рассчитанным target state; повторная reconciliation не создаёт лишних команд.

## Открытые вопросы (не блокеры)

- Поведение user-data stream при ручной перестановке Strategy OCO/OTOCO в UI Астера — подтверждается signed testnet-тестом.
- Builder-веб-интерфейс на тестнете отсутствует (только API-маршруты) — для теста онбординга нужен свой мини-апп или скрипт.
- Финальные лимиты rate limits на тестнете могут отличаться от mainnet (sentinel -2) — смотреть mainnet exchangeInfo перед продом.

---

Источники: [ASTER_API_RESEARCH_2026-08.md](ASTER_API_RESEARCH_2026-08.md) — status 20–21.08.2026; [REPORTS_FACTUAL_COMPARISON_2026-08.md](reports/REPORTS_FACTUAL_COMPARISON_2026-08.md).
