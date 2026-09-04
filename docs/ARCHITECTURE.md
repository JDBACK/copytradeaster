# Архитектура copytradeaster

Архитектура покрывает testnet-движок, Telegram-бота и Aster Code / Builder в одном процессе. Узкие места (порядок WS-событий, partial fill, ручная перестановка OCO/OTOCO) закрыты инвариантами ниже; подтверждение — signed testnet-прогоны (сводка в `ENGINE_TEST_STATUS_2026-08.md`).

Торговое ядро едино для всех этапов: testnet-движок, Telegram-бот и Aster Code / Builder — внешние интерфейсы вокруг него.

Исходные условия:

- один разработчик + LLM;
- один master account;
- ориентир 30–100 followers;
- Python 3.12, async-first;
- надёжность финансовых операций важнее максимальной пропускной способности;
- без микросервисов и инфраструктуры, которая не нужна этому масштабу;
- модули должны иметь ясные обязанности, но не дробиться «по одному модулю на функцию».

## 1. Архитектурный стиль

**Modular monolith + state reconciliation + durable execution journal.**

На старте это один Python-проект, один процесс и одна БД. Внутри — явные границы модулей.

Не используем без реальной необходимости:

- микросервисы;
- Kafka / RabbitMQ / Celery;
- Redis как обязательный компонент;
- Kubernetes;
- CQRS/event-sourcing frameworks;
- отдельный service/class под каждый endpoint или каждую торговую операцию.

Telegram, CLI и Builder onboarding — внешние интерфейсы вокруг одного торгового ядра.

## 2. Главный принцип копирования

Система копирует **состояние**, а не буквально команды мастера.

```text
master event
    ↓
master state changed
    ↓
calculate follower target state
    ↓
compare actual ↔ target
    ↓
execute only the difference
    ↓
verify / reconcile
```

WebSocket — быстрый сигнал об изменении, но не единственный source of truth.

После reconnect, timeout, partial fill, duplicate/out-of-order event или рестарта система должна уметь восстановить состояние через REST snapshot + собственный журнал и снова привести followers к target state.

## 3. Основной runtime flow

```text
ASTER MASTER
     │
     │ user-data WebSocket
     ▼
┌────────────────┐
│   UserStream   │
└───────┬────────┘
        │ master user-data WS (`aster/user_stream.py`)
        ▼
┌────────────────┐
│ Master Projector│
└───────┬────────┘
        │ normalized master state
        ▼
┌────────────────┐
│  Coordinator   │
└───────┬────────┘
        │ follower × N
        ▼
┌────────────────┐
│   Risk Engine  │
└───────┬────────┘
        │ target state
        ▼
┌────────────────┐
│ Planner / Diff │
└───────┬────────┘
        │ execution plan
        ▼
┌────────────────────┐
│ Persistent Commands│
└───────┬────────────┘
        ▼
┌────────────────┐
│    Executor    │
└───────┬────────┘
        ▼
   ASTER FOLLOWERS
        │
        ▼
┌────────────────┐
│   Reconciler   │
└────────────────┘
```

Telegram и Builder не находятся внутри этого потока. Они только управляют настройками, onboarding и уведомлениями.

## 4. Границы модулей

### `aster/`

Адаптер к Aster V3. Не содержит copytrade- и risk-логики.

Отвечает за:

- REST (trading_api) и Agent/Builder management (management_api) — раздельно: разные обязанности и signing contexts;
- user-data WebSocket (user_stream): listenKey, keepalive, reconnect;
- EIP-712 signing (signing.py);
- nonce;
- разбор ошибок;
- weighted rate limits.

Signing details:
- Контексты EIP-712: trading — chainId 1666 (mainnet) / 714 (testnet); registration (ApproveAgent / ApproveBuilder) — только 56 (BSC; `CT_REGISTRATION_CHAIN_ID` валидатором отвергает всё остальное).
- Nonce signer-ключа персистентен: хранится в storage, инкрементируется и сохраняется ДО отправки подписи; после рестарта восстанавливается из БД, а не из памяти.
- При retry после 503/timeout сначала проверяется фактическое состояние биржи (clientOrderId / open orders), затем переподпись с актуальным nonce.

### `master/`

Получает события master account и строит нормализованное состояние.

`aster/user_stream.py` (`UserStream`) держит соединение, listenKey, keepalive,
reconnect и приём raw events; `master/projector.py` (`MasterProjector`)
преобразует Aster payloads во внутреннее состояние проекта, ведёт
copy_trades-эпизоды и эмитит дельты в coordinator. Отдельного
`master/observer.py` нет: граница `UserStream` + `MasterProjector` — достаточная (см. §17).

### `risk/`

Чистый risk engine.

Главная модель риска проекта:

```text
risk_budget = balance/equity base × risk_percent
quantity = функция(entry, stop, fees/slippage, exchange limits)
```

