# Архитектура copytradeaster

Архитектура покрывает testnet-движок, Telegram-бота и Aster Code / Builder в одном процессе. Узкие места (порядок WS-событий, partial fill, ручная перестановка OCO/OTOCO) закрыты инвариантами ниже; подтверждение — signed testnet-прогоны (сводка в `ENGINE_TEST_STATUS_2026-08.md`).

Торговое ядро едино для всех этапов: testnet-движок, Telegram-бот и Aster Code / Builder — внешние интерфейсы вокруг него.

Исходные условия:

- один разработчик + LLM;
- один master account;
- текущий кап: 10 followers (`max_followers` / `CT_MAX_FOLLOWERS`, fail-closed); 30–100 — целевой масштаб архитектуры, не действующий кап;
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
- Nonce signer-ключа персистентен: хранится в storage (`NonceStore.next_nonce`, `storage/repositories.py`), инкрементируется и сохраняется ДО отправки подписи; после рестарта восстанавливается из БД, а не из памяти.
- При retry после 503/504/timeout (`MAX_ATTEMPTS=3`) сначала проверяется фактическое состояние биржи по `clientOrderId`: `query_order` (текущий ордер), затем `query_order_history` (`allOrders`, до 3 попыток) плюс дельта `positionRisk` для MARKET; `open_orders` используется только для confirm условных `PLACE_STOP`/`PLACE_TP`. Повторная отправка идёт с новым `clientOrderId` (`…r{attempt}`) и свежим nonce через обычный signed-запрос.

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

Runtime leverage follower управляется `LeverageManager`: catalog из signed
`leverageBracket` задаёт ceiling, authoritative `positionRisk` — текущую
конфигурацию. Любое положительное подтверждённое плечо не выше catalog/operator
ceilings (≤) считается готовым для текущего поколения.
до записи и не повышает stop-driven downshift обратно до максимума, в том
числе после рестарта. Нечитаемая конфигурация блокирует подготовку. Записи при
необходимом понижении ограничены semaphore/pacing; детерминированный отказ
допускает account-bracket fallback в пределах ceilings.

Coordinator держит account mutation lock от восстановления in-flight и снимка
до исполнения и post-fill convergence; тот же lock использует maintenance.
Повторный CONFIRMED SET требует текущего positionRisk-proof, исторического
статуса недостаточно. Planner создаёт один SET на symbol, выбирая минимум
целевых плеч обоих HEDGE legs. Дубли action_id запрещены. Оба увеличения
зависят от общего SET; закрытия и защита уже существующей позиции независимы.

`risk/account_budget.py` резервирует stop-risk и margin сразу для всех
увеличений аккаунта. `CT_MAX_TOTAL_RISK_PCT` (по умолчанию 0.10) ограничивает
суммарный mark-to-stop риск относительно USDT equity; для margin доступно не более
80% available (cap, не остаток). Ноги не неттируются; неподтверждённые закрытия/ужесточения
стопов не освобождают резерв, отсутствие наблюдаемого стопа блокирует рост.
Fee buffer в суммарном резерве считается от цены единицы. Это консервативный
stop-loss budget, не модель гарантированного исполнения или cross-liquidation.

Направление оценивается по текущему mark (LONG опасен при liq на/ниже mark, SHORT — на/выше; равенство опасно; shared HEDGE-quote относится к противоположной ноге). Нарушение — против стопа с буфером: LONG liq >= stop×(1−0.005), SHORT liq <= stop×(1+0.005); входной гейт учитывает MMR-запас 0.01.

Ядро не меняет `marginType` перед входом: текущий режим аккаунта сохраняется
без скрытой подмены; торговых вызовов `set_margin_type` из ядра нет, отдельной
isolated-проверки в коде нет.

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

History projection использует отдельный durable revision contract. Импорт fills и
изменение complete coverage в одной транзакции повышают `facts_revision` и держат
самый ранний не опубликованный `dirty_from_ms`. Публикация episodes/events/boundary
делает CAS по ожидаемой revision и версии boundary в той же SQLite-транзакции;
dirty очищается только когда принятый cutoff покрывает текущий complete frontier.
Open episode при continuation replay-ится от принятого origin, а уже выданный
`public_ref` разрешается как identity, но никогда повторно не используется как
новый anchor. Это сохраняет одну карточку/аллокации через incremental capture,
restart и late backfill без отдельного accumulator-сервиса.

