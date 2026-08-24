# План разработки Stage 2: Telegram + direct V3 onboarding

Дата подготовки: 2026-08-23  
Статус: **реализация и автоматический gate выполнены 2026-08-24; Stage 2 не CLOSED, открыт ручной WP9b**  
Ветка: `codex/stage-2-telegram`, baseline `03e3060` (`main`)  
Область: один master, 10–30 followers на первом запуске, максимум 100  
Среда acceptance: только Aster futures testnet

## 0. Текущий execution status — 2026-08-24

- Реализация Stage 2 и автоматический gate завершены; текущий результат не
  означает закрытие всего этапа.
- Stage 2 остаётся **OPEN**: отдельно открыт ручной WP9b — owner acceptance в
  private Telegram chat.
- Подтверждённый owner flow для WP9b: captcha, выдача доступа, admin approval
  и direct onboarding/подключение. Остальные ручные проверки перечислены в
  `docs/DEV_PLAN_SKELETON.md` и остаются открытыми.
- Финальный аудит, review и acceptance выполняет root лично. Они не являются
  задачами субагентов и не подменяют ручную owner acceptance.

Раздел 14 ниже сохранён как исходный плановый baseline: его чек-листы `[ ]`
не являются текущей картой выполненных работ и не переписываются задним числом.
Текущая карта исполнения — этот раздел и отдельный WP9b-чек-лист в
`docs/DEV_PLAN_SKELETON.md`.

## 1. Цель и итоговое архитектурное решение

Stage 2 добавляет к принятому торговому ядру Stage 1 четыре возможности:

1. Telegram-интерфейс для пользователей и оператора;
2. прямое подключение существующего Aster V3 Agent по набору
   `user + signer + signer private key`;
3. просмотр и изменение существующей `RiskPolicy`, pause/resume и безопасное
   управление аккаунтом;
4. персональные Telegram-уведомления, не блокирующие торговое исполнение.

Архитектура остаётся **одним async modular monolith process + SQLite WAL**.
Telegram является входным/выходным адаптером вокруг готового core:

```text
Telegram update
    -> Router/filter/FSM
    -> UserApplicationService
    -> onboarding/control/settings application service
    -> repositories / public App boundary

Aster master event
    -> Projector -> Coordinator -> Risk -> Planner -> Journal -> Executor
    -> structured notification event
    -> SQLite notification outbox
    -> Telegram worker
```

`risk`, `planner`, `executor`, `reconciler` и master projection не
переписываются. Допустимы только узкие integration seams: notifier port,
публичная регистрация follower runtime и безопасный снимок follower list на
одну fan-out generation.

## 2. Проверенный baseline и реальные точки расширения

### Что уже готово

- `App` — composition root с корректным fail-closed boot, heartbeat, master WS,
  periodic reconcile и shutdown;
- `SqliteCredentials` — AES-256-GCM, master key вне БД, cache-backed
  `IdentityProvider/KeyProvider`;
- `SettingsRepo` — durable `RiskPolicy` и глобальные runtime flags;
- `Coordinator` читает policy из SQLite при каждом recompute;
- `Coordinator` уже принимает async notification callback;
- `Executor` имеет per-account locks, durable journal, idempotency и
  `operation_id`;
- SQLite WAL/write lock и 30/100-slot scale gates уже доказаны Stage 1;
- Aster `AsterClient` уже умеет signed read `balance`, `positionRisk`,
  `openOrders`, position mode и все trading writes.

### Что мешает Stage 2 сейчас

1. `App` жёстко импортирует console `notify`, поэтому Telegram sink нельзя
   подставить composition-level dependency.
2. Credentials и followers загружаются только при boot.
3. `Coordinator.on_master_delta()` итерирует живой mutable list и после await
   снова zip-ит его; hot-add во время fan-out небезопасен.
4. `SqliteCredentials.import_identity()` коммитит account, credential и default
   policy несколькими независимыми writes; crash может оставить REAL account
   без credential и сломать следующий boot.
5. Нет связи numeric Telegram user/chat с follower account.
6. Нет public application services для per-account status, pause/resume,
   disconnect и emergency; CLI использует внутренние методы `App`.
7. Уведомления — неструктурированные строки, часть account context находится
   только внутри текста.
8. `GET /fapi/v3/agent` и DTO прав Agent ещё не реализованы в Aster adapter.

## 3. Scope Stage 2

### Пользовательский scope

- только private chats;
- один Telegram user управляет одним follower account в MVP;
- один Aster `user_address` и один Telegram owner не могут быть привязаны к
  двум активным follower rows;
- operator/admin определяется immutable numeric Telegram user ID из env;
- интерфейс на русском;
- прямой V3 onboarding на testnet;
- default risk policy, изменение risk %, leverage cap и entry deviation;
- pause/resume новых входов;
- status из локального authoritative runtime state и опциональный read-only
  Aster refresh;
- per-account emergency close с отдельным подтверждением;
- disconnect/credential replacement только при безопасных preconditions;
- персональные и operator notifications.

Direct private-key input через Telegram является экспериментальным
**testnet-only** путём и не считается production-secure onboarding. Любое
будущее разрешение mainnet/real funds при таком transport — P0 blocker; для
production предназначен Builder/WebApp Stage 3.

### Admin scope

- общий status;
- список подключённых followers;
- inline allowlist: список пользователей и выдача доступа по Telegram ID;
- постраничный список счетов подписчиков и адресный reconcile;
- global pause/resume;
- существующий emergency-close-all через общий application service;
- reconcile одного account;
- просмотр безопасного summary ошибок/outbox без raw secrets.

### Non-goals

- Builder/Aster Code, wallet WebApp и main-wallet signatures — Stage 3;
- mainnet и реальные средства;
- зеркало неисполненных master limit orders;
- новый `master/observer.py` только ради структуры: текущий `UserStream` уже
  закрывает ответственность;
- follower WebSocket pool;
- Strategy Orders/OCO как follower execution foundation;
- Redis, PostgreSQL, Celery, Kafka, RabbitMQ, Kubernetes, микросервисы;
- webhook/reverse proxy/TLS, пока один polling process достаточен;
- persistent dialog framework, custom SQLite FSM, Redis FSM;
- i18n, payments, subscription billing, массовая marketing-рассылка;
- реализация Telegram Mini App/admin web panel в этом pass; общий application
  contract для него обязателен уже сейчас;
- изменение risk formula или trading semantics Stage 1.

## 4. Версия и используемый aiogram API

Runtime dependency:

```text
aiogram==3.30.0
```

Python 3.12 поддерживается; aiogram 3.30.0 поддерживает Telegram Bot API 10.2.

Используемые механизмы:

- `Bot`, `Dispatcher`, feature `Router`;
- `DefaultBotProperties(parse_mode=ParseMode.HTML)`;
- `CommandStart`, `Command`, `F`, custom filters;
- typed `CallbackData`;
- `InlineKeyboardBuilder`;
- `StatesGroup`, `State`, `FSMContext`;
- `MemoryStorage` + `SimpleEventIsolation`;
- Dispatcher workflow-data dependency injection;
- один authorization/private-chat outer middleware;
- router/dispatcher error handlers;
- `TelegramRetryAfter` и типизированные Telegram exceptions;
- `Dispatcher.feed_raw_update()` для offline tests;
- long polling с `tasks_concurrency_limit=16` и
  `resolve_used_update_types()`.

