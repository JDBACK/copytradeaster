# ASTER COPY — Mini App

Telegram Mini App для Builder-авторизации, управления follower account и
просмотра состояния копитрейдинга через общий application layer.
Стек: Vite + React 19 + TypeScript, без UI-кита — кастомные токены из
`docs/reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md` §5.

## Экраны и live-поведение

1. **Session gate** — server-derived доступ: loading / pending / rejected / invalid.
2. **Builder wizard** — trust rail `Wallet → Agent → Builder → Sync`,
   review разрешений и fee до подписи, одна combined-подпись
   `approveAgent` (EIP-712, chainId 56) кошельком пользователя.
3. **Success** — подключено, копирование на паузе, срок авторизации.
4. **Root AppShell** — четыре вкладки: `Главная`, `Позиции`, `Активность`,
   `Настройки`; nested screens включают Risk, Notifications, System Status,
   Account Builder и историю сделок.

Позиции, PnL, история и статус получают resource invalidations через
аутентифицированный single-use-ticket WebSocket. При разрыве остаётся последний
подтверждённый снимок и показывается stale-rail; штатной refresh-кнопки нет,
единственный ручной путь — pull-to-refresh сверху shell.

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

# 2) Публичный HTTPS туннель (Telegram требует https):
#    Либо полный запуск всего стека: ./scripts/dev-up.sh
#    Либо ручной cloudflared: cloudflared tunnel --url http://127.0.0.1:8080
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

## Текущий scope

В production bundle входят полный 4-табовый shell, account lifecycle,
revoke/disconnect, `builder+feeRate` в регулярной торговле, account-owned
история и live presentation. Реализация и acceptance S1–S3/Part 2 описаны в
`docs/reports/STAGE3_PART2_PROGRESS_2026-09.md` и
`docs/reports/MINIAPP_LIVE_MODE_PLAN_2026-09.md`.
