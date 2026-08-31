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

- WP0 — реестр решений, builder-адрес, конфиг (этот отчёт);
- WP1 — `onboarding/builder`: pending-заявка с TTL, генерация signer,
  EIP-712 typed-data для кошелька, ecrecover-верификация, submit в Aster,
  signed verify + preflight, атомарный paused-commit, typed ошибки;
- WP2 — application: `MiniAppSessionDTO`, `BuilderOnboardingDTO`, use cases
  `miniapp_session`, `builder_prepare`, `builder_submit` на общем
  `UserApplicationService`;
- WP3 — aiohttp-транспорт в том же процессе: static + `/miniapp/api/*`
  (initData на каждом запросе), dev-обвязка cloudflared;
- WP4 — фронтенд Vite+React+TS: session gate → builder wizard (trust rail
  Wallet → Agent → Builder → Sync) → минимальный home (paused + pause/resume);
- WP5 — тесты: unit (recover/typed data/TTL/ошибки), интеграция FakeAster
  полного flow, signed testnet e2e с проверочной сделкой; обновление
  `DEV_PLAN_SKELETON.md`, `ARCHITECTURE.md`, `ENGINE_TEST_STATUS_2026-08.md`.

## 6. Журнал

### 2026-08-31

- волна уточнений владельцем отвечена; реестр решений §2 зафиксирован;
- перепроверены официальные доки Builder и исходники демо (§3);
- сгенерирован builder-адрес, добавлен в `wallet.txt` (§4);
- начало WP1.