Плечо влияет на margin/liquidation, но не определяет денежный риск до Stop.

Risk engine должен возвращать целевое состояние/quantity, а не отправлять ордера.

Runtime leverage follower — это control-plane, а не вычисление на hot path.
`trading/leverage.py` (`LeverageManager`) держит в RAM два разделённых
состояния: глобальный *кандидатный* максимум по symbol (signed
`GET /fapi/v3/leverageBracket`, обновление stale-while-revalidate фоновым
worker-ом, никогда из торгового пути) и account-specific *подтверждённое*
плечо `(account_id, symbol)`. Подтверждение — `POST /fapi/v3/leverage`
(успех = биржа приняла уровень для этого аккаунта; reject → fallback на
account-specific bracket GET + POST меньшего максимума). Источник правды при
рестарте — authoritative `positionRisk`: coordinator собирает leverage всех
строк (включая flat symbol) в `AccountState.symbol_leverages` и через
`observe()` восстанавливает RAM-модель без единой записи. SQLite для плеча не
используется. Готовность (`is_ready`) привязана к поколению каталога: смена
максимума инвалидирует старые подтверждения, новый вход ждёт переподготовки;
закрытие, сокращение и защита никогда не зависят от готовности. Настройка
происходит в естественных точках: onboarding/resume (application adapter,
аккаунт остаётся paused при неудаче), boot recovery и bounded background
worker (`Semaphore(2)` + pacing, приоритет trading traffic в общем limiter).
Планировщик эмитит `SET_LEVERAGE` только при расхождении known/confirmed
значения с таргетом и связывает его generic-зависимостью `action_id →
depends_on_action_id` с входом и защитой: FAILED prerequisite блокирует
риск-увеличивающие действия плана, но никогда — закрытие/отмены.

Ядро не меняет `marginType` перед входом: текущий режим аккаунта сохраняется
без скрытой подмены; isolated включается только по результату отдельной
проверки на Aster testnet.

### `trading/`

Главное прикладное ядро.
- `coordinator` — запускает пересчёт для затронутых followers;
- `planner` — сравнивает actual state с target state и строит минимальный набор действий;
- `executor` — выполняет подготовленные действия через Aster;
- `reconciler` — повторно сверяет фактическое состояние и исправляет расхождения;
- `leverage` — RAM control-plane плеча: кандидатный каталог + account-confirmed
  состояние, фоновая сверка и preparation (см. §4 `risk/`).

### `storage/`

Хранение торгового состояния, журналов и mappings.

Для этапов 1–2 достаточно SQLite + WAL. PostgreSQL нужен только когда появится реальная причина разделять процессы или возрастёт нагрузка.

Для load-only проверки масштаба `accounts.execution_mode` может быть `REAL` или
`SHADOW`. `SHADOW` rows не имеют credentials и направляются в локальный
`aster/shadow.py`; режим разрешён только явным opt-in на testnet и не является
production execution path. Миграции — упорядоченный список в
`storage/database.py` (`MIGRATIONS`), репозитории — `storage/repositories.py`.
Финальный hybrid gate: 30 slots (3 REAL + 27 SHADOW), 100 циклов, 12 302
подтверждённые команды (пост-фикс rerun 20260823: 12 207/12 207);
подробности в `docs/reports/HYBRID_LOAD_TEST_REPORT_2026-08.md`.

### `credentials/`

Шифрование и получение signer credentials — только для пути `onboarding/direct`.

Приватные signer keys в БД хранятся только зашифрованными (AESGCM, ключ вне БД:
`CT_ENCRYPTION_KEY`). Реализация: `credentials/service.py` + `credentials/crypto.py`.

Асимметрия Builder-пути (этап 3): при ApproveAgent + ApproveBuilder сгенерированный signer-ключ шифруется и персистится в `credentials/` после успешного commit, а главный приватный ключ пользователя в backend никогда не передаётся и не сохраняется. До момента commit промежуточное состояние онбординга хранится в таблице `builder_onboarding_attempts` (состояния: `PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`).

`APPROVAL_SENT` является fail-closed состоянием неизвестного результата
удалённого `approveAgent`: transport/5xx/unknown exchange response не откатывает
его в `PREPARED` и не разрешает слепой повтор user-signed POST. Recovery сначала
проверяет Agent идемпотентным signed GET. Mini App может продолжить без второй
wallet signature только server-owned `APPROVAL_SENT`/`APPROVED`; `PREPARED`
всегда требует подпись. После paused account commit attempt переводится в
`COMMITTED`, а затем очищается после runtime activation либо лениво при
идемпотентном recovery.

