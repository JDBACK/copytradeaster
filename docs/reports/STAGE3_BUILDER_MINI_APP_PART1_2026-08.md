# Stage 3, часть 1 — Builder-auth Mini App: план реализации и журнал

Дата старта: 2026-08-31

Статус: **реализация начата** (design-этап закрыт в
`STAGE3_MINI_APP_UX_PLAN_2026-08.md`)

Среда: только Aster Futures testnet (`fapi.asterdex-testnet.com`)

## 1. Границы части 1

Часть 1 даёт фолловеру вход в копик через Mini App с аутентификацией по
Builder:

1. Telegram WebApp-сессия: server-side проверка подписанного initData,
   access-gate (pending/rejected/approved) read-only с возвратом в бота;
2. Builder onboarding по эталону `01-builder-auth.png`:
   подключение кошелька → review разрешений и fee → одна combined-подпись
   `ApproveAgent` (с builder-полями) основным кошельком пользователя →
   server-side submit → signed verify `GET /fapi/v3/agent` + preflight
   (balance/positionRisk/openOrders/position mode) → атомарный commit
   строго paused → success;
3. Минимальный home от эталона: подключено, маскированный кошелёк,
   копирование на паузе, pause/resume существующими use cases.

В часть 1 НЕ входят: полный 4-табовый shell, Позиции/Активность/Настройки,
read-only `PositionPageDTO`, risk constraints, revoke/disconnect-семантика
замены, экран Account+Builder, `builder+feeRate` в регулярной торговле
(монетизационная логика), mainnet.

## 2. Реестр решений (утверждено владельцем 2026-08-31)

Волна 2 (уточнения перед WP4, 2026-08-31):

| # | Решение | Значение |
|---|---|---|
| 19 | WalletConnect | адаптер и ленивый чанк в WP4 сразу; активация `VITE_WC_PROJECT_ID`, project ID заводит владелец к приёмке из Telegram (десктопный webview тоже без инжекта) |
| 20 | Dev-сессия | `CT_MINIAPP_DEV_UNSIGNED` + `CT_MINIAPP_DEV_USER_ID`, fail-closed: только testnet REST base, только явные оба поля |
| 21 | Тесты фронта | минимальный vitest (api-client, wizard reducer, home view-model, wallet-адаптер, формат); компоненты принимаются вручную |
| 22 | Пуш ветки | держать локально до конца части 1 |
| 23 | WebApp-кнопка | бот сам ставит `setChatMenuButton` из `CT_MINIAPP_PUBLIC_URL` при старте |

| # | Решение | Значение |
|---|---|---|
| 1 | Хостинг мини-апа | dev: cloudflared quick-tunnel + авто-обновление WebApp-кнопки через Bot API; далее VPS + nginx + certbot |
| 2 | Кошелёк во фронтенде | dev/приёмка на Mac: инжект-провайдер (`window.ethereum`, EIP-1193) в браузере; мобильный Telegram — WalletConnect/Reown к моменту mobile-приёмки (project ID бесплатный, заводит владелец) |
| 3 | Signed e2e | автоматический прогон подписывается ключами из `wallet.txt`; ручная owner-приёмка — отдельным кошельком |
| 4 | Проверочная сделка | signed e2e части 1 включает один цикл с `builder+feeRate` (снимает риск «подключён, но не торгует» и исторический Builder-e2e Stage 1) |
| 5 | Builder address | отдельный новый адрес, секция `builder` в `wallet.txt` (не мастер) |
| 6 | `maxFeeRate` | дефолт `0.0001`; UI рисует значение из бэкенда; перед mainnet сверить с фактическим соглашением |
| 7 | Agent expiry | 30 дней; дата окончания видна на success-экране |
| 8 | Signature plan | combined (builder-поля внутри `approveAgent`), `signature_plan` остаётся в DTO |
| 9 | ipWhitelist | dev на Mac: пустой whitelist + `CT_TESTNET_IP_WHITELIST_OVERRIDE=true` (как direct-путь); на VPS — реальный egress IP |
| 10 | Scope success | чистый минимум от эталона, без 4-табового shell |
| 11 | Existing direct-аккаунт | блокировать: typed `OWNER_CONFLICT` + remediation «отключите аккаунт в боте»; полный replace/revoke — часть 2 (§12.6 UX-плана) |
| 12 | Direct-путь | оба пути живут параллельно, пока builder не будет fully working |
| 13 | WebApp-кнопка | в главном меню бота + в карточке после approval |
| 14 | Стек фронтенда | Vite + React + TypeScript, кастомные CSS-токены из §5 UX-плана, без UI-кита |
| 15 | Сессия | stateless: HMAC-проверка initData на каждом запросе, TTL по `auth_date` |
| 16 | TTL prepare→submit | 10 минут; истёкшая заявка — typed `APPROVAL_EXPIRED` |
| 17 | Схема БД | часть 1 без миграций; `identity_source` + builder-снапшот — миграция части 2 |
| 18 | Rate limiting | не делаем; гигиена — размер тела/таймауты |

## 3. Факты перепроверки официальной документации (2026-08-31)

Перечитаны `asterCode/integration-flow`, `asterCode/authentication` и
исходники `asterdex/API-demo/aster-code-demo` (utils.js, 01_approveAgent.js):

