# Архитектура copytradeaster

Статус: базовая архитектура проекта, 21.08.2026.

Этот документ фиксирует архитектуру сразу для всех текущих этапов: testnet-движок, Telegram-бот и Aster Code / Builder. Цель — не менять торговое ядро при переходе между этапами.

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

Telegram, CLI и будущий Builder onboarding — внешние интерфейсы вокруг одного торгового ядра.

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
│ Master Observer│
└───────┬────────┘
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

Адаптер к Aster V3.

Отвечает за:

- REST;
- user-data WebSocket;
- EIP-712 signing;
- nonce;
- разбор ошибок;
- rate limits;
- Agent / Builder management.

Не содержит copytrade- и risk-логики.

Trading API и Agent/Builder management лучше держать раздельно, поскольку у них разные обязанности и signing contexts.

### `master/`

Получает события master account и строит нормализованное состояние.

`observer` отвечает только за соединение, listenKey, keepalive, reconnect и получение raw events.

`projector` преобразует Aster payloads в внутреннее состояние проекта.

### `risk/`

Чистый risk engine.

Главная модель риска проекта:

```text
risk_budget = balance/equity base × risk_percent
quantity = функция(entry, stop, fees/slippage, exchange limits)
```

Плечо влияет на margin/liquidation, но не определяет денежный риск до Stop.

Risk engine должен возвращать целевое состояние/quantity, а не отправлять ордера.

### `trading/`

Главное прикладное ядро.

- `coordinator` — запускает пересчёт для затронутых followers;
- `planner` — сравнивает actual state с target state и строит минимальный набор действий;
- `executor` — выполняет подготовленные действия через Aster;
- `reconciler` — повторно сверяет фактическое состояние и исправляет расхождения.

### `storage/`

Хранение торгового состояния, журналов и mappings.

Для этапов 1–2 достаточно SQLite + WAL. PostgreSQL нужен только когда появится реальная причина разделять процессы или возрастёт нагрузка.

### `credentials/`

Шифрование и получение signer credentials.

Приватные signer keys в БД хранятся только зашифрованными. Master encryption key находится вне БД.

### `onboarding/`

Способ получения `TradingIdentity`.

Два независимых адаптера:

- `direct` — прямой V3 набор для testnet/MVP;
- `builder` — ApproveAgent + ApproveBuilder.

Trading core не должен знать, каким способом credentials появились.

### `telegram/`

Только пользовательский интерфейс:

- onboarding;
- настройки риска;
- enable/disable;
- статус;
- уведомления.

Telegram handlers никогда напрямую не вызывают торговые endpoints Aster.

## 5. Минимальные внутренние модели

Не требуется большая иерархия классов. На старте достаточно нескольких простых dataclass/Pydantic-моделей:

```text
PositionKey
PositionState
AccountState
RiskPolicy
TargetState
ExecutionAction
ExecutionPlan
TradingIdentity
```

Отдельно полезна внутренняя сущность `CopyTrade` / `trade_id`, чтобы отличать два последовательных торговых эпизода по одному символу после `close → reopen`.

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

`503` или timeout после отправки финансовой команды трактуется как `UNKNOWN`, а не как гарантированный fail. Перед retry сначала проверяется состояние биржи / clientOrderId.

Собственный `command_key` является долговременной идемпотентностью сервиса; `newClientOrderId` используется как внешний correlation id, но не заменяет внутренний журнал.

## 8. Конкурентность и масштаб 30–100 followers

Для текущего масштаба достаточно одного asyncio application process.

Модель выполнения:

```text
followers выполняются параллельно
один follower → последовательные mutation-команды
```

То есть операции разных пользователей могут идти конкурентно, но внутри одного follower используется один mutation lock/queue.

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

Рекомендуемые defaults:

- новая follower-позиция требует известного Stop;
- при слишком большом entry deviation сделка пропускается;
- после фактического follower fill риск пересчитывается;
- если риск выше допустимого — уменьшается quantity;
- Stop мастера не двигается искусственно ради сохранения quantity;
- если мастер расширил Stop — follower position при необходимости сокращается;
- если Stop стал ближе — автоматически наращивать позицию не нужно.

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
resolve UNKNOWN commands
 ↓
reconciliation
 ↓
READY
```

Только после `READY` разрешаются новые follower entries.

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
```

Назначение:

- `accounts` — master/followers;
- `credentials` — encrypted signer credentials;
- `copy_settings` — risk %, limits, enable/disable;
- `master_events` — raw event ledger для replay/recovery;
- `copy_trades` — внутренние торговые эпизоды;
- `order_links` — master ↔ follower order mapping;
- `execution_commands` — durable execution journal/queue.

Подробная схема появляется только по мере реализации конкретного этапа.

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

`Emergency close` — отдельное явное действие, а не побочный эффект pause.

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

### Этап 1 — backend/testnet

Реализуются:

```text
domain
aster
master
risk
trading
storage
credentials
cli
```

Master + несколько testnet followers, без Telegram.

### Этап 2 — Telegram

Добавляются:

```text
telegram
notifications
onboarding/direct
```

Trading core не меняется.

### Этап 3 — Aster Code / Builder

Добавляется:

```text
onboarding/builder
```

и builder metadata в Aster requests.

Risk, planner, reconciler и master flow не меняются.

## 17. Filesystem tree

```text
copytradeaster/
│
├── pyproject.toml
├── README.md
├── AGENTS.md
├── ARCHITECTURE.md
├── ASTER_API_RESEARCH_2026-08.md
├── DEV_PLAN_SKELETON.md
│
├── src/
│   └── copytradeaster/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py                 # composition root / startup
│       ├── config.py
│       │
│       ├── domain/
│       │   ├── __init__.py
│       │   └── models.py          # основные внутренние модели
│       │
│       ├── aster/
│       │   ├── __init__.py
│       │   ├── trading_api.py     # account/trading REST
│       │   ├── management_api.py  # Agent / Builder management
│       │   ├── user_stream.py     # listenKey + WebSocket
│       │   ├── signing.py
│       │   ├── models.py          # Aster DTO
│       │   └── rate_limits.py
│       │
│       ├── master/
│       │   ├── __init__.py
│       │   ├── observer.py
│       │   └── projector.py
│       │
│       ├── risk/
│       │   ├── __init__.py
│       │   └── engine.py
│       │
│       ├── trading/
│       │   ├── __init__.py
│       │   ├── coordinator.py
│       │   ├── planner.py
│       │   ├── executor.py
│       │   └── reconciler.py
│       │
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── database.py
│       │   ├── repositories.py
│       │   └── migrations/
│       │
│       ├── credentials/
│       │   ├── __init__.py
│       │   ├── service.py
│       │   └── crypto.py
│       │
│       ├── onboarding/
│       │   ├── __init__.py
│       │   ├── direct.py
│       │   └── builder.py
│       │
│       ├── telegram/
│       │   ├── __init__.py
│       │   ├── app.py
│       │   └── handlers.py
│       │
│       ├── notifications/
│       │   ├── __init__.py
│       │   └── service.py
│       │
│       └── cli/
│           ├── __init__.py
│           └── main.py
│
└── tests/
    ├── unit/
    ├── integration/
    ├── replay/
    │   └── fixtures/
    └── testnet/
```

Этот tree — целевая структура, а не требование создать все пустые файлы заранее. Модуль/файл добавляется тогда, когда появляется его реальная реализация.

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