Durable account identity (P2.6): `accounts` несут `identity_source`
(`direct|builder`) + nullable Builder snapshot (`builder_address`,
`builder_name`, `builder_max_fee_rate`, `builder_agent_expires_at_ms`,
`builder_verified_at`). Snapshot — last-known evidence, не proof: verified
только одновременными signed `GET /agent` (current signer, допустимые
permissions) + exact-match `GET /builder` (`userAddress`, `builderAddress`,
`maxFeeRate` == cap). Commit Builder-атомарен (credentials + validation +
`identity_source=builder` + snapshot); direct/replacement/disconnect
атомарно чистят proof (`identity_source=direct`, поля NULL). Race исключён
`account_version` (mismatch → `IDENTITY_CHANGED` без commit). Disconnect —
local-only: remote Agent/Builder revoke не выполняется и не заявляется.
Reauthorization — same-signer reuse Part 1 wallet/EIP-712 flow, без ротации
signer; после неё account остаётся paused.

### `onboarding/`

Способ получения `TradingIdentity`. Два независимых адаптера: `direct`
(прямой V3 набор) и `builder` (ApproveAgent + ApproveBuilder). Direct
принимается CLI-импортом (`cli/main.py`) и `onboarding/direct.py`: локальная
проверка адресов/key pair, подписанный `GET /fapi/v3/agent`,
balance/position/open-order preflight и atomic paused commit через
`application/adapters.py`. Trading core не знает, каким способом
credentials появились.

### `notifications/`

Структурированные события не отправляются в Telegram из торговой операции.
`NotificationRouter` быстро и идемпотентно пишет recipient-specific rows в
SQLite outbox, а bounded `TelegramOutboxWorker` доставляет их независимо с
lease/retry/backoff и изоляцией Telegram 403. Console sink сохраняется.

Telegram handlers никогда напрямую не вызывают торговые endpoints Aster.

Direct onboarding остаётся двухфазным: signed preflight обязан завершиться до
любого credential commit; activation после commit публикует только paused
account. `UserApplicationService` не сворачивает `OnboardingError` в общий
`ValidationError`: наружу передаётся только typed
`OnboardingApplicationError.code` (`OnboardingErrorCode`). Exchange status,
адреса и private key не переходят через application boundary. Telegram и
Mini App сами маппят enum в безопасный actionable copy.

### `application/` и `telegram/`

`UserApplicationService` — единая бизнес-граница для Telegram Bot и Mini
App: authorization/ownership, dashboard/status, direct credentials, risk,
pause/resume, notifications, account lifecycle и admin controls. Concrete
`AppStage2RuntimeAdapter` связывает её с готовым Stage 1 core; Telegram handlers
зависят только от `ApplicationProtocol`.

Follower может менять только `risk_percent`. Leverage caps и entry-deviation
остаются legacy storage/runtime полями для совместимости, но application
boundary отвергает их даже при forged Bot/Mini App input. Per-account emergency
остаётся внутренним control-core; пользовательская confirmation для него
запрещена, а `GLOBAL_EMERGENCY` доступен только admin.

Telegram UI — один редактируемый inline dashboard с typed `CallbackData` и
короткими `StatesGroup` только для non-secret input. `Scenes Wizard` изучен, но
не используется: он experimental и не является menu/navigation engine;
ветвящихся wizard flows в Stage 2 нет. Slash-команды `/start`, `/menu`,
`/cancel`, `/help` — только вход и recovery.

Первичный доступ не требует пересылки Telegram ID вручную: неизвестный private
user допускается middleware только на `/start` и typed `GuestAccessCallback`,
проходит короткую inline арифметическую captcha, после чего заявка сохраняется в
`telegram_access_requests`. Карточка с профилем и кнопками approve/reject
доставляется только configured admin IDs из env. Если Bot API временно недоступен,
заявка не откатывается и остаётся в резервном экране заявок админки; recovery
добавление по ID сохраняется только как ручной запасной путь.

Решение администратора повторно проверяет env capability в application service.
Approval/reject идемпотентны: SQLite transaction переводит только `PENDING`, а
approval атомарно создаёт/открывает UI identity, сохраняя существующие account,
role и notification settings. Telegram Bot API вызывается после commit. После
approval бот пытается сразу отправить первое inline-меню подключения V3; ошибка
доставки не меняет durable access и оставляет `/start` recovery.

Onboarding copy использует язык Aster UI: адрес обычного testnet-кошелька из
верхней панели, `API wallet address` со страницы API Wallet и `private key`,
показанный под этим адресом. Перед сбором данных UI показывает путь
`Aster testnet → Settings → Position mode → Hedge Mode`, при создании
`Agent/API wallet` выбрать `Perps`/`canPerpTrade` и чтение, выключить вывод,
авторизовать API
wallet подписью обычного кошелька и проверить отсутствие позиций/ордеров.
Каждое из трёх сообщений с данными удаляется сразу после чтения, до проверки;
Telegram caveat остаётся явно указанным. Переходы шагов создают свежую карточку,
а при невозможности удаления используют редактирование без дубликата; истёкшая
сессия заменяет stale-карточку чистой recovery-карточкой. После удаления
secret-message любой typed preflight/commit/activation failure получает error
screen с remediation, `🔄 Попробовать снова` и `⌂ На главную`. Retry callback
хранит `connect`/`replace` branch, а FSM state и key material очищаются в
`finally`.

