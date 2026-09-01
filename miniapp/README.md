# ASTER COPY — Mini App (Stage 3, part 1)

Telegram Mini App для входа в копитрейд через Builder-авторизацию.
Стек: Vite + React 19 + TypeScript, без UI-кита — кастомные токены из
`docs/reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md` §5.

## Экраны (часть 1)

1. **Session gate** — server-derived доступ: loading / pending / rejected / invalid.
2. **Builder wizard** — trust rail `Wallet → Agent → Builder → Sync`,
   review разрешений и fee до подписи, одна combined-подпись
   `approveAgent` (EIP-712, chainId 56) кошельком пользователя.
3. **Success** — подключено, копирование на паузе, срок авторизации.
4. **Home (минимум)** — состояние, риск, открытые позиции, freshness,
   pause/resume (resume — с typed-подтверждением, когда мастер в позиции).

## Запуск (локальная разработка)

```bash
# 1) Бэкенд-процесс с ботом + aiohttp-транспортом (.env):
#    CT_MINIAPP_ENABLED=true
#    CT_MINIAPP_STATIC_DIR=miniapp/dist
#    CT_MINIAPP_PUBLIC_URL=https://<tunnel>.trycloudflare.com   # авто-кнопка
#    CT_MINIAPP_DEV_UNSIGNED=true          # только для браузерной разработки
#    CT_MINIAPP_DEV_USER_ID=<ваш telegram id>
pnpm install
pnpm build                                # соберёт dist/ для раздачи бэкендом

# 2) Публичный HTTPS (Telegram требует https):
../scripts/dev-miniapp-tunnel.sh 8080

# 3) Dev-сервер фронтенда с hot-reload (проксирует /miniapp/api на :8080):
VITE_DEV_USER_ID=<ваш telegram id> pnpm dev
```

Два режима проверки:

- **Браузер (Chrome + MetaMask)** — фронтенд `pnpm dev`, сервер с
  `CT_MINIAPP_DEV_UNSIGNED=true`: initData нет, сессия строится из
  `X-Dev-User-Id` (fail-closed, только testnet). Кошелёк подключается через
  `window.ethereum`.
- **Telegram** — `pnpm build` + туннель + `CT_MINIAPP_PUBLIC_URL`: бот сам
  ставит WebApp-кнопку меню. В webview Telegram провайдера нет — включается
  WalletConnect (см. ниже).

## WalletConnect (для приёмки из Telegram)

Telegram webview не инжектит `window.ethereum`, поэтому там подключение идёт
через WalletConnect (`@walletconnect/ethereum-provider`, чанк грузится лениво).
Для активации нужен бесплатный project ID (cloud.reown.com):

```bash
VITE_WC_PROJECT_ID=<project id> pnpm build
```

Без project ID кнопка WalletConnect не появляется — работает только injected
провайдер.

## Тесты и качество

```bash
pnpm test      # vitest: api-client, wizard reducer, home view-model, wallet adapter
pnpm lint      # oxlint
pnpm build     # tsc -b && vite build
```

## Границы части 1

Полный 4-табовый shell, Позиции/Активность/Настройки, revoke/disconnect,
`builder+feeRate` в регулярной торговле — часть 2 (см. реестр решений в
`docs/reports/STAGE3_BUILDER_MINI_APP_PART1_2026-08.md`).
