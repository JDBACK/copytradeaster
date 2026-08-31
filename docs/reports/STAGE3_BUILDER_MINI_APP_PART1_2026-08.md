# Stage 3, часть 1 — Builder-auth Mini App: план реализации и журнал

Дата старта: 2026-08-31

Статус: **часть 1 закрыта 2026-08-31** — реализация, signed e2e (3 passed,
живой тестнет) и ручная приёмка владельцем из Telegram (WalletConnect →
TokenPocket → подпись → paused-commit → home). Design-этап закрыт в
`STAGE3_MINI_APP_UX_PLAN_2026-08.md`. Гайд для ревьюера:
`STAGE3_PART1_REVIEW_GUIDE_2026-08.md`. Открытый пункт приёмки: resume
билдер-фолловера из мини-апа возвращает service_unavailable (диагностика
в логе; direct-путь в боте не затронут) — разбирается отдельно.

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

### 2026-08-31 (ручная приёмка владельцем — принята)

- Reown project ID получен (free tier), WalletConnect активирован
  (`VITE_WC_PROJECT_ID`, git-ignored local env);
- fixes по ходу приёмки: дубль кнопки подключения; клиентская диагностика
  `POST /miniapp/api/client-log` (ошибки фронта падают в лог оператора с
  кодом и причиной); текст `wallet_unavailable`; код ошибки виден в боксе;
- WC-адаптер: явный `provider.connect()` (иначе «Please call connect()
  before request()»); модалка выбора кошельков открывается в Telegram;
- `.env`: прописан `CT_BUILDER_ADDRESS` (бот валидировал конфиг до этого);
- **РУЧНАЯ ПРИЁМКА ИЗ TELEGRAM ПРОЙДЕНА владельцем**: второй аккаунт
  (direct из stage 2) отключён в боте → визар из Telegram → WalletConnect →
  TokenPocket → подпись approveAgent → verify+preflight → commit paused →
  success → home. Прод-БД: `tg-8264892202` user=0x3c45…F975 (кошелёк
  владельца), signer=0xa701…41Ce (сгенерирован билдером), chain 714, paused.
- **ЧАСТЬ 1 ЗАКРЫТА** (автоматика + ручная приёмка). Известные отложенные
  пункты: fee-стейтмент `/builder/userTrades` (недокументированный auth) и
  `identity_source`+builder-снапшот миграция — часть 2.

### 2026-08-31 (третья сессия — WP5, живой тестнет)

- владелец передал основной evm-ключ копика (совпал с copyc2) — e2e
  переключён на copyc2; парсер wallet.txt выбирает ключ, восстанавливающийся
  в evm-адрес секции (исторические `privte key` — ключи API-кошельков);
- **живой тестнет, итеративно:**
  - подпись main-кошелька валидна, submit дошёл до биржи;
  - первый блокер: `Builder address … not registered in Aster` — продуктовая
    регистрация билдера, API-ручки нет (POST /builder → 405), оба официальных
    демо берут билдера «как данность»; тестнет-вкладки Aster Code в SPA
    тестнета нет (подтверждено вскрытием бандла);
  - реестр общий с мейннетом: зонд с демо-билдером `ivan` дал ошибку
    **баланса**, не регистрации → владелец зарегистрировал наш адрес через
    Builder Center (asterdex.com/en/aster-code) в своём браузере;
  - второй блокер: `has no ASTER balance` — требование ≥100 ASTER на perps
    билдера (проверка per-environment; тестнет-faucet закрывает);
  - после faucet владельца (perps билдера ≈ $1,695) — блокеры сняты;
- **SIGNED E2E ЗЕЛЁНЫЙ (3 passed, живой тестнет):** полный builder-онбординг
  (подпись кошельком copyc2 → approveAgent с builder-полями → signed verify
  → preflight → paused-commit → активация), сделка `builder+feeRate=0.0001`
  исполнена биржей (HEDGE: BUY LONG → SELL LONG, flat подтверждён),
  `feeRate=0.5` отклонён биржей (кап работает);
- `/builder/userTrades` — недокументированный auth («No agent found» при
  подписи билдер-идентичностью); fee-стейтмент отложен на часть 2 (Builder
  Center), сделочный уровень доказан;
- попутно доказано живьём: чистый `approveAgent` → 200 success (пайплайн
  подписи), у copyc2 есть живой Agent с builder-привязкой (истекает через
  30 дней, production-БД не затронута);
- offline gate: **714 passed / 2 skipped**, ruff clean; signed gate части 1:
  **3 passed**; vitest 32; мини-ап поднят из бота владельцем (кнопка ASTER
  COPY в чате, home на реальных данных);
- фикс бот-кнопки: per-chat override в админ-чате перекрывал дефолт —
  runtime ставит WebApp-кнопку и в админ-чаты явно;
- скрипт `scripts/preview_miniapp.py` — превью визара без Telegram.

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
