# Stage 3, часть 1 — гайд для ревьюера (2026-08-31)

Ветка: `codex/stage-3-mini-app-part1`. Что это: вход фолловера в копитрейд
через Telegram Mini App с Builder-авторизацией — main-ключ пользователя не
входит в систему, билдер генерирует и хранит signer/Agent, первая сделка
требует одной combined-подписи `approveAgent` кошельком пользователя.

Среда: только Aster Futures **testnet**. Mainnet запрещён конструкцией
(fail-closed guards в `onboarding/builder.py::_validate_config` и
`Settings._validate_runtime_guards`).

## 1. Что принимать (scope части 1)

1. Telegram WebApp-сессия: server-side HMAC-проверка подписанного `initData`
   на каждом запросе + TTL по `auth_date` (24ч, `CT_MINIAPP_INIT_DATA_TTL_S`).
2. Access-gate (pending/rejected/approved) read-only.
3. Builder-онбординг: prepare (EIP-712 typed data) → подпись кошельком
   пользователя → ecrecover → submit на биржу → signed verify `GET /agent` +
   preflight (balance/positionRisk/openOrders/position mode) → атомарный
   paused-commit → runtime activation (register + leverage prepare).
4. Минимальный home: состояние, риск, позиции-count, freshness, pause
   (one-tap) и resume (typed confirmation, когда мастер в позиции).
5. Транспорт: dashboard/pause/resume/confirmation begin+confirm/client-log.
6. Wallet-адаптер: injected EIP-1193 (браузер) + WalletConnect
   (Telegram-вебвью), ленивый чанк за `VITE_WC_PROJECT_ID`.

НЕ в scope части 1 (задокументировано): 4-табовый shell, позиции-экран,
активность, настройки, revoke/replace, `builder+feeRate` в регулярной
торговле движка, миграции `identity_source`, `/builder/userTrades`
fee-стейтмент (недокументированный auth), mainnet.

## 2. Коммиты (хронология, ветка локальная)

| Коммит | Что |
|---|---|
| 9c0431c | UX-план + 8 эталонов (design gate) |
| e050965 | `onboarding/builder.py`: ядро Builder-авторизации |
| 747c6f5 | application use cases + aiohttp транспорт (WP2/WP3) |
| 52da849 | Vite-каркас, tunnel script, отчёт кампании, реестр решений |
| 72452cf | TTL initData, home-эндпоинты, dev-сессия, авто WebApp-кнопка, русский текст, Decimal-JSON |
| f902a44 | Фронтенд WP4: gate → wizard → success → home; WC-шов; pnpm v11 allowBuilds |
| a298c70 | vitest (32), фикс 403 на `/miniapp/` (порядок маршрутов) |
| 8e37f4b | **Фикс булевых в signed query** + интеграция полного flow |
| 4c1ab4a | Signed e2e (builder-flow + сделка `builder+feeRate`) |
| 6f4e70c | WebApp-кнопка per-admin-chat + preview-скрипт |
| 293ad06 | Живой зонд: пайплайн подписи валиден; e2e skip до регистрации билдера |
| a656ab6 | **Signed e2e GREEN (3 passed)** — полный flow + сделка на живом тестнете |
| c6e4b64 | Дубль кнопки, client-log канал, `wallet_unavailable` copy |
| 6b2b38d | WC `provider.connect()` + cause-chain в клиентских логах |
| 8625da2 | Доки: часть 1 ПРИНЯТА (ручная приёмка владельцем из Telegram) |

## 3. Инварианты безопасности — проверить в первую очередь

1. **Main-ключ не входит в систему.** `onboarding/builder.py`: prepare
   генерирует signer (`Account.create()`), submit принимает только подпись;
   ключи не логируются (`_PendingApproval.__repr__` — redacted; лог-вызовы
   без параметров-секретов). Проверить: grep по `private_key` в логах,
   `signer_private_key` используется только внутри submit/commit.
2. **Клиентская идентификация не доверяется.** `miniapp/server.py::_identity`:
   HMAC через `safe_parse_webapp_init_data`, TTL `auth_date`, user id
   серверно-производный. Client-log требует той же сессии.
3. **Fail-closed dev-режим.** `CT_MINIAPP_DEV_UNSIGNED` работает только при
   явном `CT_MINIAPP_DEV_USER_ID` + testnet REST host (проверяется и в
   Settings-валидаторе, и в `_dev_mode_allowed`). Без флага dev-заголовок
   игнорируется.
4. **Атомарный paused-commit.** `BuilderOnboardingService.submit` →
   `store.commit_onboarding` c `allow_new_entries=False`; активация ПОСЛЕ
   коммита; неудачная активация не откатывает коммит (recovery в
   `builder_prepare` при следующем заходе).
5. **Пользователь ≠ билдер ≠ мастер.** Конфликт-чеки `_check_conflict`;
   `master_address` guard.
6. **Privileged actions закрыты.** `begin_confirmation(EMERGENCY/GLOBAL_*)`
   для не-админа → 403 (тест `test_confirmation_begin_rejects_forbidden_action`).
7. **Cap комиссии.** Биржа отклоняет `feeRate` выше одобренного (signed e2e
   `test_exchange_rejects_fee_rate_above_approved_cap`).

## 4. Ключевые файлы для ревью