Не использовать старые aiogram 2.x/ранние 3.x паттерны: `Executor`,
`Dispatcher(bot)`, `_handler` decorators, keyword filters, `skip_updates`,
global bot context и прямой `parse_mode` в `Bot`.

Полный срез API и источники находятся в
[STAGE2_AIOGRAM_RESEARCH_2026-08.md](STAGE2_AIOGRAM_RESEARCH_2026-08.md).
Утверждаемый UX/screen contract находится в
[STAGE2_TELEGRAM_UX_BRIEF_2026-08.md](STAGE2_TELEGRAM_UX_BRIEF_2026-08.md).

`Scenes Wizard` изучен отдельно и в Stage 2 **не используется**: в aiogram
3.30 он остаётся experimental, решает ветвящиеся FSM-диалоги, а не menu
navigation. Dashboard строится на stateless fresh render; линейный onboarding
и risk input — на `StatesGroup`. Критерии пересмотра записаны в UX brief §12.

## 5. Минимальная структура модулей

```text
src/copytradeaster/
├── app.py                         # engine composition, notifier/registry injection
├── config.py                      # Telegram/security settings
├── application/
│   ├── models.py                  # transport-neutral Stage 2 DTO/errors
│   └── service.py                 # ownership + dashboard/onboarding/control use cases
├── onboarding/
│   ├── __init__.py
│   └── direct.py                  # aiogram-independent direct V3 use case
├── telegram/
│   ├── __init__.py
│   ├── runtime.py                 # Bot/Dispatcher/routers/lifecycle
│   ├── menu.py                    # Screen/callback/render/edit/back/stale policy
│   ├── flow.py                    # short non-secret StatesGroup + auth middleware
│   └── routers/
│       ├── common.py              # start/help/cancel/menu
│       ├── onboarding.py          # transport only
│       ├── settings.py            # risk/status/pause/resume
│       └── admin.py               # выделяется только при реальном объёме
├── notifications/
│   ├── models.py                  # NotificationEvent/port
│   ├── console.py                 # existing sink adapted to port
│   └── telegram.py                # outbox worker/sink
└── storage/
    ├── database.py                # append-only migrations
    └── repositories.py            # Telegram/outbox + targeted atomic writes
```

Это не жёсткое требование «по файлу на endpoint». Если `common`/`settings`
остаются короткими, их можно объединить; `admin.py`/отдельный middleware тоже
не создаются заранее. Нельзя объединять aiogram handlers с криптографией/Aster
validation. Future Mini App импортирует `application.service`, но имеет свой
presenter; Telegram screen/keyboard модели туда не протекают.

## 6. Роли и authorization

### Идентификаторы

- security identity — `Telegram User.id` (`INTEGER`), не username;
- delivery destination — private `chat.id`;
- follower ownership — relation `telegram_user_id -> account_id` в SQLite;
- username/first name могут использоваться только как необязательный display
  metadata и всегда HTML-escape-ятся.

### Два слоя проверки

1. outer middleware допускает только private chat и разрешённых/известных
   пользователей;
2. `UserApplicationService` повторно проверяет ownership/role для каждой операции.

Для `Message` private-chat gate проверяет `message.chat.type`. Для
`CallbackQuery` обязательны `from_user.id`, повторная ownership-проверка и
доступный private message context. Inline callback, group callback и callback
с inaccessible/no message получают deny + `answer()` без runtime facts.

Callback payload никогда не является authorization. Подделанный `account_id`
или старый callback должен завершаться deny + `callback.answer()`, не изменяя
состояние.

### Bootstrap пользователей

- admin IDs приходят из `CT_TELEGRAM_ADMIN_IDS` и являются единственным
  источником admin authority;
- `telegram_users.role='ADMIN'` — только metadata: DB role без ID в env не даёт
  global pause/emergency/reconcile;
- self-registration в public bot Stage 2 не открывается;
- новый Telegram user сначала пишет боту и получает собственный numeric ID;
  оператор выдаёт доступ через inline-админку; узкая CLI-команда остаётся
  только recovery-инструментом;
- неизвестному пользователю бот отвечает нейтральным отказом без runtime facts.

## 7. Storage и миграции

Миграции добавляются только в конец `MIGRATIONS`.

### `telegram_users`

```text
telegram_user_id INTEGER PRIMARY KEY
chat_id           INTEGER NOT NULL UNIQUE
account_id        TEXT UNIQUE REFERENCES accounts(id)
role              TEXT NOT NULL CHECK(role IN ('USER','ADMIN'))
notifications_enabled INTEGER NOT NULL DEFAULT 1
blocked_at        TEXT
access_enabled    INTEGER NOT NULL DEFAULT 1
created_at        TEXT NOT NULL
updated_at        TEXT NOT NULL
```

`account_id` у admin может быть `NULL`. Один follower не может принадлежать
двум Telegram users.

Application transaction дополнительно доказывает: owner связывается только с
REAL follower (не master/SHADOW). Admin authority определяется immutable ID из
env и является дополнительной capability: тот же Telegram ID при необходимости
может владеть одним follower account. Stable internal ID генерируется как
`tg-<telegram_user_id>`, не из
пользовательского label. Адреса нормализуются до одного canonical form;
expression index `lower(accounts.user_address)` защищает от case-variant
дубликатов старых/новых rows.

### `notification_outbox`

```text
id             INTEGER PRIMARY KEY AUTOINCREMENT
event_key      TEXT NOT NULL
telegram_user_id INTEGER NOT NULL REFERENCES telegram_users(telegram_user_id)
chat_id        INTEGER NOT NULL
account_id     TEXT
kind           TEXT NOT NULL
severity       TEXT NOT NULL CHECK(severity IN ('INFO','WARN','CRITICAL'))
text           TEXT NOT NULL
status         TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SENT','FAILED'))
attempts       INTEGER NOT NULL DEFAULT 0
available_at   TEXT NOT NULL
lease_until    TEXT
claimed_by     TEXT
last_error     TEXT
created_at     TEXT NOT NULL
updated_at     TEXT NOT NULL
sent_at        TEXT
UNIQUE(event_key, telegram_user_id)
```

Один critical event создаёт отдельные rows для owner и каждого admin. Индекс
`(status, available_at)` обслуживает worker.

Статусы: `PENDING`, `SENDING`, `SENT`, `FAILED`. Claim выполняется атомарным
`UPDATE ... WHERE status='PENDING' AND available_at<=now`; `SENDING` получает
`lease_until` и `claimed_by`. Только истёкший lease возвращается в `PENDING`.
После bounded `max_attempts` row становится terminal `FAILED`. Неизвестный
результат send допускает возможный duplicate notification, но никогда не
повторяет финансовый effect.

### `agent_validations`

```text
account_id        TEXT PRIMARY KEY REFERENCES accounts(id)
signer_address    TEXT NOT NULL
can_read          INTEGER NOT NULL
can_perp_trade    INTEGER NOT NULL
can_withdraw      INTEGER NOT NULL
expired_ms        INTEGER NOT NULL
ip_whitelist      TEXT NOT NULL
status            TEXT NOT NULL
checked_at        TEXT NOT NULL
last_error_code   TEXT
```