- registration-домен `AsterSignTransaction` v1 c **chainId 56 захардкожен**
  в демо независимо от среды; среда передаётся полем `asterChain: 'Testnet'`;
  `signatureChainId=56` добавляется после подписи и не входит в подписываемый
  набор — совпадает с нашим `aster/signing.py::sign_registration`;
- combined-approve подтверждён: `builder`, `maxFeeRate`, `builderName` входят
  в параметры `approveAgent` одной подписью;
- пустой `ipWhitelist` в демо опускается из запроса — бэкенд повторяет это;
- порядок полей подписи: business params + `asterChain` + `user` + `nonce`,
  ключи Capitalized, типы `bool`/`uint256`/`string` — наш код уже 1:1;
- `builder + feeRate` объявлены частью официального order-flow; отклоняется
  ли ордер без них — не документировано, поэтому проверочная сделка (реш. 4)
  обязательна;
- способ подключения кошелька (extension/WalletConnect) Aster не
  регламентирует — выбор за фронтендом (реш. 2).

## 4. Артефакты части 1

- Builder-адрес создан 2026-08-31: `0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8`
  (ключ в локальном `wallet.txt`, git-ignored). Владельцу: пролить faucet
  (1000 test USDT + 1000 test ASTER в сутки) на этот адрес в режиме Futures.

## 5. Рабочие пакеты

- WP0 — реестр решений, builder-адрес, конфиг (этот отчёт); ✅
- WP1 — `onboarding/builder`: pending-заявка с TTL, генерация signer,
  EIP-712 typed-data для кошелька, ecrecover-верификация, submit в Aster,
  signed verify + preflight, атомарный paused-commit, typed ошибки; ✅ (e050965)
- WP2 — application: `MiniAppSessionDTO`, `BuilderOnboardingDTO`, use cases
  `miniapp_session`, `builder_prepare`, `builder_submit` на общем
  `UserApplicationService`; ✅ (747c6f5)
- WP3 — aiohttp-транспорт в том же процессе: static + `/miniapp/api/*`
  (initData на каждом запросе), dev-обвязка cloudflared; ✅ (747c6f5,
  72452cf: TTL auth_date, home-эндпоинты, dev-сессия, авто-кнопка)
- WP4 — фронтенд Vite+React+TS: session gate → builder wizard (trust rail
  Wallet → Agent → Builder → Sync) → минимальный home (paused + pause/resume);
  ✅ + vitest на чистые модули (32);
- WP5 — тесты: unit (recover/typed data/TTL/ошибки), интеграция FakeAster
  полного flow, signed testnet e2e с проверочной сделкой; обновление
  `DEV_PLAN_SKELETON.md`, `ARCHITECTURE.md`, `ENGINE_TEST_STATUS_2026-08.md`.
  Юнит + интеграция ✅; signed e2e написан (`test_builder_mini_app_e2e.py`),
  ожидает прогона `CT_TESTNET=1` и ручной приёмки владельца.

## 6. Журнал

### 2026-08-31 (вторая сессия — WP4/WP5)

- волна 2 ответена, решения #19–#23 зафиксированы (§2);
- К1 бэкенд: `auth_date` TTL 24ч (рекомендация доков Telegram; aiogram не
  проверяет сам), русский текст транспорта, JSON-энкодер Decimal/datetime
  (латентный 500 на реальных Decimal DTO), эндпоинты
  dashboard/pause/resume/confirmation begin+confirm, dev-сессия fail-closed,
  авто WebApp-кнопка (`register_commands`), валидатор https
  `CT_MINIAPP_PUBLIC_URL`; 25 юнит-тестов;
- К2 фронтенд: Vite `base:/miniapp/` + dev-proxy, telegram-web-app.js, токены
  §5, api-client с typed-ошибками, wallet-адаптер (injected + ленивый
  WalletConnect-чанк, BSC 56 switch/add-chain, typed wallet-ошибки),
  session gate → wizard (trust rail, review из prepare, одна combined
  подпись) → success → минимальный home (pause/resume + confirmation sheet);
  pnpm v11: `allowBuilds` вместо удалённого `onlyBuiltDependencies`;
- найдено и исправлено: aiohttp маршрут `/miniapp/` перехватывался статикой
  (403) — index-хендлеры регистрируются до `add_static`;
- К3: vitest 32 теста (node env) + oxlint 0 warnings, build зелёный;
- К4.1: интеграция полного flow (`test_miniapp_builder_flow.py`) через
  `httpx.MockTransport` — вскрыла баг: `post_signed_registration`
  сериализовал Python bool как `True` (демо/биржа ждут `true`);
  `stringify_param_value` теперь публичный и используется транспортом
  регистрации; также дифференцированы HTTP-статусы application-ошибок
  (409 confirmation_expired, 403 not_authorized, 503
  service_unavailable/not_ready, 400 — остальные typed);
- предсуществующий флак `test_two_equal_reductions_both_execute`
  зафиксирован (order-dependent, соло падает на чистом дереве) — вне
  скоупа части 1;
- offline gate: 714 passed / 2 skipped; vitest: 32 passed; pnpm build: ok.

### 2026-08-31

- волна уточнений владельцем отвечена; реестр решений §2 зафиксирован;
- перепроверены официальные доки Builder и исходники демо (§3);
- сгенерирован builder-адрес, добавлен в `wallet.txt` (§4);
- начало WP1.