Финальный hybrid gate: 30 slots (3 REAL + 27 SHADOW), 100 циклов, 12 302
подтверждённые команды (пост-фикс rerun 20260823: 12 207/12 207);
подробности в `docs/reports/HYBRID_LOAD_TEST_REPORT_2026-08.md`.

### `credentials/`

Шифрование и получение signer credentials — для путей `onboarding/direct` и `onboarding/builder` (оба через `credentials/` + OnboardingStore; builder-commit несёт `identity_source=builder` со snapshot).

Приватные signer keys в БД хранятся только зашифрованными (AESGCM, ключ вне БД:
`CT_ENCRYPTION_KEY`). Реализация: `credentials/service.py` + `credentials/crypto.py`.

Асимметрия Builder-пути (этап 3): при комбинированном ApproveAgent (builder-поля внутри одного user-signed POST `/fapi/v3/approveAgent`, `signature_plan=combined_approve_agent`; отдельного POST approveBuilder в Mini App-флоу нет) сгенерированный signer-ключ шифруется и персистится в `credentials/` после успешного commit, а главный приватный ключ пользователя в backend никогда не передаётся и не сохраняется. До момента commit промежуточное состояние онбординга хранится в таблице `builder_onboarding_attempts` (состояния: `PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`).

`APPROVAL_SENT` является fail-closed состоянием неизвестного результата
удалённого `approveAgent`: transport/5xx/unknown exchange response не откатывает
его в `PREPARED` и не разрешает слепой повтор user-signed POST; исключение — истечение `approval_deadline_ms`: неподтверждённый Agent возвращает просроченный `APPROVAL_SENT` в `PREPARED`.
проверяет Agent идемпотентным signed GET. Mini App может продолжить без второй
wallet signature только server-owned `APPROVAL_SENT`/`APPROVED`; `PREPARED`
всегда требует подпись. После paused account commit attempt переводится в
`COMMITTED`, а затем очищается после runtime activation либо лениво при
идемпотентном recovery.

Durable account identity (P2.6): `accounts` несут `identity_source`
(`direct|builder`) + nullable Builder snapshot (`builder_address`,
`builder_name`, `builder_max_fee_rate`, `builder_agent_expires_at_ms`,
`builder_verified_at`). Snapshot — last-known evidence, не proof: verified
только последовательными signed `GET /agent` (current signer, допустимые
permissions) + exact-match `GET /builder` (`userAddress`, `builderAddress`,
`maxFeeRate` == cap) с race-check по `account_version` перед commit.
`identity_source=builder` + snapshot); direct/replacement/disconnect
атомарно чистят proof (`identity_source=direct`, поля NULL). Race в reauthorization исключён
`account_version` (mismatch → `IDENTITY_CHANGED` без commit); первичный submit защищён конфликтом владельца. Disconnect —
local-only: remote Agent/Builder revoke не выполняется и не заявляется.
Reauthorization — same-signer reuse Part 1 wallet/EIP-712 flow, без ротации
signer; после неё account остаётся paused.

### `onboarding/`

Способ получения `TradingIdentity`. Два независимых адаптера: `direct`
(прямой V3 набор) и `builder` (комбинированный ApproveAgent). Direct Stage 2
принимается только `onboarding/direct.py`: локальная
проверка адресов/key pair, подписанный `GET /fapi/v3/agent`,
balance/position/open-order preflight и atomic paused commit через
`application/adapters.py`. Исполнение ордеров от способа не зависит, но
attribution/отображение знают `identity_source` (`trading/builder_attribution`).

### `notifications/`