Это не credential и не source of truth Aster, а безопасный последний snapshot
для Status screen. Resume требует свежую проверку (по умолчанию не старше
60 секунд) либо выполняет новый signed `GET /agent`; missing/null critical
flags трактуются fail-closed.

### Существующие flags/settings

Семантика фиксируется явно:

- `accounts.enabled` — account имеет валидные credentials и может быть включён
  в runtime registry;
- `copy_settings.enabled` — жёсткое участие в coordinator; user-facing обычный
  pause не меняет этот флаг;
- `copy_settings.allow_new_entries=false` — не открывать/не увеличивать, но
  продолжать reductions, closes и protection convergence;
- `app_state.allow_new_entries` — global operator pause;
- `emergency_active` — global fail-closed emergency state.

Hard disable/disconnect разрешён только после authoritative flat/no bot-owned
orders и idle journal. Для обычной кнопки «Пауза» используется только
per-account `allow_new_entries=false`.

`SqliteCredentials.load()` в Stage 2 читает только `REAL AND enabled=1`.
Disconnect атомарно ставит `accounts.enabled=0`, `copy_settings.enabled=0`,
удаляет credential blob и runtime entry, но может сохранить Telegram/history
rows. Поэтому disabled historical account не ломает следующий boot.

### Atomic credential commit

Нужна одна узкая transaction operation, которая атомарно сохраняет:

```text
accounts row
encrypted credentials blob
default/existing copy_settings
telegram ownership mapping
latest agent validation snapshot
```

Нельзя оставлять REAL account без credential: текущий boot cache fail-closed
и законно остановит весь App. Не нужен generic Unit of Work framework;
достаточен один transaction method/service под direct onboarding и replacement.

Transaction выполняется под общим `Database.write_lock` с явными
`BEGIN/COMMIT/ROLLBACK` и прямыми statements на одном connection; внутри неё
нельзя вызывать текущие autocommit `execute_write()`/repo methods. Cache и
runtime registry обновляются только после успешного commit.

## 8. Direct V3 onboarding service

`onboarding/direct.py` не импортирует aiogram и принимает DTO примерно такого
смысла:

```text
telegram_user_id
user_address
declared_signer_address
signer_private_key
label
```

### Шаг 1 — локальная проверка

- все поля непустые и находятся в согласованных size limits;
- user/signer — валидные EVM addresses и нормализованы;
- private key разбирается `eth-account`;
- signer, вычисленный из private key, равен введённому signer;
- user не равен master/follower address другого account;
- credential input не включается ни в `repr`, ни в exception message;
- main private key никогда не принимается.

### Шаг 2 — ephemeral signed preflight

До записи в БД создаётся временный in-memory `IdentityProvider/KeyProvider` и
обычный `AsterClient`. Persistent nonce резервируется для вычисленного signer.

Endpoint относится к USER_DATA и подписывается authorized signer, а не main
wallet registration key. Public typed method размещается на явной Aster
signed-query boundary; registration writes остаются в `management_api.py`.
До реализации фиксируются: list response, обязательные query fields,
millisecond expiry, address normalization, space-separated IP/CIDR whitelist,
missing/null permissions и safe error codes.

Проверки:

1. `GET /fapi/v3/agent` возвращает запись именно вычисленного signer;
2. `canRead=true`;
3. `canPerpTrade=true`;
4. `canWithdraw=false` — иначе hard reject;
5. `expired` находится в будущем с заданным safety margin;
6. `ipWhitelist` удовлетворяет configured egress policy;
7. signed `balance`, `positionRisk`, `openOrders`, position mode читаются;
8. REST URL остаётся testnet;
9. account flat и не имеет открытых ордеров при первом подключении;
10. position mode совпадает с configured master mode либо исправляется только
    отдельным явным действием на уже flat account.

### IP whitelist policy

Acceptance требует статический egress IP VPS и наличие этого IP/CIDR в Agent
whitelist. Пустой или несовпадающий whitelist блокирует activation.

Для локальной разработки допустим только явный testnet-only override; он:

- выключен по умолчанию;
- запрещён на URL без `testnet`;
- виден на Status screen как warning;
- не считается production acceptance evidence.

### Шаг 3 — commit и activation

1. encrypted atomic commit;
2. обновление credential cache;
3. public `FollowerRegistry.add_or_replace()`;
4. account остаётся с `allow_new_entries=false`;
5. read-only snapshot и targeted reconcile без новых entries;
6. пользователь видит masked identity, balance, mode и default risk;
7. только отдельное явное «Начать копирование» разрешает entries.

Текущий `SqliteCredentials.import_identity()` для этого use case не
используется: он создаёт default policy с `allow_new_entries=true` и не даёт
нужной общей transaction boundary. Новый onboarding commit до публикации в
registry явно пишет `allow_new_entries=0`.

Если любой шаг до commit упал, в БД нет account/credential. Если runtime
registration после commit упала, account остаётся paused и восстанавливается
при boot.

### Credential replacement

- account сначала paused;
- executor mutation lock должен быть idle;
- новый user address обязан совпасть с привязанным account либо выполняется
  отдельный disconnect+new onboarding;
- новый signer проходит весь preflight;
- swap encrypted blob атомарный;
- cache заменяется после commit;
- targeted read-only snapshot подтверждает новую identity;
- entries не включаются автоматически.

## 9. Public follower runtime registry

Hot-add нужен: ручной restart после каждого из 10–30 onboarding workflows —
плохая эксплуатационная граница. Реализуется узкий публичный сервис, а не
вызовы `_make_client`, `_snapshot` или других private методов из handlers.

Минимальный API:

```text
FollowerRegistry.load_at_boot(rows)
FollowerRegistry.add_or_replace(account_id)
FollowerRegistry.get(account_id)
FollowerRegistry.snapshot()
FollowerRegistry.remove_when_safe(account_id)
```

Требования:

- одна запись на account_id;
- registry становится единым источником для `App._client_for`, Coordinator,
  Executor client lookup, CLI/Telegram services и hot-add/replace/remove;
- credential cache остаётся key provider, но registry entry публикуется только
  после успешного cache update;
- mutation registry сериализована asyncio lock;
- `Coordinator.on_master_delta()` берёт immutable tuple snapshot один раз и
  использует тот же snapshot для `gather` и result mapping;
- текущая generation может не увидеть нового follower; следующая reconcile
  увидит — это допустимо, потому что account ещё paused;
- remove невозможен, пока есть position/order/in-flight mutation;
- runtime registry не хранит secret, client получает identity из credential
  provider;
- ошибочный hot-add одного account не меняет существующих followers.

Для pause/replacement/disconnect вводится один public per-account mutation
context, сохраняющий существующий порядок locks: сначала `FollowerRuntime.lock`,
затем executor account lock. Workflow сначала durable ставит
`allow_new_entries=false`, затем ждёт уже начатую generation и удерживает оба
lock через DB commit, cache swap, registry update и targeted snapshot.

Семантика pause честная: generation, начатая до записи pause, может завершиться;
после возврата команды pause является barrier для новых entries. Aiogram
`SimpleEventIsolation` эту бизнес-гонку не решает.