## 5. Минимальные внутренние модели

Не требуется большая иерархия классов. На старте достаточно нескольких простых dataclass/Pydantic-моделей:

```text
PositionKey
ProtectiveOrder
PositionState
AccountState
RiskPolicy
TargetPosition
TargetState
ExecutionAction
ExecutionPlan
TradingIdentity
SymbolFilters
QtyDecision
```
Отдельно полезна внутренняя сущность `CopyTrade` / `trade_id`, чтобы отличать два последовательных торговых эпизода по одному символу после `close → reopen`. В реализации эпизод живёт в storage (`copy_trades` + `CopyTradeRepo`), а не в `domain/models.py`: `trade_id` связывает планы, действия и линки.

Для Hedge mode `PositionKey` обязательно различает LONG и SHORT.

## 6. Planner и reconciliation

Planner работает по принципу:

```text
actual state + target state → minimal execution plan
```

Пример:

```text
actual:
  position = 0.15
  stop = 63000

target:
  position = 0.10
  stop = 64000

plan:
  reduce 0.05
  replace stop
```

Повторный `reconcile()` при уже совпадающем состоянии должен создавать **0 новых команд**.

Инвариант конвергенции защиты (правило 9 planner):
после reconciliation на одной follower-leg остаётся **ровно один** bot-owned
Stop и **ровно один** bot-owned TP. Дубликатные условные ордера слота
(остаток FAILED dependent cancel после CONFIRMED replacement) снапшот отдаёт в
`extra_stops/extra_tps`, и planner отменяет их отдельными независимыми
`CANCEL_ORDER` (priority 1) — в том числе на flat-ноге.