Структурированные события не отправляются в Telegram из торговой операции.
`NotificationRouter` быстро и идемпотентно пишет recipient-specific rows в
SQLite outbox, а bounded `TelegramOutboxWorker` доставляет их независимо с
lease/retry/backoff и изоляцией Telegram 403. Console sink сохраняется.
S3 production factory включает durable user-event consumer: per-scope cursor
и outbox commit атомарны, первый enable записывает rollout floor. Boot и
периодический sweep используют один consumer; выключенные уведомления не
останавливают историю. Mini App/menu/push используют `format_user_event`; строки
пропуска входа приходят в него кодами движка (`SkipReason`) и рендерятся русской
копией — неизвестный код не печатается вовсе, а сырьё остаётся в facts.
Command confirmations в S3 не доставляются как факты позиции. Keyboard строится
из configured HTTPS Mini App URL и owned opaque trade ref, только private chat при включённом `trade_links_enabled`, action `open_trade` и непустом ref.

Telegram handlers никогда напрямую не вызывают торговые endpoints Aster.

Direct onboarding остаётся двухфазным: signed preflight обязан завершиться до
любого credential commit; activation после commit публикует только paused
account. `UserApplicationService` не сворачивает `OnboardingError` в общий
`ValidationError`: наружу передаётся только typed
`OnboardingApplicationError.code` (`OnboardingErrorCode`); инварианты результата/контекста
дополнительно охраняются `ValidationError`. Exchange status,

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