Это единственное необходимое изменение follower lifecycle; отдельный process
manager или distributed registry не нужен.

## 10. Telegram runtime и lifecycle

### Конфигурация

Добавить в `Settings`:

```text
telegram_bot_token                 SecretStr/строка, не логируется
telegram_admin_ids                 list[int]
telegram_polling_timeout_s         default 10
telegram_tasks_concurrency_limit   default 16
telegram_notifications_enabled    default true
telegram_outbox_poll_interval_s    небольшой положительный interval
aster_egress_ips                   list[str/CIDR]
require_agent_ip_whitelist         default true
```

Все значения валидируются Pydantic; bot token никогда не печатается в config
dump или status.

### Startup

1. создать Telegram Bot/Dispatcher, но не polling tasks;
2. создать notifier/outbox dependencies и установить sink до `App.start()`,
   чтобы ранние startup/READY events уже попадали в SQLite;
3. во внешнем `try/finally` запустить `App.start()` и дождаться Stage 1 `READY`;
4. best effort зарегистрировать private-chat Bot commands: ошибка меню не
   блокирует handlers, но видна оператору;
5. вызвать `get_webhook_info()`; при непустом URL выполнить
   `delete_webhook(drop_pending_updates=False)` и повторно доказать пустой URL;
6. запустить notification worker;
7. запустить long polling с `close_bot_session=False`: Telegram session
   закрывает внешний lifecycle owner после drain.

Если engine не достигает READY, process остаётся fail-closed и polling не
публикует ложный рабочий статус. Оператор использует systemd/CLI/SQLite status
для восстановления; строить второй «management-only mode» в Stage 2 не нужно.
Внешний `finally` обязан закрыть частично созданные App/Bot resources: startup
exception в aiogram hook сам по себе не гарантирует `emit_shutdown` и закрытие
Bot session.

### Polling

```text
handle_as_tasks=true
tasks_concurrency_limit=16
allowed_updates=dispatcher.resolve_used_update_types()
handle_signals=true у единственного lifecycle owner
```

`drop_pending_updates=true` никогда не включается скрыто; это отдельное
операторское решение при первом запуске/инциденте.

Startup test фиксирует exact expected update set (`message`,
`callback_query`). Динамическая регистрация handlers после старта запрещена.
`TelegramConflictError` означает второй polling process и завершает runtime с
явной operator error; это не маскируется бесконечным локальным retry.

### Shutdown

1. прекратить принимать updates и закрыть admission новых user mutations;
2. project-level task tracker дожидается уже запущенных handlers до bounded
   timeout, затем безопасно отменяет остаток;
3. дать notification worker bounded drain, незавершённое остаётся в outbox;
4. дождаться/остановить coordinator post-fill tasks;
5. остановить engine tasks и master WS через `App.stop()`;
6. закрыть Telegram aiohttp session ровно один раз;
7. Aster httpx и SQLite закрывает их владелец;
8. финальный `ready=false` остаётся обязательным.

aiogram не предоставляет этот полный barrier автоматически. Outer middleware
регистрирует текущие update tasks в локальном tracker; `App.stop()` получает
public coordinator drain/stop до закрытия HTTP/DB.

Runtime связывает оба lifecycle через два awaitables: polling task и
`App.stop_event.wait()`. Engine heartbeat/lease failure вызывает
`Dispatcher.stop_polling()`, а завершение/conflict polling ставит engine stop
event. Shutdown/finally идемпотентен, поэтому сигнал и внутренняя ошибка не
закрывают ресурсы дважды. Обычный transient polling reconnect обрабатывает
aiogram backoff и не останавливает engine.

## 11. Telegram flows

### Primary inline menu

Основной UI — одно редактируемое dashboard-сообщение. Текст всегда отвечает на
system/copy/account/risk/staleness, а первая полноширинная кнопка показывает
следующий контекстный шаг: `Подключить`, `Начать`, `Пауза` или `Проверить`.

Основные inline screens:

```text
Dashboard
 -> Status / Refresh Aster
 -> Risk / Edit
 -> Activity
 -> Notifications
 -> Help
 -> Account / Emergency / Disconnect
 -> Admin (только env-admin)
```

Обычная глубина — не больше двух переходов. На screen обычно 6–8 actions,
1–2 buttons в строке; primary action стоит отдельно первой. Back/Home стоят
последними. ReplyKeyboard не используется.

### Slash shortcuts

| Команда | Поведение |
|---|---|
| `/start` | onboarding при empty state, иначе dashboard |
| `/menu` | свежий dashboard |
| `/cancel` | очистить временный input flow и вернуться домой |
| `/help` | help screen |

Остальные функции доступны через inline buttons. Эти четыре команды
регистрируются через `set_my_commands` с private-chat scope; menu commands не
являются authorization. Stage 2 использует `MenuButtonCommands`; будущий
Mini App сможет использовать `MenuButtonWebApp`, не удаляя `/menu`.

### Inline menu engine

- `InlineKeyboardBuilder` + один typed `CallbackData` family;
- callback содержит short ASCII action, view revision и optional opaque token;
- full account ID/address/label/JSON/secret в callback запрещены;
- обычный user account определяется по owner mapping; admin выбирает short
  opaque account key;
- `pack()` проверяется по UTF-8 budget 64 bytes;
- каждый callback немедленно получает `answer(cache_time=0)`;
- per-message asyncio lock защищает от out-of-order edit при double tap;
- данные перечитываются перед каждым render;
- stale revision не выполняет mutation, а открывает свежий dashboard;
- destructive confirmation использует single-use TTL token, привязанный к
  user/chat/message/action; restart инвалидирует token;
- `Message`, `InaccessibleMessage`, `None` и inline callback обрабатываются
  явно; business UI разрешён только в private message context;
- renderer обновляет anchor через `edit_text`/`edit_reply_markup`; если anchor
  удалён, создаёт новый и инвалидирует старую revision;
- только известный no-op `message is not modified` можно подавить; другие
  `TelegramBadRequest` остаются видимыми safe error boundary;
- malformed/stale catch-all handler регистрируется последним и всегда отвечает
  callback.

Menu navigation — shallow parent map Home/Back, не FSM history и не Scene.

### Onboarding screen flow

FSM:

```text
consent/warning
 -> user address
 -> signer address
 -> private key (последний шаг)
 -> delete secret message best effort
 -> direct onboarding service
 -> clear FSM in finally
 -> masked result + paused state
```

Private key:

- не записывается через `state.update_data()`;
- не echo-ится;
- raw update/message payload не логируется;
- локальная переменная живёт только до вызова use case;
- входящее сообщение удаляется максимально рано;
- failure удаления логируется без message text;
- пользователь заранее видит, что Telegram transport не является vault.

Начало onboarding всегда очищает старую незавершённую форму. Внутренний
account ID выводится из Telegram user ID; пользователь не придумывает label. FSM
хранит non-secret `started_at`; шаг старше 15 минут сбрасывается и начинается
заново. Персистентный TTL/custom storage для этого не нужен.

### Risk screen

Настраиваются существующие поля `RiskPolicy`:

- `risk_percent` — UI принимает проценты, application layer переводит в долю
  через `Decimal`, никогда через float;
- `max_leverage`;
- `max_entry_deviation_pct`;
- показывается `risk_base=available_balance`, без новой policy family.