Инвариант блокировки зависимых действий при смене плеча (правило 1 planner, баг #25):
`SET_LEVERAGE` получает `action_id`, а последующие `MARKET_OPEN` и защитные ордера —
`depends_on_action_id`. При FAILED/UNKNOWN статусе `SET_LEVERAGE` исполнитель переводит
зависимые действия в `BLOCKED` (fail-closed), при этом закрытия (`MARKET_CLOSE`) и
отмены ордеров никогда не зависят от плеча и исполняются безусловно.
Семантика нулевого таргета:

```text
qty=0 при живом эпизоде мастера ≠ всегда «намеренный флэт».
```

Причина нуля переносится явно — `TargetPosition.hold_flat_until_episode_end`:

- `True` — только same-direction пропорциональная редукция, округлившаяся ниже
  step (dust-нога): персистится маркер `sync_basis.zero_target`, вход запрещён
  (skip `INTENTIONAL_FLAT`) до конца эпизода мастера;
- `False` — транзитный ноль flip'а («мастер перевернулся»): basis очищается,
  обратная сторона открывается следующим проходом.

Флип является границей эпизода: `_apply_fill` закрывает старый episode и
открывает новый с колонкой `copy_trades.direction = ±1` (миграция #13).
Snapshot-sync `rebuild_full` детектит рассинхрон направления после офлайна и
делает тот же close+open. Новый эпизод автоматически снимает sticky-маркеры,
поэтому dust-flat одного направления не блокирует вход в противоположном.

Reconciliation запускается не только после ошибок, а является нормальной частью работы:

- startup;
- master event;
- завершение follower command;
- reconnect;
- timeout/503 UNKNOWN;
- периодический sweep;
- ручной admin trigger.

## 7. Execution journal

Критичные торговые команды нельзя хранить только в `asyncio.Queue`.

Минимальная persistent-модель команды:

```text
id
command_key
account_id
trade_id
kind
payload
recovery_context
priority
status
attempts
last_error
timestamps
```

Минимальные статусы:

```text
PENDING → SENDING → SUBMITTED → CONFIRMED
                         ↘ UNKNOWN
                         ↘ FAILED
```

`503` или timeout после отправки финансовой команды трактуется как `UNKNOWN`, а не как
гарантированный fail. Для `MARKET_OPEN`/`MARKET_CLOSE` Executor до перехода
`PENDING → SENDING` сохраняет в `recovery_context` symbol/leg, точную Decimal-позицию
до mutation и ожидаемый signed delta. Runtime, повторный submit и boot используют один
resolver с тремя исходами:

- `FOUND` — исходный COID найден либо authoritative `positionRisk` показывает
  согласованное продвижение в ожидаемом направлении не больше requested delta;
- `PROVEN_NOT_EXECUTED` — исходный COID отсутствует в трёх полных чтениях
  `allOrders`, а соответствующая position leg во всех чтениях не изменилась;
- `UNRESOLVED` — history/position недоступны, history page насыщена 1000 строками,
  recovery context отсутствует/повреждён или position delta противоречив.

Только `PROVEN_NOT_EXECUTED` разрешает новый COID. `UNRESOLVED` остаётся
non-terminal, ставит degraded и блокирует новую economic mutation. Current-order query
служит быстрым positive lookup, но его `None` не является negative proof. Partial
progress подтверждает выполненную часть; residual создаёт следующий authoritative
reconciliation, поэтому полный исходный quantity повторно не отправляется.

Собственный `command_key` является долговременной идемпотентностью сервиса; `newClientOrderId` используется как внешний correlation id, но не заменяет внутренний журнал. `BLOCKED` — ephemeral-результат исполнителя для действий с упавшим prerequisite (в журнале не персистится; шесть статусов выше — весь CHECK).

### P2.7 Builder attribution (account-specific `builder + feeRate`)

Outbound Builder-атрибуция применяется после journal identity,
непосредственно при построении `PlaceOrderRequest` (`builder`/`feeRate`,
Decimal, cap `0.001`, optional `time_in_force` для plain LIMIT): `command_key`, journal payload и operation ID от
metadata не зависят. `BuilderAttributionProvider` (composition boundary,
`App`) различает DIRECT / BUILDER_VALID / BUILDER_UNAVAILABLE по durable
P2.6 proof + current signer + Agent permissions/actual expiry + config match (permissions строго tri-state: read/perp ровно True, withdraw ровно False; malformed fail closed);
UI TTL на execution не влияет. Snapshot берётся лениво под per-account
mutation lock, один на весь `Executor.submit`, сохраняется через
send→query→retry (`_with_coid`). Safety: entry при UNAVAILABLE fail-closed
до HTTP, close/Stop/TP — fallback без полей + degraded signal,
cancel/leverage не блокируются.

## 8. Конкурентность и масштаб 30–100 followers

Для текущего масштаба достаточно одного asyncio application process.

Модель выполнения:

```text
followers выполняются параллельно
один follower → последовательные mutation-команды
```

То есть операции разных пользователей могут идти конкурентно, но внутри одного follower используется один mutation lock/queue.

Mutation lock сериализует mutation-команды и порядок подписанных запросов одного
follower. Дополнительно storage-level `Database.write_lock` сериализует все
короткие SQLite writes и атомарный nonce `INSERT ... RETURNING` на одном
aiosqlite connection. Это предотвращает interleave `execute/commit` и удержание
скрытых cursor locks при fan-out; обычные writes работают в explicit autocommit.
Read/write cursors закрываются сразу после операции, WAL и busy timeout остаются
включёнными.

Это упрощает nonce, order sequencing и recovery, не создавая заметного bottleneck при 30–100 followers.

Общий weighted rate limiter находится в `aster/` и учитывает фактические веса Aster endpoints.

## 9. Master и follower streams

Master user-data WebSocket обязателен.

Для followers на первом этапе достаточно:

```text
REST confirmation + reconciliation
```

Не нужно сразу держать отдельный WebSocket на каждого follower.

Follower stream pool добавляется позже только если testnet покажет, что REST confirmation недостаточно быстра для post-fill risk correction.

## 10. Stop / TP и Strategy Orders

Внутреннее состояние не должно зависеть от того, как master представил сделку на Aster.

Master может использовать:

- standalone STOP/TP;
- OCO/OTOCO;
- ручной UI.

После нормализации trading core видит только target position/protection state.

Для followers базовый путь предпочтительно строить на обычных ордерах:

```text
MARKET
STOP_MARKET
TAKE_PROFIT_MARKET
```

Strategy Orders не должны быть фундаментом follower execution из-за высокого request weight и более сложного recovery. Их можно добавить позже как отдельную возможность.

## 11. Базовые risk policies

Количество продуктовых политик на старте держим минимальным.

Рекомендуемые defaults и внутренние safety gates:

- новая follower-позиция требует известного Stop;
- при слишком большом entry deviation сделка пропускается;
- после фактического follower fill риск пересчитывается;
- если риск выше допустимого — уменьшается quantity;
- Stop мастера не двигается искусственно ради сохранения quantity;
- если мастер расширил Stop — follower position при необходимости сокращается;
- если Stop стал ближе — автоматически наращивать позицию не нужно.

`entry deviation` остаётся внутренним safety gate ядра и не является
настройкой follower в Bot/Mini App. Единственный пользовательский risk control —
`risk_percent`.

## 12. Recovery при запуске

Приложение не должно принимать новые входы сразу после старта.

```text
BOOT
 ↓
load local state
 ↓
master REST snapshot
 ↓
follower snapshots
 ↓
resolve SENDING/SUBMITTED/UNKNOWN commands
 ↓
seed leverage candidate catalog (fail-closed if unavailable)
 ↓
reconciliation
 ↓
READY
```

Только после `READY` разрешаются новые follower entries. Boot recovery применяет тот же
`FOUND / PROVEN_NOT_EXECUTED / UNRESOLVED` контракт, что и runtime. Legacy MARKET-row
без `recovery_context` не может быть признан неисполненным: он остаётся in-flight и
останавливает boot fail-closed.

Stage 2 process (`application/engine.py`) продолжает этот порядок:

```text
connect/migrate → bootstrap configured admins/recovery floor
→ compose durable notifier + App → App READY
→ start outbox worker → Telegram long polling
```

Shutdown идёт в обратной зависимости: закрыть admission/drain Telegram
handlers → остановить и осушить outbox при живых Bot/DB → `App.stop()` → один
раз закрыть Bot session. На старте процесс ставит notification recovery
floor: исторические confirmations не рассылаются, восстанавливаются только
недоставленные confirmed commands после этого floor.

Операционный лог — отдельный локальный durable sink, не SQLite и не outbox:
`setup_logging()` всегда поднимает console handler и bounded
`RotatingFileHandler` с default `data/logs/copytradeaster.log` (10 MiB, пять
backup-файлов); active и rollover-файлы создаются с POSIX mode `0600`.
Повторный вызов переиспользует managed handlers и не создаёт
дубликаты даже рядом с pytest/caplog handlers. `CT_LOG_PATH`,
`CT_LOG_MAX_BYTES` и `CT_LOG_BACKUP_COUNT` задают настройки; путь не требует
изменения для обычного restart. `httpx`/`httpcore` подняты до WARNING, чтобы
signed URL/query не попадали в request logs.

Критические onboarding-действия пишутся через `log_onboarding_audit`: только
стабильные `user_id`/opaque `account_id`, `mode` (`connect`/`replace`),
`event` (`attempt`/`success`/`recovered`/`rejected`), `result`, `paused` и
стабильная typed classification. `attempt`/`success`/`recovered` имеют INFO,
`rejected` — WARNING. Wallet/API-wallet addresses, private keys, token,
signature, signed URL/query и raw exchange/exception payload не являются
аргументами этих logging calls. Для `attempt` и `rejected` состояние `paused`
пишется как `unknown`, потому что replace может обращаться к уже существующему
account; только `success` и `recovered` подтверждают `paused=true`. Handler
filter — только defense-in-depth для legacy/third-party records, а не замена
этому контракту.

## 13. Минимальное хранение

Не фиксируем подробную SQL-схему заранее. Нужны только основные группы данных:

```text
accounts
credentials
copy_settings
master_events
copy_trades
order_links
execution_commands
telegram_users
telegram_access_requests
agent_validations
notification_outbox
```

Назначение:

- `accounts` — master/followers;
- `credentials` — encrypted signer credentials;
- `copy_settings` — risk %, limits, enable/disable;
- `master_events` — raw event ledger для replay/recovery;
- `copy_trades` — внутренние торговые эпизоды (с `direction = ±1` — сторона
  эпизода, миграция #13);
- `order_links` — master ↔ follower order mapping;
- `execution_commands` — durable execution journal/queue; additive migration #33
  добавляет `recovery_context` с pre-mutation evidence для MARKET recovery.
- `telegram_users` — private-chat allowlist и единственный owner account;
  admin — дополнительная capability immutable ID из env, а не взаимоисключающая
  роль, поэтому один operator ID может одновременно иметь owner account;
  `access_enabled` отзывает только вход в UI и не останавливает торговый runtime;
- `telegram_access_requests` — audit trail заявок первичного доступа; уникальный
  `PENDING` на Telegram user, idempotent decision и `decided_by` configured admin;
- `agent_validations` — последняя безопасная signed Agent snapshot;
- `notification_outbox` — recipient-specific delivery journal без secrets.

В реализации также есть `app_state` — персистентные runtime флаги (`ready`,
`runtime_id`, `started_at`, `heartbeat_at`, `allow_new_entries`,
`emergency_active`, `emergency_operation_id`, `degraded`, `degraded_active`,
`degraded_reason`); `sync_basis` — per-follower sizing reference с
`zero_target`-маркером намеренного dust-flat и `issued_target` для
идемпотентности ревизий; `nonce_store` — атомарная выдача nonces.

## 14. Ошибки и safety state

Ошибка одного follower не должна останавливать остальных.

Минимальные runtime состояния приложения:

```text
RUNNING
PAUSED
DEGRADED
```

Отдельно должен существовать флаг/политика `allow_new_entries`.

`Pause` означает: не открывать и не увеличивать позиции, но не закрывать существующие автоматически.

`Emergency close` — отдельное явное действие admin control-core, а не
пользовательская операция и не побочный эффект pause.

Stage 2 control publication использует два уровня serialization:

- account mutation lock для risk/resume/replacement/disconnect и internal
  per-account emergency;
- global mutation lock для global pause/resume/admin emergency и финальной публикации
  account resume.

Global pause сначала персистит `allow_new_entries=false`, закрывает и осушает
generation admission, затем открывает его специальным seam, который никогда
не записывает `true`: reductions/close/protection продолжаются без окна для
entry. Destructive confirmation привязано к user/action/account, TTL и версии
credentials; replacement делает старую кнопку недействительной.

## 15. Тестовая стратегия

Главный приоритет тестов:

1. `risk/` — pure unit tests;
2. `planner/` — actual → target → exact actions;
3. replay реальных testnet WS payloads;
4. integration через fake Aster adapter;
5. signed testnet scenarios отдельным test marker.

Особенно важный инвариант:

```text
reconcile()
reconcile()
reconcile()

если actual == target → новых команд нет
```

## 16. Этапы развития без смены архитектуры

Торговое ядро едино для всех этапов; Telegram, CLI и Builder onboarding — внешние интерфейсы вокруг него (§1, §4). Risk, planner, reconciler и master flow между этапами не меняются.

- Этап 1 — backend/testnet: `domain aster master risk trading storage credentials cli`; master + testnet followers без Telegram.
- Этап 2 — Telegram: + `telegram notifications onboarding/direct application`. Один asyncio-процесс, long polling, SQLite WAL. Фактический контур: captcha + durable access approval, direct V3 onboarding с typed errors, dashboard/status/risk/notifications/account, admin controls, pause/resume, symbol leverage, notification outbox. Пользователь меняет только риск на Stop; `marginType` этап не меняет.
- Этап 3 — Aster Code / Builder: + `onboarding/builder`, `miniapp` (aiohttp-транспорт под `/miniapp/`: 21 POST `/miniapp/api/*` + собранный Vite-бандл; frontend в `miniapp/`: 4-табовый AppShell, flow SessionGate/BuilderWizard/Recovery/Success). Builder metadata передаётся в Aster requests вместе с ордерами; account-specific `builder + feeRate` — через `BuilderAttributionProvider` (journal payload от attribution не зависит). Транспортные хендлеры знают только application use cases и typed public codes.

Детали исполнения и acceptance — в `docs/reports/`: Stage 2 — `STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md`; Stage 3 Part 1 — `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md`; Part 2 — `STAGE3_PART2_PROGRESS_2026-09.md`.

## 17. Filesystem tree

```text
copytradeaster/
│
├── pyproject.toml
├── AGENTS.md                       # правила работы агентов с репо
├── miniapp/                        # Mini App frontend (Vite+React+TS, Stage 3: 4-tab shell)
│   ├── src/screens/                #   Home, Positions, Activity, Settings; nested Risk, Notifications, SystemStatus, AccountBuilder, Security, Help; flow SessionGate, BuilderWizard/Recovery, SuccessScreen
│   ├── src/wallet.ts               #   injected EIP-1193 + WalletConnect adapter
│   └── dist/                       #   собранный бандл (раздаёт бот, git-ignored)
├── scripts/                        # operational scripts (tunnel, mini app preview, load tests)
├── docs/                           # документация; карта — docs/AGENTS.md
│   ├── AGENTS.md                   # карта доков: что где лежит и когда читать
│   ├── ARCHITECTURE.md             # этот файл
│   ├── DEV_PLAN_SKELETON.md        # roadmap + чеклисты этапов
│   ├── ENGINE_TEST_STATUS_2026-08.md  # ЕДИНЫЙ живой статус движка
│   ├── ASTER_API_RESEARCH_2026-08.md  # справочник по V3 API
│   ├── reports/                    # исторические отчёты кампаний и журналы прогонов
│   ├── reviews/                    # дословные тексты внешних ревью (не редактируются)
│   └── history/                    # выполненный план Stage 1, исходный брифинг
│
├── src/
│   └── copytradeaster/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py                 # composition root / startup (§12 boot order)
│       ├── config.py              # Settings (.env)
│       ├── logging_utils.py
│       │
│       ├── domain/
│       │   └── models.py          # основные внутренние модели (PositionKey,
│       │                           #   TargetPosition, ExecutionAction, ...)
│       │
│       ├── aster/                  # адаптер Aster V3, без copytrade-логики
│       │   ├── trading_api.py     # account/trading REST (AsterClient)
│       │   ├── management_api.py  # Agent / Builder management
│       │   ├── user_stream.py     # listenKey + WebSocket (UserStream)
│       │   ├── signing.py         # EIP-712 trading/registration contexts
│       │   ├── models.py          # Aster DTO
│       │   ├── ports.py           # exchange-порт для тестов
│       │   ├── rate_limits.py     # weighted limits
│       │   └── shadow.py          # load-only SHADOW adapter (без кредов)
│       │
│       ├── master/
│       │   └── projector.py       # MasterProjector: raw WS -> MasterState +
│       │                          #   deltas, episode lifecycle, rebuild_full
│       │
│       ├── risk/
│       │   └── engine.py          # entry_quantity / scaled_reduce / post_fill_cap
│       │
│       ├── trading/                # прикладное ядро
│       │   ├── coordinator.py     # master delta -> targets per follower
│       │   ├── planner.py         # actual vs target -> minimal plan
│       │   ├── executor.py        # durable command execution + retry/recovery
│       │   ├── reconciler.py      # periodic full sweep
│       │   ├── leverage.py        # LeverageManager: RAM candidate catalog + confirmed state
│       │   ├── builder_attribution.py # Builder fee attribution (DIRECT/VALID/UNAVAILABLE)
│       │   └── registry.py        # FollowerRegistry: dynamic follower runtime
│       ├── storage/                # SQLite: миграции + репозитории
│       │   ├── database.py        # aiosqlite, WAL, write_lock, MIGRATIONS
│       │   └── repositories.py    # CopyTradeRepo / SyncBasisRepo / CommandRepo ...
│       │
│       ├── credentials/
│       │   ├── service.py         # SqliteCredentials (AESGCM-v1)
│       │   └── crypto.py
│       │
│       ├── notifications/
│       │   ├── console.py         # локальный structured sink
│       │   ├── formatter.py       # trader cards formatting (format_confirmed_action, format_skip_reason)
│       │   ├── models.py          # NotificationEvent, NotificationRecipient, NotificationFilter
│       │   ├── protocol.py        # NotificationRouter protocol
│       │   ├── router.py          # owner/admin routing → durable outbox
│       │   └── worker.py          # bounded Telegram delivery/retry
│       │
│       ├── onboarding/
│       │   ├── builder.py         # 4-state Builder onboarding (PREPARED..COMMITTED)
│       │   ├── direct.py          # signed V3 preflight, testnet-only
│       │   ├── errors.py          # typed OnboardingErrorCode / OnboardingError
│       │   ├── models.py          # DirectOnboardingRequest / EphemeralKeyProvider / AgentValidation
│       │   └── providers.py       # DirectKeyProvider
│       │
│       ├── application/
│       │   ├── service.py         # shared Bot/Mini App use cases
│       │   ├── adapters.py        # concrete App/SQLite/Aster boundaries
│       │   ├── control.py         # common emergency workflow
│       │   ├── engine.py          # Stage 2 process lifecycle
│       │   └── models.py          # transport-neutral Stage 2 DTO/errors
│       │
│       ├── telegram/
│       │   ├── runtime.py         # aiogram dispatcher/polling lifecycle
│       │   ├── menu.py            # inline presenters/typed callbacks
│       │   ├── flow.py            # non-secret onboarding FSM
│       │   ├── access.py          # captcha challenges and access request store
│       │   ├── middleware.py      # authorization and activity tracker middleware
│       │   └── routers/           # navigation/settings/admin handlers (common, settings, access)
│       ├── miniapp/
│       │   └── server.py          # Mini App aiohttp transport: HMAC initData сессия,
│       │                          #   builder/dashboard/pause/resume/confirmation, client-log,
│       │                          #   static /miniapp/ (Vite bundle)
│       └── cli/
│           └── main.py            # run/run-bot/import/admin commands
│
├── tests/
│   ├── unit/                      # risk/planner/storage/domain
│   ├── integration/               # FakeAster e2e через реальное ядро
│   ├── testnet/                   # signed scenarios (маркер `testnet`, CT_TESTNET=1)
│   └── replay/                    # replay захваченных стримов
│
├── scripts/                        # operational scripts (hybrid_load_test.py, ...)
└── data/                           # SQLite БД и артефакты load-прогонов (git-ignored)
```

Tree отражает фактическую структуру. Отдельного `master/observer.py` нет:
граница `UserStream` + `MasterProjector` — достаточная, механическое выделение
файла архитектуру не меняет.

## 18. Архитектурные правила проекта

1. Trading core не зависит от Telegram или Builder onboarding.
2. Aster payloads не протекают по всему приложению: они нормализуются на границе `aster/master`.
3. Risk engine не делает I/O.
4. Planner не вызывает Aster API.
5. Executor не решает бизнес-вопрос «сколько нужно купить».
6. Финансовые write-команды имеют persistent journal и idempotency key.
7. Reconciliation — нормальный рабочий механизм, а не только аварийный recovery.
8. Ошибка одного follower изолируется от остальных.
9. Сначала простой модуль; новая абстракция появляется только когда есть минимум две реальные реализации/причины для неё.
10. Не создавать God class, но и не дробить одну цельную обязанность на множество микросервисов/микромодулей.

Эта архитектура рассчитана на заявленный масштаб 30–100 followers и должна оставаться основной до появления измеримой причины усложнять инфраструктуру.