Повторный `reconcile()` при совпадающем состоянии возвращает **0 оценённых
действий**. Recompute возвращает фактический размер оценённого плана вместе
с safety-действиями; retry/blocked не превращается в ноль из-за reuse строки
или размера `recent()`. Неоценённая generation даёт -1. Snapshot scope локален
вызову через ContextVar. Degraded снимает только полный authoritative отчёт App READY
(master-boundary + stream) для всех followers: без sweep-failures, с нулевыми действиями,
чистым master (без dirty), пустым in-flight, готовым leverage (is_ready, pending=0) и в пределах follower-cap; scoped sweep и не-READY права не дают.

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
открывает новый с колонкой `copy_trades.direction = ±1` (миграция #14).
Snapshot-sync `rebuild_full` детектит рассинхрон направления после офлайна и
делает тот же close+open. Новый эпизод автоматически снимает sticky-маркеры,
поэтому dust-flat одного направления не блокирует вход в противоположном.

Reconciliation запускается не только после ошибок, а является нормальной частью работы:

- startup;
- master event;
- завершение follower command;
- reconnect/boundary;
- периодический sweep;
- ручной admin/CLI-триггер.
UNKNOWN разрешается резолвером внутри submit и доводится следующим sweep; отдельного UNKNOWN-триггера нет.

## 7. Execution journal

Критичные торговые команды нельзя хранить только в `asyncio.Queue`.

Минимальная persistent-модель команды:

```text
command_key (PK)
account_id
trade_id
kind
payload (JSON)
recovery_context
priority
status
attempts
last_error
client_order_id
operation_id
created_at/updated_at
```

Минимальные статусы:

```text
PENDING → SENDING → SUBMITTED → CONFIRMED
                         ↘ UNKNOWN
                         ↘ FAILED
```

`503/504`, timeout либо коды `-1000/-1006/-1007` после отправки финансовой команды трактуется как `UNKNOWN`, а не как
гарантированный fail. Для `MARKET_OPEN`/`MARKET_CLOSE` Executor до перехода
`PENDING → SENDING` сохраняет в `recovery_context` symbol/leg, точную Decimal-позицию
до mutation и ожидаемый signed delta. Runtime, повторный submit и boot используют один
resolver с тремя исходами:

- `FOUND` — исходный COID имеет подтверждённый результат (для MARKET — terminal),
  либо authoritative `positionRisk` показывает полный ожидаемый signed delta;
- `PROVEN_NOT_EXECUTED` — исходный COID отсутствует в трёх полных чтениях
  `allOrders`, а соответствующая position leg во всех чтениях не изменилась;
- `UNRESOLVED` — history/position недоступны, history page насыщена 1000 строками,
  recovery context отсутствует/повреждён или position delta противоречив.

Только `PROVEN_NOT_EXECUTED` разрешает новый COID. `UNRESOLVED` остаётся
non-terminal, ставит degraded и блокирует новую economic mutation. Current-order query
служит быстрым positive lookup, но его `None` не является negative proof.
Partial progress без terminal history остаётся UNKNOWN. Новая qty/revision
не обходит незавершённую экспозицию аккаунта; reductions/protection остаются
доступны. Перед следующей generation resolver выполняется до нового снимка,
чтобы residual рассчитывался по актуальной позиции.

Собственный `command_key` является долговременной идемпотентностью сервиса; `newClientOrderId` используется как внешний correlation id, но не заменяет внутренний журнал. `BLOCKED` — ephemeral-результат исполнителя для действий с упавшим prerequisite (в журнале не персистится; шесть статусов выше — весь CHECK).

### P2.7 Builder attribution (account-specific `builder + feeRate`)

Outbound Builder-атрибуция применяется после journal identity,
непосредственно при построении `PlaceOrderRequest` (`builder`/`feeRate`,
Decimal, cap `0.001`, optional `time_in_force` — планировщик/исполнитель сейчас не выставляют): `command_key`, journal payload и operation ID от
metadata не зависят. `BuilderAttributionProvider` (composition boundary,
`App`) различает DIRECT / BUILDER_VALID / BUILDER_UNAVAILABLE по durable
P2.6 proof + current signer + Agent permissions/actual expiry + config match (permissions строго tri-state: read/perp ровно True, withdraw ровно False; malformed fail closed);
:UI TTL на execution не влияет. Snapshot решения берётся лениво под per-account
mutation lock, один на весь `Executor.submit` (кэш на план); персистентность команды — insert/set_status, а send→query→retry с новым COID — контур разрешения UNKNOWN. Safety: entry при UNAVAILABLE fail-closed
до HTTP, close/Stop/TP — fallback без полей + degraded signal,
cancel/leverage не блокируются.

## 8. Конкурентность и масштаб (v1 launch cap: 10 followers)

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

Это упрощает nonce, order sequencing и recovery, не создавая заметного bottleneck при ≤10 followers (v1 launch cap `max_followers=10`); масштаб 30–100 — только после нагрузочных замеров и поднятия cap.

Общий weighted rate limiter находится в `aster/` и учитывает фактические веса Aster endpoints.

## 9. Master и follower streams

Master user-data WebSocket обязателен.

`UserStream` запускает socket receive до post-connect callback: frames, уже
накопившиеся у Aster, попадают в bounded App queue параллельно REST
boundary. Disconnect callback вызывается до завершения медленного
post-connect callback, чтобы он не мог открыть entry admission для уже
мёртвого socket.

Authoritative master boundary имеет process-local generation. `MasterProjector`
публикует новую generation только после всех REST reads и durable episode
sync. Envelope, полученный до этого cutover, audit-персистится, но не
мутирует новый state. Delayed event той же generation сверяет exchange
`E` с cutoff в Aster server-time; `E <= cutoff` не считается доказанно
post-snapshot, отбрасывается и делает master dirty.

Queue overflow и envelope с monotonic age `> 5s` закрывают process-local
entry admission и запрашивают coalesced REST recovery. Не более трёх
непрерывных boundary passes делаются за один вызов; при непрерывном
потоке система остаётся close-only/dirty и повторяет recovery по
периодическому sweep, не крутя бесконечный REST-loop.

Для торговой корректности followers по-прежнему достаточно `REST confirmation +
reconciliation`: private WebSocket никогда не становится authoritative входом
planner/executor. Для live presentation Mini App процесс дополнительно держит
отдельный user-data stream каждого REAL follower. `ACCOUNT_UPDATE`,
`ORDER_TRADE_UPDATE`, `ACCOUNT_CONFIG_UPDATE` и `MARGIN_CALL` — только
low-latency hints для coalesced REST snapshot. SHADOW follower внешнего socket не
создаёт.

На reconnect follower сначала получает новый REST boundary и только после этого
публикуется как `live`. На disconnect торговый state не переписывается, а Mini App
сохраняет последний подтверждённый снимок с presentation-status
`reconnecting/stale`. Public mark-price stream общий для symbol и меняет только
presentation quote/PnL; он не влияет на entry admission и финансовые команды.

## 10. Stop / TP и Strategy Orders

Внутреннее состояние не должно зависеть от того, как master представил сделку на Aster.

Master может использовать:

- standalone STOP/TP;
- ручной UI (позиция без защитных ордеров).

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
resolve SENDING/SUBMITTED/UNKNOWN commands
 ↓
master REST snapshot
 ↓
seed leverage candidate catalog (fail-closed if unavailable)
 ↓
startup follower reconciliation (entry gate closed; close/reduce/protect only)
 ↓
start ingest + master WS receive
 ↓
post-connect master REST snapshot
 ↓
close-only follower reconciliation
 ↓
final master REST snapshot
 ↓
open process-local entry admission + entry reconciliation
 ↓
READY
```

Новые follower entries разрешаются только при одновременных process-local admission (взводится вместе с `READY`), live master stream, clean projector и соблюдении follower-cap (`len(real_followers) <= max_followers`). Если WS недоступен, процесс остаётся живым
для authoritative REST close/reduce/protection sweeps, но не публикует READY и не
открывает exposure. Disconnect, dirty projection, continuous boundary events или
in-flight commands закрывают admission, но не блокируют risk-reducing actions.

Boot recovery применяет тот же
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
handlers → остановить user-event sweep → осушить outbox при живых Bot/DB
→ остановить Mini App/history worker → `App.stop()` → закрыть Bot session.
S3 production восстанавливает user events после отдельного rollout floor и
запускает recurring sweep раз в 2 секунды. Explicit delivery=False seam
сохраняет прежний command recovery без user-event scan/cursor advance.

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
стабильные `user_id`/opaque `account_id`, `mode` (`connect`/`replace`/`miniapp_recover_runtime`/`builder_prepare`/`builder_submit`/`builder_resume_attempt`),
`event` (`attempt`/`success`/`recovered`/`rejected`), `result`, `paused` и
стабильная typed classification. `attempt`/`success`/`recovered` имеют INFO,
`rejected` — WARNING. Wallet/API-wallet addresses, private keys, token,
signature, signed URL/query и raw exchange/exception payload не являются
аргументами этих logging calls. Для `attempt` и `rejected` `paused` обычно пишется как
`unknown`, потому что replace может обращаться к уже существующему
account; исключение — `miniapp_recover_runtime`, чей `attempt` подтверждает `paused=true`.
Завершённые `success`/`recovered` подтверждают `paused=true`; `success` подготовки
(`builder_prepare`/`prepared`) пишет `unknown`, потому что пауза ещё не зафиксирована. Handler

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
  эпизода, `0` = не установлена, миграция #14);
- `order_links` — master ↔ follower order mapping;
- `execution_commands` — durable execution journal/queue; additive migration #59
  добавляет `recovery_context` с pre-mutation evidence для MARKET recovery.
- `telegram_users` — private-chat allowlist и единственный owner account;
  admin — дополнительная capability immutable ID из env, а не взаимоисключающая
  роль, поэтому один operator ID может одновременно иметь owner account;
  `access_enabled` отзывает только вход в UI и не останавливает торговый runtime;
- `telegram_access_requests` — audit trail заявок первичного доступа; уникальный
  `PENDING` на Telegram user, idempotent decision и `decided_by` configured admin;
- `agent_validations` — последняя безопасная signed Agent snapshot;
- `notification_outbox` — recipient-specific delivery journal без secrets.
- `follower_fills`, `follower_cash_flows`, `history_sync_state` — факты и
  coverage импорта account-owned истории;
- `follower_episode_*`, `history_scope_state`,
  `history_projection_boundaries` — revisioned projection сделок, stable refs,
  aliases/splits и durable replay boundaries;
- `user_activity_events`, `user_event_delivery_state` — immutable
  пользовательская лента и per-network delivery cursor.

В реализации также есть `app_state` — персистентные runtime флаги (`ready`,
`runtime_id`, `started_at`, `heartbeat_at`, `allow_new_entries`,
`emergency_active`, `emergency_operation_id`, `degraded`, `degraded_active`,
`degraded_reason`); `sync_basis` — per-follower sizing reference с
`zero_target`-маркером намеренного dust-flat и `issued_target` для
идемпотентности ревизий; `nonce_store` — атомарная выдача nonces.

## 14. Ошибки и safety state

Ошибка одного follower не должна останавливать остальных.

Минимальные состояния: `SystemState` (`READY`/`STALE`/`DEGRADED`/`UNKNOWN`) и
`CopyState` (`ACTIVE`/`PAUSED`/`DEGRADED`/`BLOCKED`, плюс `EMPTY` без счёта);
CLI `status` показывает `ready`/`stale`/`stopped`.

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
- Этап 3 — Aster Code / Builder: + `onboarding/builder`, `history`, `miniapp`
  (aiohttp-транспорт под `/miniapp/`: 27 POST `/miniapp/api/*` + собранный
  Vite-бандл; frontend в `miniapp/`: 4-табовый AppShell, flow
  SessionGate/BuilderWizard/Recovery/Success и account-owned trade history).
  Builder metadata передаётся в Aster requests вместе с ордерами;
  account-specific `builder + feeRate` — через `BuilderAttributionProvider`
  (journal payload от attribution не зависит). Транспортные хендлеры знают
  только application use cases и typed public codes.

Детали исполнения и acceptance — в `docs/reports/`: Stage 2 — `STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md`; Stage 3 Part 1 — `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md`; Part 2 — `STAGE3_PART2_PROGRESS_2026-09.md`.

## 17. Filesystem tree

```text
copytradeaster/
├── pyproject.toml
├── AGENTS.md                       # правила работы агентов с репо
├── miniapp/                        # Mini App frontend (Vite+React+TS, Stage 3: 4-tab shell)
├── scripts/                        # operational scripts + doc-budgets.json (карта: scripts/AGENTS.md)
├── docs/                           # документация; карта — docs/AGENTS.md
├── src/copytradeaster/             # backend: app.py / config.py + 14 пакетов
│                                   # (domain, aster, master, risk, trading, history,
│                                   #  storage, credentials, notifications, onboarding,
│                                   #  application, telegram, miniapp, cli)
├── tests/                          # unit/ + integration/ + testnet/ + replay/
└── data/                           # SQLite БД и артефакты load-прогонов (git-ignored)
```

Tree отражает фактическую структуру на уровне пакетов. Назначение каждого файла — в корневом `AGENTS.md` (Key Directories) и §4 выше. Отдельного `master/observer.py` нет:
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

## 19. Куда класть новую возможность (Footprint Ladder)

Новая capability кладётся на высшую подходящую ступень — выбирай первую сверху, которая корректно решает задачу:

1. Расширь существующий код/модуль — вариация имеющегося, без новых границ и протоколов.
2. Новый скрипт в `scripts/` — операция, выражаемая процедурой (карта: `scripts/AGENTS.md`).
3. Новый модуль в существующем пакете — новая обязанность внутри известной границы (§4).
4. Новый пакет `src/copytradeaster/<name>/` — новая граница с протоколом; карта доков обновляется в том же коммите.
5. Новый процесс/инфраструктура — только при измеренной причине (§1, §8); текущий масштаб держится на одном процессе.

## 20. Live presentation Mini App

Live mode остаётся внутри существующего asyncio-процесса и состоит из четырёх
границ:

1. process-local `FollowerSnapshotStore` хранит только последний нормализованный
   `AccountState`, UTC `captured_at` и monotonic revision на account;
2. follower private streams дают быстрый сигнал переснять REST state, но никогда
   не мутируют trading target напрямую;
3. `MiniAppLiveHub` публикует authenticated browser-клиентам только безопасные
   resource invalidations/health/revisions через bounded latest-wins queues;
4. frontend live client перечитывает существующие application DTO слабым запросом
   из общего cache и сохраняет last-good DTO во время reconnect.

WebSocket handshake не передаёт Telegram `initData` в URL. Сначала
аутентифицированный POST выдаёт short-lived single-use opaque ticket, затем GET
upgrade атомарно его поглощает. User/account scope определяется сервером.

Порядок freshness:

```text
private event -> coalesced REST snapshot -> store revision -> live invalidation
public mark event -------------------------> quote revision -> live invalidation
browser reconnect -> REST resnapshot ------> LIVE
```

HTTP resource generations и live revisions монотонны: duplicate/out-of-order
события игнорируются, а старый request не может затереть более новую публикацию.
`hello` всегда отправляется первым и задаёт baseline revision. Bounded latest-wins
queue может сознательно выбросить промежуточные события под backpressure; поэтому
любой revision gap (или reconnect `hello` с более новым revision) означает full
invalidation live-backed DTO, а не попытку угадать потерянные resource keys.
Server посылает application heartbeat каждые 10 секунд; видимый WebView считает
канал потерянным после 30 секунд без frame, тогда как hidden WebView heartbeat
timeout не использует, чтобы background throttling не создавал ложный stale.
Revision counter живёт в одном процессе, поэтому каждое событие и стартовый
`hello` штампуются per-process `epoch`: после рестарта backend клиент видит смену
epoch и пере-baseline'ит watermark с полной reconciliation вместо того, чтобы
отбросить более низкие revision как out-of-order и залипнуть в `stale`.
При разрыве UI не очищает данные. Rail сверху существует ровно для одного
случая — канал молчит дольше своего deadline и данные на экране перестали
обновляться; тогда показывается `Нет связи · N сек назад`. Пока канал здоров,
rail не рендерится (`LIVE`, `connecting` и `Восстанавливаем связь…` — это работа
транспорта, а не информация для пользователя; короткое переподключение чинится в
фоне и не видно вообще).
Ручного обновления в продукте не осталось ни одной кнопкой: routine re-read (live
invalidation, TTL, очередной reconcile) никогда не рисуется как «данные
устарели», упавшее чтение перечитывается само с расширяющимся backoff, а
единственный ручной путь — pull-to-refresh вниз с верха скролла, который делает
сильное чтение и сразу возвращает вертикальный жест Telegram. Один факт — один голос: пока rail сообщает о разрыве и последние
данные остаются на экране, экраны не повторяют ту же ошибку своей плашкой
(`railSpeaks`), а предупреждение о снимке показывается только когда сам движок
помечает снимок устаревшим; ошибка действия и состояние «данных ещё нет» никогда
не скрываются. Обратная связь по нажатию (`usePaging`) принадлежит нажатию: фоновая
перечитка не переключает кнопку в «Загружаем…» и не гасит её. Возраст серверного
вердикта (`useAgeSeconds`) продолжает идти, пока экран открыт, поэтому строка
«Проверка: HH:MM · N сек назад» не замирает на числе из ответа. Служебные
fail-closed окна движка (periodic master boundary) пользователю не показываются
как деградация: presentation-verdict отдаёт их как READY, пока окно короче
`ROUTINE_DEGRADED_GRACE_MS` (затянувшееся окно снова видно), тогда как admission
остаётся закрыт собственным рычагом. Давность доказательств (`Разрешения`)
считается возрастом строки, а не отказом: общий вердикт падает в STALE только
когда попытка проверки реально сорвалась. Explicit permission recheck
Agent/Builder остаётся отдельным security use case; экран Статуса один раз при
открытии перепроверяет права сам — в том числе когда доказательство просто старше
TTL проверки, а не помечено невалидным.

L4 — один shared presentation-стрим котировок на процесс. На Aster **testnet**
кластер mark-price поток молчит и через raw path, и через SUBSCRIBE-ack (кадров
нет; `aggTrade` на том же endpoint стримит), поэтому liveness объявляется только
после первого реального кадра, а котировки добирает bounded unsigned
`GET /fapi/v3/premiumIndex` (weight 10, Decimal, whitelist). Fallback простаивает,
пока стрим отдаёт кадры, и не обновляет REST `captured_at`: mark-тики влияют
только на presentation PnL, никогда на trading state.

Детальный план и acceptance:
`docs/reports/MINIAPP_LIVE_MODE_PLAN_2026-09.md`.