Валидация переиспользует domain `RiskPolicy`; продуктовый safe range может быть
уже domain max и задаётся config/policy, но не дублируется разными handlers.
Update policy атомарный и сохраняет не изменяемые пользователем поля.

Если master/follower сейчас в позиции, изменение risk не вызывает
необъяснимый немедленный grow. Действующая coordinator semantics Stage 1
остаётся source of truth и описывается пользователю перед подтверждением.

### Pause action

Ставит per-account `allow_new_entries=false`:

- новые entries/increases блокируются;
- reductions, closes, Stop/TP convergence продолжаются;
- позиции не flatten-ятся;
- повторная команда идемпотентна.

### Resume action

Разрешается только если:

- engine READY, runtime heartbeat fresh, нет global emergency;
- global `app_state.allow_new_entries` не запрещает entries; если global pause
  активен, account может стать только «armed», но фактически не resume;
- credentials valid и Agent permission проверен signed запросом не старше
  60 секунд;
- policy valid;
- account runtime зарегистрирован;
- authoritative follower snapshot не содержит внешней позиции или открытых
  ордеров; Stage 2 не «adopt»-ит manual state скрыто;
- user подтверждает, что при уже открытой master position возможен вход по
  текущему target/deviation policy.

После resume выполняется targeted reconcile.

### Status screen

Default status читает SQLite/runtime без тяжёлого Aster fan-out:

```text
engine ready/stale/degraded
global pause/emergency
account connected/paused
masked user/signer
risk policy
последняя успешная permission validation
последние безопасные command summaries
notification delivery state
```

Отдельная кнопка «Обновить с Aster» вызывает bounded read-only
balance/position/openOrders для собственного account и rate-limit-ится.

### Emergency action

Двухшаговый typed callback confirmation:

1. показать точный scope: positions будут закрыты, bot-owned protections
   отменены, entries останутся paused;
2. новый callback повторно проверяет ownership/state и запускает общую
   emergency service с новым `operation_id`.

Сервис извлекается из CLI так, чтобы CLI и Telegram использовали одну
реализацию. Per-account emergency сначала durable ставит только его
`copy_settings.allow_new_entries=false`, затем выполняет targeted close с
`operation_id`; global `emergency_active` не затрагивается, поэтому остальные
followers продолжают работу. Global admin emergency сохраняет прежнюю
app-wide semantics. Handler не строит `ExecutionPlan` и не вызывает Aster
endpoints. Callback получает немедленный `answer()`, а итог приходит
notification event.

### Disconnect action

Требуются:

- per-account pause;
- authoritative flat;
- bot-owned open orders = 0;
- execution journal in-flight = 0;
- отдельное подтверждение.

После этого account удаляется из registry, `accounts.enabled=0`, policy
disabled, credential blob удалён, а Telegram ownership/history row сохраняется
как disconnected record для audit и повторного onboarding.
Удаление credentials — явная destructive operation и отдельно тестируется.

## 12. Shared application service

Telegram handlers и будущий Mini App получают не весь `App`, а одну узкую
transport-neutral зависимость:

```text
get_menu_context(requester_id)
get_status(requester_id, account_id=None, refresh=False)
connect_direct(requester_id, input)
update_risk(requester_id, values)
pause_account(requester_id)
resume_account(requester_id, confirmation)
start_emergency(requester_id, scope, confirmation)
disconnect(requester_id, confirmation)
admin_reconcile(requester_id, account_id)
```

Каждый метод:

- проверяет numeric requester ID и ownership;
- принимает/возвращает typed DTO, не aiogram objects;
- никогда не возвращает private key;
- пишет audit-safe operation code/account ID, но не raw input;
- использует public service/repository boundary;
- идемпотентен там, где действие является set-state.

Service возвращает business DTO, а не Telegram `ScreenVM`. Telegram presenter
строит текст/inline keyboard; Mini App presenter позже построит свой JSON/UI.
Так общими остаются ownership, validation, risk/control semantics и errors,
но две поверхности не вынуждены копировать layout друг друга.

## 13. Notification architecture

### Structured event

Минимальная модель:

```text
event_key
account_id optional
kind
severity
text
created_at
```

Events с account_id идут владельцу и при critical severity — admin. Global
events идут admins. Console sink остаётся всегда.

Допустимое изменение core ограничено emission boundary:

- `App` больше не импортирует глобальный console function;
- notifier передаётся в constructor/composition;
- все emission points получают structured safe event: coordinator skips/errors/
  unsafe flatten, App startup/READY, mode warning, mark-refresh/critical
  recovery и confirmed execution;
- coordinator account events получают явный `account_id` вместо необходимости
  парсить `[account_id]` из строки;
- raw `str(exception)` не отправляется пользователю: mapper сохраняет только
  безопасный code/category, детали остаются в локальном redacted log;
- risk/planning/execution decisions не меняются.

### Outbox worker

- enqueue — короткий SQLite write и не ждёт Telegram;
- один worker process с 2–4 asyncio send tasks;
- каждая task получает row только через atomic lease/claim;
- per-chat serialization и примерно 1 message/sec;
- `TelegramRetryAfter` переносит `available_at`;
- network/5xx — bounded retry с jitter;
- 400 payload errors — FAILED без tight loop;
- 403 — `notifications_enabled=false`, остальные users продолжают;
- HTML user data escape-ится; core technical event можно отправлять
  `parse_mode=None`;
- plain-text notification ограничена 4096 символами и безопасно суммируется/
  обрезается до send, без разрыва HTML entities;
- outbox backlog виден в status, но не переводит trading command в FAILED.

После restart recovery scan сверяет durable terminal execution commands с
`event_key` outbox и восстанавливает отсутствующие command notifications. Это
закрывает crash-gap между `CONFIRMED` и enqueue; для не-command operational
events гарантируется доставка после успешного enqueue, но не exactly-once.

### Что не строим

- отдельный broker/worker process;
- exactly-once Telegram delivery;
- template/i18n platform;
- attachment/report delivery;
- bulk marketing broadcasts.

## 14. Work packages и порядок реализации

### WP0 — dependency и документационные boundaries

- [ ] добавить `aiogram==3.30.0`;
- [ ] добавить Telegram settings и `.env.example` без реальных secrets;
- [ ] обновить `ARCHITECTURE.md` только принятыми Stage 2 boundaries;
- [ ] принять/скорректировать `STAGE2_TELEGRAM_UX_BRIEF_2026-08.md`;
- [ ] создать package skeleton без пустых speculative services;
- [ ] сохранить testnet/mainnet guard.

Gate: package import/build проходит; Settings redaction и validation покрыты.

### WP1 — storage/ownership/atomic credentials

- [ ] append-only migrations `telegram_users`, `agent_validations`,
  `notification_outbox` и case-insensitive address index;
- [ ] repositories с закрытыми cursor/writes через общий lock;
- [ ] targeted `BEGIN/COMMIT/ROLLBACK` direct credential transaction без
  autocommit repo calls внутри;
- [ ] atomic direct credential commit/replacement/delete;
- [ ] credentials boot load фильтрует disabled REAL accounts;
- [ ] ownership uniqueness;
- [ ] policy patch methods, не затирающие остальные поля;
- [ ] boot с partially simulated failure не оставляет REAL account без creds.