| Файл | Что смотреть |
|---|---|
| `src/copytradeaster/onboarding/builder.py` | весь жизненный цикл prepare→submit; typed errors; TTL; конфиг-гарды |
| `src/copytradeaster/aster/signing.py` | `eip712_json_payload`/`recover_registration`/`stringify_param_value` (bool → "true"/"false") |
| `src/copytradeaster/aster/management_api.py` | `post_signed_registration`: query из исходных имён, error field `msg`/`error` |
| `src/copytradeaster/miniapp/server.py` | `_identity` (HMAC+TTL+dev-bypass), эндпоинты, `_error_status` (403/409/400/503), JSON-энкодер Decimal/datetime, порядок index→static |
| `src/copytradeaster/application/service.py` (miniapp_session/builder_prepare/builder_submit/resume) | audit-журнал, recovery, confirmation flow |
| `src/copytradeaster/telegram/runtime.py` | `register_commands(web_app_url)`, `install_admin_webapp_buttons` |
| `miniapp/src/api.ts`, `src/errors.ts` | typed-коды, маппинг на русский, client-log |
| `miniapp/src/wallet.ts` | injected + WC адаптеры, chain 56 switch/add, `provider.connect()` |
| `miniapp/src/screens/*` | gate/wizard/success/home; отсутствие секретов в UI |
| `tests/integration/test_miniapp_builder_flow.py` | полный stack-тест (HMAC → приложение → сервис → MockTransport биржа → SQLite) |
| `tests/testnet/test_builder_mini_app_e2e.py` | живой тестнет: онбординг + сделка builder+feeRate + cap-reject |

## 5. Найденные проблемы и их решения (за сессию)

| # | Проблема | Решение |
|---|---|---|
| 1 | Китайские тексты ошибок в транспорте | русские сообщения |
| 2 | Отсутствие проверки свежести `auth_date` (рекомендация Telegram) | TTL 24ч, тесты stale/future |
| 3 | Латентный 500: `asdict` + `json_response` не умеет Decimal/datetime | `_json_default` encoder |
| 4 | Дубль кнопки «Подключить кошелёк» | убран внешний рендер |
| 5 | `GET /miniapp/` → 403 (aiohttp отдаёт ресурсы в порядке регистрации; static перехватывал index) | index-хендлеры до `add_static` + регресс-тест |
| 6 | Все ошибки приложения были 400 | 403 not_authorized, 409 confirmation_expired, 503 service_unavailable/not_ready, 400 typed |
| 7 | **`str(True)` → `"True"` в signed query** (демо шлёт `"true"`); биржа не парсит | `stringify_param_value` в `post_signed_registration`; поймано интеграцией |
| 8 | pnpm v11: `onlyBuiltDependencies` удалён | `pnpm-workspace.yaml` → `allowBuilds` |
| 9 | Module-scoped async fixture → зависание на чужом event loop | function-scoped + module-cache в e2e |
| 10 | `GET /miniapp/api/client-log` без сессии | 401 fail-closed (проверено) |
| 11 | WC: `eth_requestAccounts` до `provider.connect()` → "Please call connect() before request()" | явный `provider.connect()` (модалка выбора кошельков) |
| 12 | `builder_config_missing` в боте | `CT_BUILDER_ADDRESS` в `.env` |
| 13 | WebApp-кнопка перекрывалась per-chat override в админ-чате | `install_admin_webapp_buttons` |
| 14 | Resume отклонён: `emergency_active=true` из истории | штатный CLI resume (флаг персистентен by design) |
| 15 | Биржа: `Builder address not registered` / `has no ASTER balance` | продуктовые шаги: регистрация в Builder Center (реестр общий — доказано зондом) + faucet ≥100 ASTER на perps билдера |
| 16 | Тайминг-флак `test_two_equal_reductions_both_execute` | задокументирован (order-dependent, вне этапа) |

## 6. Как гнать тесты

```bash
# офлайн-гейт (быстро, без сети)
.venv/bin/python -m pytest -q -m "not testnet"          # 714 passed / 2 skipped
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .

# фронт
cd miniapp && pnpm install && pnpm test && pnpm lint && pnpm build

# интеграция полного flow (герметично, MockTransport)
.venv/bin/python -m pytest tests/integration/test_miniapp_builder_flow.py -q

# живой тестнет (требует CT_TESTNET=1, wallet.txt c evm-ключом copyc2,
# зарегистрированного билдера и faucet ASTER на его perps)
CT_TESTNET=1 .venv/bin/python -m pytest tests/testnet/test_builder_mini_app_e2e.py -q -m testnet
```

## 7. Живая приёмка (выполнена владельцем 2026-08-31)

- бот: `run-bot` + `CT_MINIAPP_*` env; туннель `scripts/dev-miniapp-tunnel.sh`;
  WebApp-кнопка ставится ботом (дефолт + админ-чаты);
- второй аккаунт (stage-2 direct) отключён в боте → визар из Telegram →
  WalletConnect → TokenPocket → подпись → success → home;
- прод-БД: `tg-8264892202` user=0x3c45…F975, signer=0xa701…41Ce (сгенерирован
  билдером), paused; main-ключ владельца в системе отсутствует.

## 8. Отложенное (часть 2)

`/builder/userTrades` fee-стейтмент; миграции `identity_source` + builder-
снапшот; 4-табовый shell (Позиции/Активность/Настройки); read-only
PositionPageDTO; risk constraints; revoke/replace; VPS+домен вместо туннеля;
`builder+feeRate` в регулярной торговле движка; mainnet-гейт.