Gate: storage unit tests + migration from Stage 1 DB + rollback proofs.

### WP2 — Aster Agent permission adapter

- [ ] `AgentDTO`;
- [ ] public typed signed `GET /fapi/v3/agent` на явной USER_DATA boundary;
- [ ] ephemeral identity/key provider для preflight;
- [ ] permission/expiry/whitelist validator;
- [ ] persisted safe validation snapshot + fresh-resume TTL;
- [ ] signed read bundle и safe error mapping;
- [ ] никаких registration/main-wallet signatures.

Gate: respx contract tests и один signed testnet permission preflight.

### WP3 — direct onboarding use case

- [ ] local address/key/signer validation;
- [ ] reject duplicate owner/user address/master mapping;
- [ ] full remote preflight до persistence;
- [ ] flat/no-orders first-activation guard;
- [ ] atomic commit paused by default;
- [ ] не переиспользовать текущий `import_identity()` с entries=true;
- [ ] credential replacement path;
- [ ] safe result DTO/masking;
- [ ] log-capture tests: secret отсутствует во всех outputs.

Gate: invalid input never creates account/credential; valid testnet account
появляется paused и читается после process restart.

### WP4 — public runtime registry и service boundaries

- [ ] `FollowerRegistry`;
- [ ] registry становится единым source для App/Coordinator/Executor/CLI/UI;
- [ ] immutable fan-out snapshot fix;
- [ ] public per-account mutation context для pause/replace/disconnect race;
- [ ] public targeted status/reconcile interfaces;
- [ ] extract per-account/global emergency service from CLI;
- [ ] public coordinator post-fill task drain/stop for safe App shutdown;
- [ ] CLI regression: существующие команды используют те же services;
- [ ] hot-add paused follower, no hidden entry;
- [ ] safe remove preconditions.

Gate: integration test добавляет follower во время fan-out без zip/routing
ошибок; существующие Stage 1 tests остаются зелёными.

### WP5 — aiogram runtime и authorization

- [ ] Bot/Dispatcher/Router composition;
- [ ] long polling, explicit lifecycle owner, bounded tasks;
- [ ] private-chat/auth middleware;
- [ ] workflow-data DI с shared application service;
- [ ] project-level in-flight update task tracker и bounded shutdown drain;
- [ ] inline menu engine: typed callbacks, revision, per-message lock,
  edit/fallback, Back/Home и confirmation tokens;
- [ ] только `/start`, `/menu`, `/cancel`, `/help` + `MenuButtonCommands`;
- [ ] get/delete/recheck old webhook without dropping updates;
- [ ] root error handler без raw exception leak;
- [ ] Dashboard/empty/stale/help/activity/notifications screens;
- [ ] каждый callback отвечает.

Gate: `feed_raw_update` доказывает routing/auth/DI/order без сети.

### WP6 — secure onboarding UI

- [ ] explicit risk warning/consent;
- [ ] `StatesGroup` user/signer без label и без Scenes;
- [ ] private key только последним message и вне FSM;
- [ ] best-effort immediate delete;
- [ ] clear state в `finally` для success/failure/cancel;
- [ ] masked confirmation и paused activation;
- [ ] duplicate/stale callback safety.

Gate включает доказательство, что production routers не регистрируют
`SceneRegistry`/Scene classes; решение Scenes пересматривается только по
критериям UX brief §12.

Gate: key не найден в FSM/SQLite plaintext/log/captured replies/outbox;
delete failure не раскрывает secret и не ломает cleanup.

### WP7 — risk, status и controls

- [ ] risk form на `Decimal`;
- [ ] status/local refresh separation;
- [ ] per-account pause/resume;
- [ ] resume confirmation при открытом master episode;
- [ ] per-account emergency confirmation/result;
- [ ] safe disconnect/replace credentials;
- [ ] admin global pause/resume/emergency/reconcile.

Gate: ownership bypass невозможен; pause продолжает closes/protection;
повторные callbacks идемпотентны.

### WP8 — Telegram notifications

- [ ] structured notifier port и console adapter;
- [ ] account/admin routing;
- [ ] SQLite outbox;
- [ ] atomic lease/claim, max attempts и recovery indexes;
- [ ] worker + retry/403 isolation/per-chat pacing;
- [ ] terminal command recovery scan для missing outbox event keys;
- [ ] startup recovery и bounded shutdown drain;
- [ ] backlog/status visibility;
- [ ] unsafe/raw exception sanitization.

Gate: торговая команда подтверждается независимо от Telegram outage; pending
notification доставляется после restart; blocked user не влияет на остальных.

### WP9 — full verification и документация

- [ ] offline suite/coverage/static/build;
- [ ] signed Aster testnet onboarding/control E2E;
- [ ] real Telegram test-bot smoke;
- [ ] 10–30 и 100-user simulated update/notification fan-out;
- [ ] kill/restart recovery для engine + polling + outbox;
- [ ] final flat/no-orders/in-flight audit (выполняет root лично);
- [ ] обновить living docs/status и отдельный run journal;

## 15. Offline test matrix

### Telegram routing/FSM

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| TG-U01 | allowed private user `/start` | menu и owner context |
| TG-U02 | unknown user | нейтральный deny, никаких runtime facts |
| TG-U03 | group/channel update | business handler не вызван |
| TG-U04 | forged account callback | ownership deny + callback answered |
| TG-U05 | rapid state messages | `SimpleEventIsolation`, шаги не смешаны |
| TG-U06 | `/cancel` в каждом state | state cleared, secret отсутствует |
| TG-U07 | fallback order | команды/state handlers не поглощены |
| TG-U08 | handler exception | safe reply/log, polling остаётся жив |
| TG-U09 | HTML user input | корректное escaping |
| TG-U10 | duplicate pause/risk callback | один итоговый state, без побочных эффектов |
| TG-U11 | DB role ADMIN, ID отсутствует в env | admin operation denied |
| TG-U12 | inline/inaccessible/group callback | deny + callback answered, runtime facts 0 |
| TG-U13 | restart mid-form | FSM сброшен; durable данные не потеряны |
| TG-U14 | callback 64/65 UTF-8 bytes + Unicode | 64 accepted, overflow rejected |
| TG-U15 | malformed/stale callback | fallback answered, action 0, fresh dashboard |
| TG-U16 | double tap/out-of-order edit | per-message lock + revision, один effect |
| TG-U17 | anchor deleted/no-op edit | new anchor или narrow no-op handling |
| TG-U18 | router include/fallback order | commands/state/typed callbacks не поглощены |
| TG-U19 | menu text/callback answer limits | 4096/200 guards до Bot API |
| TG-U20 | Scenes production path | `SceneRegistry`/Scene registrations отсутствуют |

### Direct onboarding

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| ONB-U01 | invalid user/signer address | до HTTP/DB не доходит |
| ONB-U02 | invalid private key | safe validation error, key не логируется |
| ONB-U03 | derived signer mismatch | hard reject |
| ONB-I01 | agent отсутствует | persistence = 0 |
| ONB-I02 | `canPerpTrade=false` | reject |
| ONB-I03 | `canWithdraw=true` | reject |
| ONB-I04 | expired agent | reject |
| ONB-I05 | whitelist mismatch/empty | reject по production policy |
| ONB-I06 | signed balance/position read fails | persistence = 0 |
| ONB-I07 | account non-flat/open orders | first activation reject |
| ONB-I08 | valid preflight | atomic account+creds+policy+owner, paused |
| ONB-I09 | injected commit failure | no partial REAL account |
| ONB-I10 | restart after commit | credentials load и owner mapping valid |
| ONB-I11 | hot-add during fan-out | current snapshot stable, next reconcile sees account |
| ONB-I12 | replacement failure | old valid creds/cache remain usable |
| ONB-I13 | master уже в позиции при onboarding | до явного Start follower entry = 0 |
| ONB-I14 | disconnect -> process restart | disabled row не требует credential, boot green |
| ONB-I15 | concurrent onboarding одного owner/address | одна ownership/account row, loser safe |

### Risk/control

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| CTL-U01 | percent parsing | Decimal only, no float |
| CTL-U02 | invalid risk/leverage/deviation | domain error mapped safely |
| CTL-I01 | account pause with open position | no grow, close/protection allowed |
| CTL-I02 | resume while global emergency | denied |
| CTL-I03 | resume with master open | explicit confirmation, targeted reconcile |
| CTL-I04 | disconnect non-flat | denied, creds preserved |
| CTL-I05 | emergency twice | distinct operation ids, both real effects if needed |
| CTL-I06 | user operates other account | denied at application service |
| CTL-I07 | admin global pause | all entries blocked, closes remain allowed |
| CTL-I08 | pause concurrent with master delta | pre-pause generation bounded; после return новых entries 0 |
| CTL-I09 | per-account emergency | выбранный flat/paused, остальные не globally paused |

### Notifications

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| NTF-I01 | account event | только owner (+admin для critical) |
| NTF-I02 | duplicate `(event_key, recipient)` | одна outbox row |
| NTF-I03 | `TelegramRetryAfter` | reschedule по retry_after |
| NTF-I04 | network/5xx | bounded retry |
| NTF-I05 | 400 | FAILED без retry loop |
| NTF-I06 | 403 blocked | recipient disabled, worker жив |
| NTF-I07 | restart with pending/SENDING | recovery и доставка |
| NTF-I08 | Telegram outage during trade | execution result не меняется |
| NTF-I09 | burst 100 users | bounded tasks, per-chat pacing, no DB lock |
| NTF-I10 | owner + несколько admins | отдельная row на каждого recipient |
| NTF-I11 | две send tasks | atomic claim, одна task на row |
| NTF-I12 | crash после claim | только expired lease возвращается в PENDING |
| NTF-I13 | crash после command CONFIRMED до enqueue | recovery scan создаёт missing notification |

### Runtime

| ID | Сценарий | Обязательная проверка |
|---|---|---|
| APP-I01 | engine boot unresolved | polling не объявляет ready |
| APP-I02 | SIGTERM во время долгого handler/post-fill | bounded drain/cancel, ready=false, resources closed once |
| APP-I03 | kill/restart | engine recovery + outbox recovery |
| APP-I04 | Telegram polling reconnect | Aster engine продолжает работу |
| APP-I05 | Aster critical recovery | Telegram failure не мешает reconcile |
| APP-I06 | App.start failure до polling | внешний finally закрывает Bot/App resources |
| APP-I07 | активный старый webhook | delete+recheck, pending updates не dropped |
| APP-I08 | второй polling process | явный conflict/exit, без двойного runtime |

## 16. Signed testnet и real Telegram acceptance

Все Aster операции — только `fapi.asterdex-testnet.com`; Telegram использует
отдельного test bot.

### REAL-TG-00 — preflight

- bot token и admin IDs загружены из env;
- Aster REST/WS URLs содержат `testnet`;
- static egress IP известен;
- master и выбранный follower flat/no-orders;
- один polling process на token;
- test chat private.

### REAL-TG-01 — direct onboarding end-to-end

1. clean Telegram owner без account;
2. inline `Подключить Aster` через реальные handlers;
3. user/signer/private key testnet follower;
4. secret message delete подтверждён Bot API;
5. real `GET /agent` и signed reads проходят;
6. SQLite содержит только encrypted blob;
7. account hot-registered, но entries paused;
8. restart сохраняет mapping и identity.

Успешное удаление Telegram message не объявляется криптографическим
доказательством отсутствия копий.

### REAL-TG-02 — risk + resume + copy cycle

- задать risk через Telegram;
- явно resume;
- master open + Stop;
- follower получает рассчитанный entry/protection через неизменённый core;
- Telegram сообщает result;
- master partial reduce/Stop move/close;
- follower converges и получает notifications;
- repeated status/reconcile не создаёт лишних commands.

### REAL-TG-03 — pause semantics

- pause при живой master/follower position;
- master reduction/close продолжает копироваться;
- новый master entry после flat не открывается;
- resume только после confirmation;
- targeted reconcile восстанавливает нормальную работу.

### REAL-TG-04 — per-account emergency

- открытая testnet position;
- двухшаговое Telegram confirmation;
- entries paused до close;
- position flat, bot-owned orders 0;
- два stable snapshots;
- notification result доставлен;
- pause остаётся активным.

### REAL-TG-05 — credential replacement

- account paused/flat/idle;
- новый valid testnet Agent signer для того же user;
- old credential заменён атомарно;
- новый signed read проходит без process restart;
- entry остаётся paused до явного resume.

### REAL-TG-06 — Telegram outage/recovery

- Bot API stub/controlled network fault во время master action;
- trading journal приходит в terminal status;
- notification остаётся pending/retry;
- после восстановления Telegram сообщение доставляется;
- duplicate financial command = 0.

### REAL-TG-07 — restart

- pending notification + connected follower;
- graceful restart и отдельный kill/recovery run;
- stale heartbeat корректно виден;
- boot recovery core проходит;
- outbox восстанавливается;
- bot polling возвращается один раз;
- follower state не дублируется.

### REAL-TG-08 — final audit

- master/followers flat/no-orders;
- execution in-flight = 0;
- notification outbox не имеет бесконечных retry/SENDING;
- stopped engine `ready=false`;
- private key отсутствует в Telegram replies, logs, FSM dump, plaintext DB;
- только encrypted credential blobs;
- все position modes восстановлены;
- cleanup errors = 0.

## 17. Scale gate

### SCALE-TG-01 — 30 users

- 30 owner/account mappings;
- mixed Status/Risk screens, pause idempotency и callback traffic;
- simulated notification burst;
- `tasks_concurrency_limit=16` не превышен;
- SQLite lock errors 0;
- unauthorized cross-account operations 0;
- Telegram failures одного recipient изолированы.

### SCALE-TG-02 — 100 users

- 100 mappings и 100 notification deliveries через local Bot API stub;
- bounded backlog и tasks;
- записать p50/p95/max handler latency, outbox drain time и retry counts;
- никакой trigger не создаёт Aster mutation напрямую из handler;
- trading core 100-slot hermetic regression остаётся зелёным.

Это подтверждает Telegram/control path, но не означает наличие 100 реальных
Aster/Telegram пользователей.

## 18. Definition of Done Stage 2

Stage 2 получает `CLOSED` только если одновременно:

- stable `aiogram==3.30.0` зафиксирован и package/build green;
- primary UI — один inline dashboard; опубликованы только `/start`, `/menu`,
  `/cancel`, `/help`, все остальные функции доступны buttons;
- Bot и будущий Mini App используют shared application service, а не две
  реализации ownership/risk/control rules;
- production path не использует experimental Scenes; `StatesGroup` содержит
  только ephemeral non-secret flow state;
- direct V3 flow валидирует signer, Agent rights, expiry и whitelist до commit;
- `canWithdraw=true` никогда не принимается;
- private key не хранится plaintext и не попадает в FSM/log/reply/outbox;
- account/credential/policy/ownership commit атомарен;
- новый follower подключается paused без ручного restart и без race fan-out;
- risk/status/pause/resume работают только в owner scope;
- pause не ломает closes/protection convergence;
- emergency использует общий durable core workflow;
- Telegram outage не блокирует и не меняет результат trading execution;
- notification outbox переживает restart и изолирует recipients;
- offline/static/build gates проходят при coverage не ниже 87%;
- signed REAL-TG-00..08 завершены без пропущенных фаз;
- SCALE-TG-01/02 завершены;
- final Aster REST/SQLite audit clean (финальную проверку выполняет root лично);
- mainnet по-прежнему запрещён;

## 19. Реалистичная оценка

Для одного разработчика с существующим ядром:

| Блок | Оценка |
|---|---:|
| WP0–WP2: dependency/storage/Aster permission adapter | 2–3 рабочих дня |
| WP3–WP4: onboarding + runtime registry/services | 2–3 дня |
| WP5–WP7: inline menu/FSM/settings/control | 3–4 дня |
| WP8: outbox/notifications | 2–3 дня |
| WP9: offline/signed/Telegram/scale/documentation | 3–5 дней |

Итого: ориентир **12–20 рабочих дней**, не календарное обещание. Наибольшая
неопределённость — реальные Agent whitelist/Telegram deletion behaviours и
signed end-to-end cleanup, а не aiogram routing.

## 20. Основные риски и ответы

| Риск | Ответ |
|---|---|
| Secret передаётся через Telegram infrastructure | explicit warning, testnet only, private chat, key-last, no FSM/log, best-effort delete, AES-GCM |
| Partial DB commit ломает boot | один atomic direct-onboarding transaction |
| Hot-add меняет list во время fan-out | immutable generation snapshot + registry lock |
| User pause оставляет позицию без управления | pause меняет только allow_new_entries |
| Telegram медленный/недоступен | SQLite outbox; core не ждёт send |
| Forged callback/account id | application ownership check на каждом use case |
| Duplicate updates/callbacks | set-state idempotency; financial effects через existing journal/operation id |
| Bot blocked одним user | 403 отключает только его notifications |
| Несколько polling processes | один process operational invariant + lifecycle test |
| Scope creep в Builder/WebApp/limits | явные non-goals; Stage 3 отдельно |

## 21. Порядок первого implementation pass

Первый безопасный вертикальный срез:

1. WP0 dependency/config;
2. migration + owner repository;
3. `GET /agent` + direct preflight без persistence;
4. atomic persistence paused;
5. runtime registry hot-add;
6. минимальные `/start`, Dashboard, inline Connect и Status screens;
7. один real testnet onboarding;
8. только после него risk/pause/emergency и notifications outbox.

Такой порядок рано проверяет главный неизвестный контракт Stage 2 и не
смешивает его с UI polish.

## 22. Журнал подготовки плана

### 2026-08-23

- branch `codex/stage-2-telegram` создан от accepted main `03e3060`;
- прочитаны `AGENTS.md`, карта документации, living engine status, roadmap,
  архитектура и полный Stage 1 corrective plan/journal;
- прочитаны `app.py`, `config.py`, CLI, credentials, storage, notifications и
  public/private seams trading core;
- через три `GPT-5.6-Luna/max` research streams и Context7/Firecrawl собрана и
  сверена актуальная stable documentation aiogram 3.30.0;
- официальная Aster reference подтвердила signed `GET /fapi/v3/agent` и поля
  permission response;
- выбран minimal one-process/long-polling/SQLite design без Redis/webhook;
- после correction владельца slash-first набросок заменён inline-first
  dashboard и shared Bot/Mini App application service;
- отдельный субагентный pass изучил Scenes Wizard/inline editing/menu buttons;
  Scenes отклонены для Stage 2 как experimental и избыточные для линейного FSM;
- создан `PRODUCT.md` и отдельный `STAGE2_TELEGRAM_UX_BRIEF_2026-08.md`;
- Исторический snapshot на 2026-08-23: implementation ещё не начиналась,
  тестовые счётчики Stage 1 не менялись.

## 23. Журнал исполнения — 2026-08-24

План реализован в ветке `codex/stage-2-telegram`. Исходная запись выше остаётся
снимком состояния на момент завершения исследования 23.08.2026.

В production path добавлены:

- общий transport-neutral `UserApplicationService` для Bot и будущей Mini App;
- concrete adapters к существующему `App`, SQLite credentials и Aster V3;
- direct onboarding/replacement с подписанным Agent preflight и paused activation;
- inline-first aiogram runtime на `Router`/typed `CallbackData`/`StatesGroup`;
- durable Telegram outbox, recipient routing и bounded worker;
- единый `run-bot` lifecycle с READY-before-polling и упорядоченным shutdown;
- user/admin controls, confirmations, account/global admission barriers;
- admin capability для одного operator ID: master summary, inline allowlist,
  paginated users/followers, grant/revoke Bot access и targeted reconcile без
  отдельного Telegram account;
- автоматические offline, signed testnet, Bot API/polling и 30/100-user gates.

Scenes Wizard в production path не появился: итог исследования §4/UX brief
сохранился без изменений. Дальнейший шаг — только ручной owner acceptance в
private Telegram chat; актуальные доказательства и единственные suite counters
ведутся в `docs/ENGINE_TEST_STATUS_2026-08.md`.

### 2026-08-24 — Access approval hardening

По owner flow второго аккаунта добавлен автоматический первичный доступ:

- unknown private `/start` → короткая inline captcha → durable
  `telegram_access_requests` → карточка configured env-admin;
- middleware пропускает до полной авторизации только `/start` и typed guest
  callback; authorization exception fail-closed, forged regular menu callback
  не попадает в application service;
- один pending request на user проверяется и вставляется одной SQLite
  transaction; `create_pending_result()` сообщает победившую insert-операцию,
  поэтому duplicate captcha callback не отправляет повторную admin-карточку;
- approve/reject допускается только immutable configured admin IDs,
  повторный click идемпотентен; approval атомарно выдаёт UI access и сохраняет
  существующие account/role/notifications;
- Bot API вызывается после commit: direct admin/user delivery failures не
  откатывают durable state, а админка содержит резервный список заявок;
- после approval бот пытается открыть пользователю первое inline-меню с V3
  connect CTA; manual add-by-ID остаётся recovery-only.

Актуальные counters suite и coverage после последующих исправлений ведутся
только в [ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md) — это
единственная точка истины для текущего gate. Полный bounded gate и остаточные
риски зафиксированы в `docs/reports/STAGE2_ACCESS_APPROVAL_2026-08.md`.
