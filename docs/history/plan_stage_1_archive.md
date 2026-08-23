# Этап 1 — торговое ядро копитрейда AsterDex (backend/testnet)

## Context

Реализовать этап 1 по `DEV_PLAN_SKELETON.md` §4 и `ARCHITECTURE.md` §16: полный backend без Telegram — стрим мастера (user-data WebSocket), риск-расчёт «% депозита на стоп → quantity», исполнение на аккаунтах-копирах (MARKET open/close/partial, STOP_MARKET/TAKE_PROFIT_MARKET, плечо), target-state reconciliation с durable-журналом команд, уведомления в консоль/лог, плюс builder-авторизации скриптом (approveAgent+approveBuilder, chainId 56) для всех 5 аккаунтов. Всё на futures testnet (`fapi.asterdex-testnet.com`, chainId 714). Кошельки уже есть в `wallet.txt`: `main` — торгующий мастер, `copyc1`–`copyc4` — копиры; пользователь дописывает в каждый блок строку `main privte key:` (ключ основного EVM-кошелька) для builder-прогона в самом конце этапа. Репозиторий пуст (только `.md`), код пишется с нуля в `src/copytradeaster/` по дереву из `ARCHITECTURE.md` §17. Требование пользователя: тщательная реализация с полным покрытием тестами (один жёсткий гейт покрытия 90%, матрица сценариев ниже); копируется только белый список символов (дефолт BTCUSDT, ETHUSDT).

План выполняется строго последовательно, сверху вниз: шаг 0 → шаг 5, внутри шага пункты по порядку. Подробные контракты (сигнатуры, DDL, payload'ы) зафиксированы ниже, чтобы исполнитель в свежей сессии не принимал дизайн-решений и ни о чём не договаривался в процессе. Использовать ли вспомогательные инструменты для ускорения — исполнитель решает сам; на корректность это не влияет.

---

## Шаг 0 — обязательное изучение документации (до первой строки кода)

Исполнитель ОБЯЗАН прочитать целиком все `.md` файлы корня репозитория до начала реализации. Они — источник требований и поведенческих правил; реализация им противоречить не может.

| Файл | Что это | Что извлечь для этапа 1 |
|---|---|---|
| `firstcontext.md` | Исходный бриф пользователя: копитрейд-бот на AsterDex; следить за мастером через API, копировать открытие/закрытие/перестановку SL/TP/сокращение позиции; размер позиции от «% депозита на стоп» + риск-ревард; Telegram как первый интерфейс | Список хотелок, с которыми сверяется каждая фича этапа 1 |
| `ASTER_API_RESEARCH_2026-08.md` | Главный фактологический отчёт по Aster V3: версия API, модель ключей user/signer + EIP-712, торговые эндпоинты, SL/TP и их перестановка, user-data WebSocket, расчёт позиции от риска на стоп, testnet/faucet, Builder/Aster Code | §4 формулы риска и post-fill алгоритм; §6 ограничения (REQUEST_WEIGHT 2400/min, ORDERS 1200/min и 300/10s, nonce, 503=UNKNOWN); §8 signed-чеклист — источник сценариев testnet-тестов |
| `ARCHITECTURE.md` | Утверждённая целевая архитектура: modular monolith, копирование состояния а не команд, границы модулей, durable execution journal, дерево `src/copytradeaster/` | §4 границы модулей; §6 принципы planner/reconciliation; §7 статусы команд PENDING→SENDING→SUBMITTED→CONFIRMED/UNKNOWN/FAILED; §11 базовые риск-политики; §12 boot-порядок; §16 состав этапа 1; §17 раскладка файлов; §18 архитектурные правила (risk без I/O, planner без вызовов Aster, executor не решает «сколько купить») |
| `DEV_PLAN_SKELETON.md` | Каркас разработки: стек Python 3.12 / httpx / websockets / eth-account / SQLite WAL, три этапа развития, план тестирования | §1 стек; §4 состав этапа 1 (скрипты, без бота; ключи из wallet.txt); §6 критерий прохождения: фактические позиции и защитные ордера фолловера совпадают с target state, повторный reconciliation не создаёт лишних команд |
| `REPORTS_FACTUAL_COMPARISON_2026-08.md` | Аудит девяти параллельных исследований: что подтверждено официальными источниками, что опровергнуто | Перечень заблуждений, которые НЕ переносить в код: старые strategy routes `/strategyOrder`, `TRADE_PRO` как user-event, `clientTranId` как order idempotency-key, universal reduceOnly в Hedge mode, `priceProtect=true` как безопасный дефолт |
| `AGENTS.md` | Регламент репо: стиль Python, тесты, коммиты, безопасность | Conventional Commits (`feat:`/`fix:`/…), ветки `codex/`, `pytest` + `ruff`; приватные ключи никогда не в БД/логах в открытом виде. Пункт про пакет `bot/` устарел относительно ARCHITECTURE.md: этап 1 без Telegram, раскладка `src/copytradeaster/` |

Иерархия источников при конфликте: официальная документация Aster V3 → `ARCHITECTURE.md` → этот план → остальные `.md`. Любое расхождение деталей подписи/эндпоинтов с официальным V3 reference перепроверяется и решается в пользу reference.

## Approach

### Шаг 1 — каркас проекта

1. Создать `pyproject.toml`:
   - `[project] name="copytradeaster"`, `requires-python=">=3.12,<3.13"`;
   - runtime-зависимости: `httpx>=0.27,<1`, `websockets>=13,<16`, `eth-account>=0.13,<0.14`, `pydantic>=2.7,<3`, `pydantic-settings>=2.3,<3`, `aiosqlite>=0.20,<0.22`, `cryptography>=42`;
   - dev-зависимости (`[project.optional-dependencies] dev`): `pytest>=8.2`, `pytest-asyncio>=0.23`, `pytest-cov>=5`, `ruff>=0.6`, `respx>=0.21,<0.23`;
   - `[project.scripts] copytradeaster = "copytradeaster.cli.main:main"`;
   - `[tool.pytest.ini_options] asyncio_mode="auto"`, `addopts="--strict-markers"`, `markers=["testnet: signed runs against fapi.asterdex-testnet.com"]`, `testpaths=["tests"]`;
   - `[tool.coverage.run] source=["src/copytradeaster"]`; `[tool.ruff] line-length=100`, `lint.select=["E","F","I","UP","B","SIM"]`.
2. Создать дерево пакетов (пустые `__init__.py` только у реализуемых модулей): `src/copytradeaster/{domain,aster,master,risk,trading,storage,credentials,cli}`, `tests/{unit,integration,replay/fixtures,testnet}`, `scripts/`, `data/` (в `.gitignore` добавить `data/`).
3. `src/copytradeaster/__main__.py`: `from copytradeaster.cli.main import main; raise SystemExit(main())`.
4. `src/copytradeaster/config.py` — pydantic-settings `Settings(BaseSettings)` с `model_config = SettingsConfigDict(env_prefix="CT_", env_file=".env", extra="ignore")`. Поля (имена = ключи env после префикса):
   ```python
   rest_base_url: str = "https://fapi.asterdex-testnet.com"
   ws_user_base: str = "wss://fstream.asterdex-testnet.com/ws"
   db_path: Path = Path("data/copytrade.sqlite3")
   encryption_key: str            # hex 64 символа (32 байта), обязателен
   log_level: str = "INFO"
   reconcile_interval_s: int = 15
   listenkey_keepalive_min: int = 50
   weight_limit_per_min: int = 2400      # тестнет отдаёт sentinel -2 — берём mainnet-значение как дефолт
   orders_limit_per_min: int = 1200
   orders_limit_per_10s: int = 300
   fee_buffer_pct: Decimal = Decimal("0.001")   # буфер комиссий+проскальзывания на единицу цены
   working_type: str = "MARK_PRICE"             # триггер защитных ордеров фолловеров
   position_mode: str = "ONE_WAY"               # ONE_WAY | HEDGE
   symbol_whitelist: list[str] = ["BTCUSDT", "ETHUSDT"]  # копируются только эти символы мастера
   registration_chain_id: int = 56              # EIP-712 domain для ApproveAgent/ApproveBuilder
   builder_address: str = ""                    # адрес билдера; пусто → approve_agent идёт without builder
   builder_name: str = "copytradeaster"
   max_fee_rate: str = "0.0001"                 # кап builder fee (futures limit 0.001)
   dry_run: bool = False                        # True: команды логируются, HTTP не отправляется
   ```
   Валидация: `encryption_key` декодируется в 32 байта при старте, иначе `ValueError`.
5. `.env.example` со всеми ключами и комментариями; `.gitignore` дополнить `data/`.

**Зависимости:** нет. После шага 1 `uv venv .venv --python 3.12 && uv pip install -e ".[dev]" && pytest` проходит (0 тестов).

### Шаг 2 — чистые модули (зависимость: доменные модели шага 1)

#### 2.1 `domain/models.py`

Только stdlib-dataclass'ы (внутреннее состояние) — pydantic оставлен для DTO/config. Все денежные величины `Decimal`, запрещён float.

```python
class Side(StrEnum): BUY; SELL
class PosSide(StrEnum): BOTH; LONG; SHORT          # BOTH = One-way
class ActionKind(StrEnum): SET_LEVERAGE; MARKET_OPEN; MARKET_CLOSE; PLACE_STOP; PLACE_TP; CANCEL_ORDER
class SkipReason(StrEnum): NO_STOP; BELOW_MIN_QTY; BELOW_MIN_NOTIONAL; INSUFFICIENT_MARGIN; ENTRY_DEVIATION; DISABLED; PAUSED

@dataclass(frozen=True, slots=True)
class PositionKey: symbol: str; pos_side: PosSide = PosSide.BOTH

@dataclass(frozen=True, slots=True)
class ProtectiveOrder: client_order_id: str; order_id: int; type: str; side: Side
                       stop_price: Decimal; reduce_only: bool; close_position: bool; working_type: str

@dataclass(slots=True)
class PositionState: key: PositionKey; qty: Decimal; entry_price: Decimal; leverage: int
                     mark_price: Decimal; liquidation_price: Decimal
                     stop: ProtectiveOrder | None; take_profit: ProtectiveOrder | None

@dataclass(slots=True)
class AccountState: account_id: str; available_usdt: Decimal; positions: dict[PositionKey, PositionState]
                    open_orders: list[dict]           # сырые строки openOrders для отладки/сверки

@dataclass(frozen=True, slots=True)
class RiskPolicy: risk_percent: Decimal; max_leverage: int; max_entry_deviation_pct: Decimal
                  enabled: bool = True; allow_new_entries: bool = True

@dataclass(frozen=True, slots=True)
class TargetPosition: key: PositionKey; qty: Decimal; stop_price: Decimal | None
                      tp_price: Decimal | None; leverage: int

@dataclass(frozen=True, slots=True)
class TargetState: account_id: str; targets: list[TargetPosition]

@dataclass(frozen=True, slots=True)
class ExecutionAction: kind: ActionKind; symbol: str; payload: dict[str, str]
                       client_order_id: str | None; replaces_client_order_id: str | None; priority: int

@dataclass(frozen=True, slots=True)
class ExecutionPlan: account_id: str; trade_id: int | None; actions: list[ExecutionAction]

@dataclass(frozen=True, slots=True)
class TradingIdentity: account_id: str; user: str; signer: str; chain_id: int

@dataclass(frozen=True, slots=True)
class SymbolFilters: symbol: str; tick_size: Decimal; step_size: Decimal; min_qty: Decimal
                     max_qty: Decimal; market_max_qty: Decimal; min_notional: Decimal

@dataclass(slots=True)
class QtyDecision: qty: Decimal | None; reason: SkipReason | None    # инвариант: ровно одно поле не None
```

Тесты `tests/unit/domain/test_models_invariants.py`: QtyDecision-инвариант, PositionKey-хэшируемость, Decimal-only (конструктор принимает str/int/Decimal, отвергает float).

#### 2.2 `storage/` — SQLite (aiosqlite, WAL)

`storage/database.py`:
```python
MIGRATIONS: list[str]  # список SQL, применяется по порядку; таблица schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)
class Database:
    def __init__(self, path: Path): ...
    async def connect(self) -> None        # aiosqlite.connect, PRAGMA journal_mode=WAL, foreign_keys=ON
    async def migrate(self) -> int         # применяет неприменённые MIGRATIONS в транзакции
    async def close(self) -> None
```
Схема (ровно эти DDL в MIGRATIONS):
```sql
CREATE TABLE accounts(
  id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('master','follower')),
  label TEXT NOT NULL UNIQUE, user_address TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE credentials(
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  encrypted BLOB NOT NULL, algo TEXT NOT NULL DEFAULT 'AESGCM-v1', updated_at TEXT NOT NULL);
CREATE TABLE copy_settings(
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  risk_percent TEXT NOT NULL, max_leverage INTEGER NOT NULL,
  max_entry_deviation_pct TEXT NOT NULL, risk_base TEXT NOT NULL DEFAULT 'available_balance',
  enabled INTEGER NOT NULL DEFAULT 1, allow_new_entries INTEGER NOT NULL DEFAULT 1);
CREATE TABLE master_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, event_time INTEGER NOT NULL,
  received_at TEXT NOT NULL, raw TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE, processed_at TEXT);
CREATE TABLE copy_trades(
  trade_id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, pos_side TEXT NOT NULL,
  opened_at TEXT NOT NULL, closed_at TEXT, master_event_id INTEGER,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED','SKIPPED')));
CREATE TABLE order_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER REFERENCES copy_trades(trade_id),
  account_id TEXT NOT NULL, kind TEXT NOT NULL,
  master_client_order_id TEXT, follower_client_order_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(account_id, follower_client_order_id));
CREATE TABLE execution_commands(
  command_key TEXT PRIMARY KEY, account_id TEXT NOT NULL, trade_id INTEGER,
  kind TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SUBMITTED','CONFIRMED','UNKNOWN','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, client_order_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_commands_account_status ON execution_commands(account_id, status);
CREATE TABLE nonce_store(signer_address TEXT PRIMARY KEY, last_nonce INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```
Decimal'ы хранятся как TEXT. `credentials.encrypted` — AES-GCM blob от JSON `{"signer_key": "<hex>", "main_key": "<hex>|null"}`.

`storage/repositories.py` — по классу на таблицу, все методы `async`:
```python
class AccountRepo:   upsert(id, role, label, user_address); get_all(role=None) -> list[Row]; get(id) -> Row | None
class CredentialRepo: put_encrypted(account_id, blob: bytes); get(account_id) -> bytes   # blob = шифрованный JSON {signer_key, main_key}
class SettingsRepo:   get_policy(account_id) -> RiskPolicy; upsert_policy(p: RiskPolicy, account_id)
                      get_flag(key: str) -> str | None; set_flag(key, value)   # таблица app_state
class MasterEventRepo: append(event_type, event_time, raw_json, dedup_key) -> int | None  # None при дубликате (IGNORE)
                       mark_processed(id)
class CopyTradeRepo:  open_trade(symbol, pos_side, master_event_id) -> int; close_trade(trade_id)
                      active_trade(symbol, pos_side) -> Row | None
class OrderLinkRepo:  add(trade_id, account_id, kind, follower_coid, master_coid=None)
                      by_follower_coid(account_id, coid) -> Row | None; by_trade(trade_id) -> list[Row]
class CommandRepo:    insert(command_key, account_id, trade_id, kind, payload_json, priority, client_order_id) -> None
                      set_status(command_key, status, last_error=None, bump_attempts=False)
                      unresolved_unknowns() -> list[Row]     # status='UNKNOWN'
                      recent(limit) -> list[Row]             # для CLI status
class NonceStore:     next_nonce(signer_address) -> int
                      # SELECT last_nonce; candidate = max(last+1, seconds*1_000_000); UPDATE ... ; COMMIT ДО возврата
                      # (persist-before-send; коммит обязан произойти до отправки HTTP)
```
Тесты `tests/unit/storage/`: миграции идемпотентны (повторный migrate() = 0 изменений); каждый репозиторий roundtrip; дедуп master_events по UNIQUE; NonceStore монотонен и переживает «перезапуск» (новый экземпляр над той же БД продолжает с сохранённого значения); CommandRepo переходы статусов.

#### 2.3 `credentials/crypto.py`

AES-256-GCM через `cryptography.hazmat`:
```python
def encrypt(key: bytes, plaintext: bytes) -> bytes   # nonce(12B) || ciphertext+tag, nonce=os.urandom
def decrypt(key: bytes, blob: bytes) -> bytes        # InvalidTag пробрасывается как есть
```
Тесты `tests/unit/credentials/test_crypto_roundtrip.py`: roundtrip; tamper → InvalidTag; неверный ключ → InvalidTag; уникальность nonce при повторном encrypt тех же данных.

#### 2.4 `aster/signing.py` + `aster/rate_limits.py`

Подпись — точный порт официального примера из V3 doc (см. раздел «Контракт»):
```python
EIP712_DOMAIN_BASE = {"name": "AsterSignTransaction", "version": "1",
                      "verifyingContract": "0x0000000000000000000000000000000000000000"}
TRADING_CHAIN_ID_MAINNET = 1666
TRADING_CHAIN_ID_TESTNET = 714

def build_query_string(params: Sequence[tuple[str, str]]) -> str
    # urllib.parse.urlencode(list_of_pairs) — порядок вставки сохраняется; это и есть подписываемая строка
def sign_trading(qs: str, private_key_hex: str, chain_id: int) -> str
    # eth_account: typed_data = {"types": {"EIP712Domain": [name/version/chainId/verifyingContract],
    #              "Message": [{"name":"msg","type":"string"}]}, "primaryType": "Message",
    #              "domain": {**EIP712_DOMAIN_BASE, "chainId": chain_id}, "message": {"msg": qs}}
    # return Account.sign_message(encode_structured_data(typed_data), private_key).signature.hex()  # БЕЗ 0x
def prepare_signed_request(params: dict[str, str], identity: TradingIdentity,
                           private_key_hex: str, nonce: int) -> tuple[str, str]
    # canonical порядок: бизнес-параметры (как переданы), затем user, signer, nonce
    # qs = build_query_string(...); sig = sign_trading(qs, key, identity.chain_id)
    # return qs, qs + "&signature=" + sig
def sign_registration(params: dict[str, Any], main_private_key_hex: str,
                      primary_type: str, chain_id: int = 56) -> tuple[dict[str, str], str]
    # Registration-контекст (ApproveAgent/ApproveBuilder): подписывает ОСНОВНОЙ кошелёк аккаунта.
    # Вызывающий (management_api) заранее кладёт в params бизнес-поля + asterChain + user + nonce.
    # 1) ключи капитализируются: agentName→AgentName (demo capitalizeKeys);
    # 2) тип поля: bool→"bool", int→"uint256", остальное→"string"; types[primaryType] строится динамически;
    # 3) domain = EIP712_DOMAIN_BASE + chainId=chain_id(56); primaryType = 'ApproveAgent'|'ApproveBuilder';
    # 4) sig = Account.sign_message(encode_structured_data(typed_data), main_key).signature.hex()  # без 0x
    # возвращает (params с добавленными signature=sig и signatureChainId=str(chain_id), urlencoded_qs)
```
`rate_limits.py`:
```python
class SlidingWindowLimiter:      # deque[(timestamp, weight)]; async acquire(weight) спит до окна
class RateLimits:
    def __init__(self, weight_per_min: int, orders_per_min: int, orders_per_10s: int): ...
    async def acquire_request(self, weight: int) -> None       # общий IP-бюджет
    async def acquire_order(self, account_id: str) -> None     # per-account два окна
```
Тесты `tests/unit/aster/test_signing.py`: детерминизм (одинаковые входы → одинаковая подпись); восстановление адреса `Account.recover_message` == адрес приватного ключа; chainId 714 меняет подпись относительно 1666; `build_query_string` сохраняет порядок вставки и кодирует спецсимволы; `sign_registration`: recover == main-адрес, капитализация ключей, инференс типов (bool→bool, int→uint256, str→string), signatureChainId добавлен. `tests/unit/aster/test_rate_limiter.py`: блокировка при превышении, отпускание по прошествии окна, независимость per-account окон.

#### 2.5 `risk/engine.py` — чистый риск-модуль (без I/O)

```python
def entry_quantity(policy: RiskPolicy, equity_available: Decimal, expected_entry: Decimal,
                   stop_price: Decimal, filters: SymbolFilters,
                   fee_buffer_pct: Decimal) -> QtyDecision
    # risk_budget = equity_available * policy.risk_percent
    # distance    = abs(expected_entry - stop_price)
    # per_unit    = distance * (1 + fee_buffer_pct)
    # qty_raw     = risk_budget / per_unit
    # qty         = floor_to_step(qty_raw, filters.step_size)
    # skip: stop==entry→NO_STOP невозможен (стоп проверен выше), qty < min_qty → BELOW_MIN_QTY,
    #       qty*expected_entry < min_notional → BELOW_MIN_NOTIONAL,
    #       qty > market_max_qty → clamp к market_max_qty (floor_to_step),
    #       маржа qty*entry/max_leverage... см. margin_check ниже
def margin_check(qty: Decimal, entry: Decimal, leverage: int, equity_available: Decimal) -> bool
    # required = qty * entry / leverage; ok если required <= equity_available * Decimal("0.8")
def post_fill_cap(policy, equity_available, fill_price, stop_price, filters, fee_buffer_pct) -> Decimal
    # floor_to_step(risk_budget / (abs(fill-stop)*(1+fee_buffer_pct)), step) — максимум qty после фактического fill
def scaled_reduce(actual_qty: Decimal, master_old_abs: Decimal, master_new_abs: Decimal,
                  filters: SymbolFilters) -> Decimal
    # floor_to_step(actual_qty * master_new_abs / master_old_abs, step); master_old_abs==0 → Decimal(0)
def deviation_exceeded(expected_entry: Decimal, actual_fill: Decimal, policy: RiskPolicy) -> bool
    # abs(actual_fill - expected_entry)/expected_entry > policy.max_entry_deviation_pct
```
Все функции принимают/возвращают доменные типы, никаких исключений на бизнес-границах — только `QtyDecision(reason=...)`.

Тесты `tests/unit/risk/test_engine_sizing.py` — табличные: базовый расчёт; округление вниз по stepSize; BELOW_MIN_QTY; BELOW_MIN_NOTIONAL; кламп к market_max_qty; margin_check на границе 0.8; post_fill_cap при проскальзывании вверх; scaled_reduce 25%/30%; deviation порог строго больше; нулевой distance (stop==entry) → защита от деления на ноль: вернуть `QtyDecision(None, SkipReason.NO_STOP)`.

#### 2.6 `trading/planner.py` — чистый планировщик

```python
def build_plan(actual: AccountState, target: TargetState,
               filters_by_symbol: dict[str, SymbolFilters],
               client_order_id_for: Callable[[ActionKind, str, int], str]) -> ExecutionPlan
```
Правила переходов (зафиксировано, менять нельзя):
1. По каждому `TargetPosition`: если `actual.positions[key].leverage != target.leverage` → `SET_LEVERAGE` (priority 0, payload `{"symbol":…, "leverage":str(int)}`).
2. `target.qty > actual.qty` (дельта ≥ step_size): `MARKET_OPEN` дельты (priority 1, side по направлению, `newOrderRespType=RESULT`), затем если `target.stop_price` задан и отсутствует/отличается более чем на tick → `PLACE_STOP` (priority 2, `STOP_MARKET`, `reduceOnly=true`, `workingType` из конфига, `priceProtect=false`, `stopPrice`). Аналогично `PLACE_TP` (`TAKE_PROFIT_MARKET`) если задан tp.
3. `0 < target.qty < actual.qty`: `MARKET_CLOSE` дельты (priority 1, `reduceOnly=true`, side противоположный позиции). Защитные ордера НЕ трогаем (они reduceOnly и остаются валидны).
4. `target.qty == 0` и позиция есть: сначала `CANCEL_ORDER` каждого защитного ордера позиции (priority 1, `replaces_client_order_id=None`), затем `MARKET_CLOSE` полного остатка (priority 2, `reduceOnly=true`, qty = фактический остаток из actual).
5. Замена стопа (уровень изменился, позиция остаётся): `PLACE_STOP` нового (priority 1) ЗАТЕМ `CANCEL_ORDER` старого (priority 2, `replaces_client_order_id=<старый coid>`). Сначала ставим новый — короткий период двойной защиты допустим, незащищённого окна нет.
6. Hedge-режим (`PosSide != BOTH`): закрывающие ордера идут с `positionSide` и БЕЗ `reduceOnly`; открывающие — с `positionSide`. One-way: `reduceOnly=true`, positionSide не передаётся.
7. `client_order_id_for(kind, symbol, seq)` даёт coid для каждого action (реализация в executor, planner только вызывает).
8. Инвариант: `actual == target` (qty равны с точностью step, уровни стопов равны с точностью tick, leverage равен) → план из 0 действий.

Тесты `tests/unit/trading/test_planner_transitions.py`: каждое правило отдельным кейсом; тройной вызов `build_plan` на совпадающем состоянии → пустой план (инвариант идемпотентности); порядок place-new-stop-before-cancel-old; hedge-варианты payload'ов; отсутствие SET_LEVERAGE при равном плече.

### Шаг 3 — адаптер Aster и мастер-стрим (зависимости: шаги 1–2)

#### 3.1 `aster/models.py` + `aster/trading_api.py`

`models.py` — pydantic-модели DTO (валидация ответов биржи):
```python
class BalanceDTO(BaseModel):  asset: str; balance: Decimal; cross_wallet_balance: Decimal
                              cross_un_pnl: Decimal; available_balance: Decimal; max_withdraw_amount: Decimal
                              model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)
class PositionDTO(BaseModel): symbol: str; position_amt: Decimal; entry_price: Decimal; mark_price: Decimal
                              leverage: int; liquidation_price: Decimal; margin_type: str
                              position_side: PosSide; un_realized_profit: Decimal
class OrderDTO(BaseModel):    order_id: int; client_order_id: str; symbol: str; side: Side; type: str
                              status: str; orig_qty: Decimal; executed_qty: Decimal; avg_price: Decimal
                              price: Decimal; stop_price: Decimal; reduce_only: bool; close_position: bool
                              position_side: PosSide; working_type: str; time_in_force: str; update_time: int
class ExchangeInfo(BaseModel) # symbols[] → SymbolFilters + triggerProtect; rateLimits[]
```
Разбор полей — по именам из раздела «Контракт» (`origQty`, `executedQty`, `avgPrice`, `unRealizedProfit`, …).

`ports.py` — протоколы границ (адаптер не знает про SQLite):
```python
class IdentityProvider(Protocol):  def identity(self, account_id: str) -> TradingIdentity
class KeyProvider(Protocol):       def private_key(self, account_id: str) -> str            # signer-ключ (trading)
                                   def main_private_key(self, account_id: str) -> str | None # основной кошелёк (registration)
class NonceSource(Protocol):       def next_nonce(self, signer: str) -> int
```

`trading_api.py`:
```python
class AsterError(Exception): code: int; msg: str
class AsterClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, limiter: RateLimits,
                 identities: IdentityProvider, keys: KeyProvider, nonces: NonceSource,
                 dry_run: bool = False): ...
    async def _signed(self, account_id: str, method: str, path: str, params: dict[str,str],
                      weight: int, is_order: bool = False) -> Any
        # limiter.acquire_request(weight); if is_order: limiter.acquire_order(account_id)
        # qs, full = prepare_signed_request(params, identity, key, nonces.next_nonce(identity.signer))
        # url = base_url + path + "?" + full; httpx.request(method, url, content=b"")  (query-mode, пустое тело)
        # dry_run: логирует URL с замаскированной подписью и возвращает {} без HTTP
        # HTTP!=2xx или {"code"<0}: парсинг в AsterError; 429/418 → RateLimitError(AsterError)
    # публичные методы (все с account_id первым аргументом):
    async def server_time(self) -> int                                   # GET /fapi/v3/time, без подписи
    async def exchange_info(self) -> ExchangeInfo                        # GET /fapi/v3/exchangeInfo, кэш 1 час
    async def balance(self, account_id) -> list[BalanceDTO]              # GET /fapi/v3/balance, w5
    async def position_risk(self, account_id, symbol=None) -> list[PositionDTO]   # w5
    async def open_orders(self, account_id, symbol=None) -> list[OrderDTO]        # w1 (symbol) / w40 (без)
    async def query_order(self, account_id, symbol, *, orig_client_order_id=None,
                          order_id=None) -> OrderDTO | None              # GET /fapi/v3/order; -2013 → None
    async def place_order(self, account_id, req: PlaceOrderRequest) -> OrderDTO   # POST /fapi/v3/order, w1, is_order
    async def cancel_order(self, account_id, symbol, *, orig_client_order_id=None,
                           order_id=None) -> OrderDTO                    # DELETE /fapi/v3/order
    async def set_leverage(self, account_id, symbol, leverage: int) -> None       # POST /fapi/v3/leverage
    async def set_margin_type(self, account_id, symbol, margin_type: str) -> None # CROSSED|ISOLATED; -4046/-4045 «no need change» глотается
    async def get_position_mode(self, account_id) -> bool                # GET /fapi/v3/positionSide/dual, w30
    async def set_position_mode(self, account_id, dual: bool) -> None    # POST dualSidePosition "true"/"false"
    async def start_listen_key(self, account_id) -> str                  # POST /fapi/v3/listenKey
    async def keepalive_listen_key(self, account_id) -> None             # PUT /fapi/v3/listenKey
    async def close_listen_key(self, account_id) -> None                 # DELETE /fapi/v3/listenKey
@dataclass(frozen=True)
class PlaceOrderRequest:  # собирается executor'ом/planner'ом
    symbol: str; side: Side; type: str; quantity: Decimal | None; price: Decimal | None
    stop_price: Decimal | None; reduce_only: bool; close_position: bool
    position_side: PosSide; new_client_order_id: str; new_order_resp_type: str = "ACK"
    working_type: str | None = None   # только для STOP_MARKET/TAKE_PROFIT_MARKET
    def to_params(self) -> dict[str, str]  # только непустые поля; Decimal → str без экспоненты
```
MARKET-ордера всегда `newOrderRespType="RESULT"` (ответ содержит итоговый avgPrice/executedQty — нужно для post-fill пересчёта риска).

Тесты `tests/unit/aster/test_trading_api.py` (respx): подпись присутствует и покрывает параметры (пересчёт на моке); порядок параметров стабилен; `to_params` опускает None и форматирует Decimal; -2013 → None; 429 → RateLimitError; dry_run не делает HTTP; weights передаются в лимитер.

#### 3.2 `aster/user_stream.py` — мастерский user-data stream

```python
class StreamEvent(NamedTuple): raw: dict; received_at: float

class UserStream:
    """Жизненный цикл: start_listen_key → ws connect ws_base/<listenKey> → события в on_event.
    keepalive PUT каждые listenkey_keepalive_min (дефолт 50 < TTL 60);
    проактивный реконнект через 23 часа (лимит 24ч);
    при 'listenKeyExpired' или разрыве: новый listenKey → reconnect, backoff 1s→30s с джиттером;
    ping/pong кадры обрабатывает библиотека websockets автоматически."""
    def __init__(self, client: AsterClient, account_id: str, ws_base: str,
                 keepalive_min: int, on_event: Callable[[StreamEvent], Awaitable[None]],
                 on_reconnect: Callable[[], Awaitable[None]]): ...
    async def run(self, stop: asyncio.Event) -> None
```
Каждое сырое событие до обработки пишется в `master_events` (делает projector, stream только доставляет).

Тесты `tests/unit/aster/test_user_stream_protocol.py`: fake-ws (объект с `send`/`recv`/async-итерацией) — доставка событий в on_event; listenKeyExpired → новый listenKey и переподключение; backoff растёт; stop-event корректно завершает.

#### 3.3 `credentials/service.py`

```python
class SqliteCredentials:   # реализует KeyProvider + запись
    def __init__(self, db: Database, key: bytes): ...
    async def import_identity(self, account_id: str, user: str, signer: str,
                              private_key_hex: str, role: str, label: str) -> None
    def private_key(self, account_id: str) -> str      # decrypt(credentials.encrypted).decode()
    def identity(self, account_id: str) -> TradingIdentity
```
Приватные ключи НИКОГДА не попадают в логи: в проекте единая точка `redact(s: str) -> str` (`src/copytradeaster/logging_utils.py`, маскирует всё после `0x` до последних 4 символов); логирование URL запроса — с `signature=` обрезанным через redact.

Тесты `tests/unit/credentials/test_service.py`: import → private_key roundtrip; неверный мастер-ключ → InvalidTag; identity строится корректно.

#### 3.4 `master/projector.py` — нормализация состояния мастера

```python
@dataclass(slots=True)
class MasterDelta: changed_symbols: frozenset[str]; state: "MasterState"

@dataclass(slots=True)
class MasterState: available_usdt: Decimal; positions: dict[PositionKey, PositionState]

class MasterProjector:
    def __init__(self, events: MasterEventRepo, trades: CopyTradeRepo,
                 links: OrderLinkRepo, settings: SettingsRepo,
                 on_delta: Callable[[MasterDelta], Awaitable[None]]): ...
    async def apply_raw(self, raw: dict) -> None
        # dedup_key = f"{raw['e']}:{raw['E']}:{o.i}:{o.x}" для ORDER_TRADE_UPDATE, иначе e:E
        # append в master_events; дубликат → выход; события с E меньше последнего обработанного → игнор (не по порядку)
    async def _on_order_update(self, o: dict) -> None
        # x=TRADE: обновить позицию (l/z, rp); эпизоды:
        #   было 0 стало ≠0 или flip знака → trades.open_trade(symbol, pos_side, event_id)
        #   стало 0 → trades.close_trade(active)
        # X=CANCELED условного STOP_MARKET/TAKE_PROFIT_MARKET → снять его с PositionState.stop/take_profit
        # X=NEW условного → поставить/заменить (замена = cancel старого + new нового, сопоставление по type+symbol)
        # после любого изменения → rebuild через REST-снапшот этого symbol (position_risk + open_orders)
        #   и emit MasterDelta
    async def _on_account_update(self, a: dict) -> None
        # a.P: pa/ep/leverage из события; a.B: доступный баланс; emit Delta по затронутым символам
    async def _on_config_update(self, body: dict) -> None
        # ac: leverage символа; ai.d: смена position mode → полный rebuild
    async def _on_margin_call(self, raw: dict) -> None
        # лог CRITICAL + app_state flag 'degraded'=reason (уведомление в консоль)
    async def rebuild_full(self, client: AsterClient, account_id: str) -> MasterState
        # REST: balance + position_risk + open_orders → MasterState
        # защитные ордера: type STOP_MARKET → stop, TAKE_PROFIT_MARKET → take_profit (по reduce_only/close_position)
```
Эпизоды (CopyTrade): открытие фиксируется только по фактическому изменению позиции (fills/ACCOUNT_UPDATE), не по выставлению ордера. Повторный вход после закрытия того же символа = новый `trade_id`.

Тесты `tests/unit/master/test_projector_payloads.py`: фикстуры-payload'ы (точные JSON из раздела «Контракт») → ожидаемые дельты; дубликат события игнорируется; событие со старым E игнорируется; cancel+new стопа заменяет ProtectiveOrder; TRADE-fill 0→qty открывает trade, →0 закрывает; ACCOUNT_CONFIG_UPDATE меняет leverage. `tests/replay/test_projector_replay_fixtures.py`: прогон всех `tests/replay/fixtures/*.json` через apply_raw, сверка конечного MasterState с эталоном в файле.

#### 3.5 `aster/management_api.py` — ApproveAgent/ApproveBuilder (registration-контекст)

```python
class ManagementClient:
    """Авторизации подписываются ОСНОВНЫМ кошельком аккаунта (main_private_key), chainId 56."""
    def __init__(self, http, base_url, limiter, identities, keys, nonces, cfg: Settings): ...
    async def approve_agent(self, account_id: str, *, agent_name: str, expired_ms: int,
                            can_perp_trade: bool = True, can_spot_trade: bool = False,
                            can_withdraw: bool = False, ip_whitelist: str = "",
                            with_builder: bool = True) -> dict
        # POST /fapi/v3/approveAgent, weight 50
        # params: agentName, agentAddress=<signer>, ipWhitelist, expired(мс),
        #         canSpotTrade/canPerpTrade/canWithdraw ("true"/"false");
        #         при with_builder и cfg.builder_address: builder, maxFeeRate=cfg.max_fee_rate, builderName;
        #         затем asterChain="Testnet", user, nonce → sign_registration(primary_type="ApproveAgent")
        # → GET-style query: qs + "&signature=" + sig + "&signatureChainId=56"
    async def approve_builder(self, account_id: str, *, max_fee_rate: str | None = None) -> dict
        # POST /fapi/v3/approveBuilder: builder, maxFeeRate (по умолчанию cfg.max_fee_rate),
        # asterChain, user, nonce → sign_registration(primary_type="ApproveBuilder")
        # Ответ биржи (code/msg) возвращается как есть — маршрут на тестнете живой (400/-1102 без подписи),
        # но успешное принятие без зарегистрированного билдера не гарантировано — см. contingencies.
```
Тесты `tests/unit/aster/test_management_api.py` (respx): typed data ApproveAgent совпадает по структуре с demo (капитализированные поля, типы bool/uint256/string); signatureChainId присутствует; подпись восстанавливает main-адрес; with_builder=False или пустой cfg.builder_address убирают builder-поля.

### Шаг 4 — исполнение и оркестрация (зависимости: шаги 1–3; подсекции делать по порядку 4.1→4.4 — каждая следующая использует предыдущие)

#### 4.1 `trading/executor.py`

```python
class CommandResult(NamedTuple): command_key: str; status: str; error: str | None

class Executor:
    def __init__(self, clients: Callable[[str], AsterClient], commands: CommandRepo,
                 links: OrderLinkRepo, locks: dict[str, asyncio.Lock],
                 on_confirmed: Callable[[str, ExecutionAction], Awaitable[None]] | None = None): ...
    async def submit(self, plan: ExecutionPlan) -> list[CommandResult]
        # per-account: async with locks[account_id]:  # последовательные mutation-команды одного follower
        # для каждого action по priority:
        #   command_key = sha256(f"{account}|{trade_id}|{kind}|{canonical_payload_json}")[:24]
        #   client_order_id = "ct" + command_key            # 26 символов, подходит под ^[\.A-Z:/a-z0-9_-]{1,36}$
        #   retries: coid = "ct" + command_key + "r" + str(attempt)   # attempt ≥ 1
        # статусы: INSERT PENDING → UPDATE SENDING → HTTP:
        #   2xx → SUBMITTED; MARKET RESULT-ответ с FILLED → сразу CONFIRMED (+on_confirmed)
        #   timeout / 503 / -1000 / -1006 / -1007 → UNKNOWN
        #   прочий AsterError → FAILED (last_error="code:msg")
        # UNKNOWN-resolution (до retry): query_order(symbol, orig_client_order_id=coid)
        #   найден → CONFIRMED; None → retry (attempts ≤ 3), потом FAILED + app_state degraded
        # после SUBMITTED условных ордеров: подтверждение через open_orders(symbol) наличие coid → CONFIRMED
        # каждая команда пишется в order_links (follower_client_order_id=coid)
```
Идемпотентность повторов: повторный submit того же plan (те же command_key) при существующем CONFIRMED/SUBMITTED пропускается (SELECT по PK перед исполнением).

Тесты `tests/unit/trading/test_executor_journal.py` (fake client): happy-path статусы; 503 → UNKNOWN → query нашёл → CONFIRMED без повторной отправки; 503 → query не нашёл → retry с новым coid; attempts>3 → FAILED + degraded-флаг; -2019 → FAILED немедленно; сериализация двух конкурентных submit одного аккаунта; пропуск уже исполненной команды (идемпотентность).

#### 4.2 `trading/coordinator.py`

```python
class Coordinator:
    def __init__(self, master_projector, followers: list[FollowerRuntime],
                 settings: SettingsRepo, executor: Executor, reconciler: "Reconciler",
                 filters_provider: Callable[[], dict[str, SymbolFilters]],
                 app_flags: SettingsRepo): ...
    async def on_master_delta(self, delta: MasterDelta) -> None
        # gather по всем enabled followers (_recompute_follower), return_exceptions=True;
        # исключение одного follower логируется и изолируется; 3 подряд ошибки → app_state degraded
    async def _recompute_follower(self, fr: FollowerRuntime, delta: MasterDelta) -> None
        # 1) policy = settings.get_policy(fr.account_id); if not policy.enabled: return
        # 2) actual = snapshot follower (client.balance+position_risk+open_orders)
        # 3) targets = self._targets_for(fr, delta.state, actual, policy)   # см. ниже
        # 4) plan = build_plan(actual, TargetState(...), filters, coid_factory)
        # 5) if plan.actions: await executor.submit(plan)
    def _targets_for(...) -> list[TargetPosition]:
        #   symbol не входит в cfg.symbol_whitelist → пропустить (debug-log), копирования нет
        #   для каждого оставшегося symbol в delta.changed_symbols:
        #   master_pos = delta.state.positions.get(key); master_stop = master_pos.stop.stop_price
        #   если master_pos is None или qty==0 → target qty=0 (закрыть)
        #   если у мастера нет стопа → SKIP reason=NO_STOP (лог + консоль), target не создаётся
        #   a-precondition (защита от повторного входа после собственного стопа фолловера):
        #     follower flat + мастерский эпизод (активный trade_id этого symbol) ещё открыт +
        #     его защитный STOP_MARKET этого эпизода уже FILLED (query_order по coiд из order_links,
        #     -2013/CANCELED ≠ FILLED) → SKIP reason=NO_STOP c логом CLOSED_BY_STOP: вход в ЭТОМ же
        #     эпизоде запрещён; новый эпизод мастера (новый trade_id) снимает запрет автоматически.
        #   ветки:
        #   a) у follower нет позиции → entry_quantity(policy, follower.available_usdt,
        #      expected_entry=master_pos.entry_price, stop=master_stop) → skip? лог+skip : TargetPosition(qty,…)
        #   b) позиция есть, мастер увеличил (master_qty вырос) → полный целевой qty =
        #      post_fill-эквивалент от текущего стопа: cap = post_fill_cap(policy, equity, entry_actual, stop)
        #      target = min(cap, current + добавка)? НЕТ: target = cap (state-based), но НЕ выше
        #      current + entry_quantity-добавки; проще и достаточно: target = cap
        #   c) мастер сократил → target = scaled_reduce(current, master_old_abs, master_new_abs)
        #   d) мастер переставил стоп ДАЛЬШЕ от входа → target = min(current, post_fill_cap(new_stop))
        #      (сокращение при расширении стопа — политика ARCHITECTURE §11)
        #   e) мастер переставил стоп БЛИЖЕ → target = current (авто-наращивание запрещено)
        #   f) allow_new_entries=False (pause) → действия увеличения/открытия отбрасываются,
        #      сокращения/закрытия проходят
        #   leverage target = min(master_pos.leverage, policy.max_leverage)
```
`FollowerRuntime` — dataclass: account_id, client, identity, lock (создаётся в app).

Тесты `tests/unit/trading/test_coordinator_routing.py`: fan-out на 4 followers; падение одного не мешает остальным; pause блокирует только открытия; ветки a–e переключаются таблично (fake master states); self-stop: стоп фолловера FILLED при открытом мастерском эпизоде → entry не создаётся, новый trade_id → entry снова разрешён.

#### 4.3 `trading/reconciler.py`

```python
@dataclass class ReconcileReport: per_account: dict[str, int]   # commands_created
class Reconciler:
    def __init__(self, coordinator, executor, settings, flags): ...
    async def reconcile(self, account_ids: list[str] | None = None, reason: str = "manual") -> ReconcileReport
        # для каждого follower: actual = REST-снапшот; target = coordinator._targets_for по текущему
        # MasterState мастера (полный, не только changed); plan = build_plan; submit; посчитать команды
```
Триггеры запуска (в app): startup (после READY-подготовки), после каждой партии команд executor'а (on_confirmed hook → debounce 1s), reconnect мастер-стрима, периодический sweep `reconcile_interval_s`, ручной CLI. Инвариант: повторный reconcile при actual==target создаёт 0 команд.

Тесты `tests/unit/trading/test_reconciler_idempotent.py`: три вызова подряд на консистентном состоянии → 0,0,0; рассинхрон (позиция есть, таргета нет) → 1 команда закрытия, второй вызов → 0.

#### 4.4 `app.py` + `cli/main.py`

`app.py` — composition root:
```python
class App:
    def __init__(self, cfg: Settings): ...
    async def start(self) -> None
        # BOOT (порядок фиксирован, ARCHITECTURE §12):
        # 1 Database.connect+migrate; 2 загрузка accounts/settings; 3 clock-drift check:
        #   |server_time() - local_ms| > 2000 → Abort("clock drift")  (окно nonce ±10s)
        # 4 resolve UNKNOWN: для каждого unresolved_unknowns → query_order по coid → CONFIRMED/FAILED
        # 5 bootstrap followers: get_position_mode → при отличии от cfg.position_mode set_position_mode
        #   (лог WARN); снапшоты мастера и followers; 6 reconcile(reason="startup")
        # 7 запуск MasterStream (on_delta → projector.apply_raw → coordinator.on_master_delta)
        #   + periodic sweep task + console-notifier
        # 8 app_state ready=true → лог "READY"
    async def stop(self) -> None   # graceful: stop event, close ws, close db
```
Уведомления этапа 1 — `notifications/console.py`: `notify(text: str)` = print с timestamp + logging INFO; вызовы: открытие/закрытие/сокращение по каждому follower, skip-причины, UNKNOWN/FAILED, MARGIN_CALL, degraded, READY.

`cli/main.py` — argparse, подпарсеры:
```text
copytradeaster init-db
copytradeaster import-wallet FILE [--wipe]
    # парсер wallet.txt: блоки, разделённые пустой строкой; поля (регистр букв не важен):
    #   "<label>" (первая строка блока), "evm adress:", "api wallet adress:", "privte key:",
    #   опционально "main privte key:" (ключ основного кошелька блока; нет строки → main_key=null)
    # label "main" → role=master, остальные → follower; upsert accounts + credentials (шифруются оба ключа)
    # + copy_settings с дефолтами (risk_percent=0.01, max_leverage=20, max_entry_deviation_pct=0.01)
    # печатает только маскированные адреса (redact), НИКОГДА ключи
copytradeaster approve-builder [--accounts all|ID,...] [--max-fee-rate F]
    # по умолчанию все 5 аккаунтов: approve_agent(with_builder=True) → approve_builder(cfg.max_fee_rate);
    # требует main privte key в credentials; аккаунт без main-ключа → WARN и пропуск;
    # сырые ответы биржи (code/msg) — в консоль
copytradeaster run                     # App.start() до Ctrl+C/stop
copytradeaster status                  # ready/paused/degraded, активные copy_trades, последние команды,
                                       # позиции followers (из REST, краткая таблица)
copytradeaster reconcile [--account ID]
copytradeaster pause | resume          # app_state allow_new_entries=false/true (мастер-флаг на всех followers)
copytradeaster emergency-close-all --yes   # target qty=0 по всем позициям всех followers, executor
```
Скрипты (не пакет, в `scripts/`):
- `manual_master_order.py` — эмуляция ручных действий мастера через тот же AsterClient (identity main): `open SYMBOL QTY STOP_PCT`, `move-stop SYMBOL NEW_STOP`, `partial-close SYMBOL PCT`, `close SYMBOL`. Нужен для воспроизводимого smoke на testnet.
- `capture_stream.py` — N секунд слушает мастерский user-data stream и пишет сырые события в JSON-файл (пополнение `tests/replay/fixtures/`).
- `assert_pure_coverage.py` — читает `coverage.json`, assert 100% для `domain/models.py`, `risk/engine.py`, `trading/planner.py`, `aster/signing.py`, `credentials/crypto.py`.

Тесты `tests/unit/cli/test_wallet_parser.py`: парсинг fixture-копии wallet.txt (5 блоков, опечатка "adress" учитывается), маскирование вывода, повторный импорт = upsert без дублей.

### Шаг 5 — тестовый контур (сквозной; зависимость: шаги 1–4)

1. `tests/integration/fake_aster.py` — FakeAster: in-memory реализация поверхности AsterClient (балансы, позиции, ордера, исполнение MARKET по заданной цене, срабатывание STOP_MARKET при движении цены, настраиваемые отказы: 503, -2019, timeout). Никакого HTTP.
2. `tests/integration/test_end_to_end_fake.py` — сценарии поверх FakeAster + реальных projector/coordinator/planner/executor:
   - мастер открыл BTCUSDT long → 4 follower'а открыли с расчётным qty и стопом;
   - мастер сократил 30% → все сократили ~30% (scaled_reduce);
   - мастер переставил стоп дальше → follower qty ≤ прежнего; ближе → qty не изменился;
   - мастер закрылся → у всех flat, защитных ордеров нет;
   - follower №3 с -2019 → остальные исполнились, №3 FAILED, degraded-флаг;
   - restart: в журнале UNKNOWN → boot-resolve → дублей ордеров нет, состояние консистентно;
   - pause: новые входы блокируются, закрытие проходит.
   - мастер открыл символ вне whitelist (SOLUSDT) → followers не реагируют;
   - стоп фолловера №2 сработал (цена дошла до stop), мастер ещё в позиции → повторного входа у №2 НЕТ (CLOSED_BY_STOP); мастер закрылся и открыл новый вход → №2 входит снова;
3. `tests/replay/fixtures/` — начальные 6 файлов: синтетические, но структурно точные payloads (ORDER_TRADE_UPDATE NEW/TRADE/CANCELED, ACCOUNT_UPDATE, ACCOUNT_CONFIG_UPDATE, MARGIN_CALL, listenKeyExpired) по деревьям полей из раздела «Контракт»; пополняются живыми записями `capture_stream.py`.
4. `tests/testnet/test_signed_scenarios.py` — маркер `@pytest.mark.testnet`, скип если нет `CT_TESTNET=1` в env. Сценарии (по §8 ресерча): server_time/drift; balance+positionRisk+openOrders обеих ролей; master MARKET open → follower copy → проверка fill и отклонения цены; partial close 25%; SL place + replace (cancel+new); TP place; disconnect listenKey → restore snapshot; повторная подача события (идемпотентность); restart с открытой позицией; недостаток маржи → FAILED; minQty/minNotional → skip; priceProtect false/true поведение триггера; self-stop: ручное касание stopPrice у одного фолловера → позиция закрыта, re-entry нет пока эпизод мастера тот же; builder e2e: approve_agent(with_builder=True) + approve_builder для всех 5 аккаунтов, ответы зафиксировать; при успехе — один order с builder+feeRate и проверка builderFee в `GET /fapi/v3/builder/userTrades`. Каждый сценарий заканчивает `reconcile()` дважды и asserts 0 новых команд.
   Builder-сценарий внутри testnet-набора помечен последним и скипается, если у аккаунтов нет main-ключей (`main_key IS NULL`) — он выполняется на шаге 8 Verification.
5. Гейт покрытия (единственный механизм, без отдельных скриптов): `pytest -m "not testnet" --cov=src/copytradeaster --cov-fail-under=90`.

---

## Контракт Aster V3 — проверенные факты

Источники, прочитанные целиком при подготовке плана: официальный V3 reference (mainnet raw GitHub), testnet reference, страницы asterCode endpoints/authentication, официальный демо-репозиторий `asterdex/API-demo/aster-code-demo` (`utils.js`, `demo.py` в доке, `env.example`).

### Подпись trading-запросов (Message) и registration-подпись (ApproveAgent/ApproveBuilder)

- Параметры аутентификации в самом запросе: `user` (master wallet address), `signer` (API-wallet address), `nonce`, `signature`.
- Подписываемая строка = **urlencoded query string всех параметров в порядке вставки, без signature**. Официальный пример: `param = urllib.parse.urlencode(my_dict)` → `typed_data["message"]["msg"] = param` → `url = host + path + "?" + param + "&signature=" + signed.signature.hex()`; POST с пустым телом валиден (query-mode). Альтернатива body-mode существует, но используем query-mode везде.
- EIP-712 typed data (ровно эта структура):
  ```python
  {"types": {"EIP712Domain": [{"name":"name","type":"string"},{"name":"version","type":"string"},
                              {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}],
             "Message": [{"name":"msg","type":"string"}]},
   "primaryType": "Message",
   "domain": {"name":"AsterSignTransaction","version":"1","chainId":1666,   # testnet: 714
              "verifyingContract":"0x0000000000000000000000000000000000000000"},
   "message": {"msg": "<urlencoded query string>"}}
  ```
- eth-account: `encode_structured_data(typed_data)` + `Account.sign_message(...)`; подпись `.hex()` **без** `0x`.
- `nonce` = `int(time.time()) * 1_000_000 + счётчик_внутри_секунды` (официальный пример `get_nonce` с локом). Окно валидности: prose говорит ±60s, пример — отставание/опережение >10s невалидно ⇒ держим drift-check 2s при буте и не полагаемся на окно. Сервер хранит последние 100 nonce на agent address ⇒ nonce персистентен и строго монотонен (NonceStore в БД, commit до отправки).
- Registration-контекст (ApproveAgent/ApproveBuilder): подписывает ОСНОВНОЙ кошелёк пользователя; ключи параметров капитализируются (agentName→AgentName); типы полей динамически: bool→bool, int→uint256, иначе string; domain тот же `AsterSignTransaction` version `1`, но chainId **56**; primaryType = `ApproveAgent`|`ApproveBuilder`; в запрос добавляется `signatureChainId=56`; параметры включают asterChain=Testnet, user, nonce; approveAgent может сразу содержать builder/maxFeeRate/builderName («одной подписью»). Проверено по официальному demo (`utils.js.signEIP712Main`, `01_approveAgent.js`) и фронтенд-демо (явный список типов ApproveAgent).

### REST (base `https://fapi.asterdex-testnet.com`, все пути `/fapi/v3/…`)

| Endpoint | Метод | Weight | Параметры |
|---|---|---|---|
| time / ping | GET | 1 | без подписи |
| exchangeInfo | GET | 1 | — |
| order (new) | POST | 1 | symbol*, side*(BUY/SELL), positionSide (BOTH one-way; LONG/SHORT hedge — обязателен в hedge), type*, timeInForce(GTC/IOC/FOK/GTX/HIDDEN), quantity, reduceOnly("true"/"false"; нельзя в hedge и с closePosition), price, newClientOrderId `^[\.A-Z\:/a-z0-9_-]{1,36}$` уникален среди открытых, stopPrice, closePosition(true только с STOP_MARKET/TAKE_PROFIT_MARKET, без quantity/reduceOnly), activationPrice, callbackRate(0.1–5), workingType(MARK_PRICE/CONTRACT_PRICE, default CONTRACT_PRICE), priceProtect(default FALSE), newOrderRespType(ACK/RESULT; RESULT для MARKET возвращает финальный fill) |
| order (query) | GET | 1 | symbol*, orderId ИЛИ origClientOrderId; -2013 если нет; canceled/expired без fills исчезают через 7 дней |
| order (cancel) | DELETE | 1 | symbol*, orderId ИЛИ origClientOrderId |
| openOrders | GET | 1 с symbol / 40 без | symbol опц. |
| balance | GET | 5 | массив: asset, balance, crossWalletBalance, crossUnPnl, availableBalance, maxWithdrawAmount |
| positionRisk | GET | 5 | symbol опц.; поля: positionAmt(знаковый), entryPrice, markPrice, liquidationPrice, leverage(str), marginType(isolated/crossed), unRealizedProfit, positionSide(BOTH/LONG/SHORT) |
| leverage | POST | 1 | symbol*, leverage int 1–125 |
| marginType | POST | 1 | symbol*, marginType ISOLATED/CROSSED |
| positionSide/dual | POST/GET | 1/30 | dualSidePosition "true"(hedge)/"false"(one-way) |
| listenKey | POST/PUT/DELETE | 1 | USER_STREAM, подписан, параметров нет |

- exchangeInfo: `rateLimits` [{REQUEST_WEIGHT,MINUTE,1,2400},{ORDERS,MINUTE,1,1200}] (тестнет может отдавать sentinel `-2` — не использовать как лимит); символы: `status=="TRADING"`, `contractType=="PERPETUAL"`, `triggerProtect`, фильтры точно с такими именами: `PRICE_FILTER{minPrice,maxPrice,tickSize}`, `LOT_SIZE{minQty,maxQty,stepSize}`, `MARKET_LOT_SIZE{minQty,maxQty,stepSize}`, `MAX_NUM_ORDERS{limit}`, `MAX_NUM_ALGO_ORDERS{limit}`, `MIN_NOTIONAL{notional}`, `PERCENT_PRICE{multiplierUp,multiplierDown}`.
- Заголовки ответа: `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)`, `X-MBX-ORDER-COUNT-…` (для отладки лимитов).
- `strategyOpenOrder` требует strategyId XOR clientStrategyId + strategyType(OTO/OCO/OTOCO) — списка «все стратегии» нет; legs OTOCO видны как обычные ордера в openOrders ⇒ снапшот защиты мастера строится из openOrders, этого достаточно.

### WebSocket user-data stream

- URL: `wss://fstream.asterdex-testnet.com/ws/<listenKey>` (mainnet: `wss://fstream.asterdex.com/ws/<listenKey>`).
- listenKey TTL 60 минут; PUT продлевает; рекомендация docs — пинг ~раз в 60 минут (мы шлём каждые 50). Соединение максимум 24 часа. События НЕ гарантированно по порядку — упорядочивать по `E`.
- `ORDER_TRADE_UPDATE`: `{e,E,T,o:{s,c,S,o,f,q,p,ap,sp,x,X,i,l,z,L,N,n,T,t,b,a,m,R,wt,ot,ps,cp,AP,cr,rp}}`. Исполнение `x`: NEW/CANCELED/CALCULATED/EXPIRED/TRADE. Статус `X`: NEW/PARTIALLY_FILLED/FILLED/CANCELED/EXPIRED/NEW_INSURANCE/NEW_ADL (+REST-значение REJECTED обрабатывать defensively). Спец-coid: префикс `autoclose-` = ликвидация, `adl_autoclose` = ADL.
- `ACCOUNT_UPDATE`: `{e,E,T,a:{m,B:[{a,wb,cw,bc}],P:[{s,pa,ep,cr,up,mt,iw,ps}]}}` — B/P содержат ТОЛЬКО изменённое; приходит только при реальном изменении позиции/баланса. Причины `m`: DEPOSIT, WITHDRAW, ORDER, FUNDING_FEE, ADJUSTMENT, INSURANCE_CLEAR, MARGIN_TRANSFER, MARGIN_TYPE_CHANGE, ASSET_TRANSFER и др.
- `ACCOUNT_CONFIG_UPDATE`: `{e,E,T,ac:{s,l}}` (плечо символа) либо `{…,ai:{j,f,d}}` (d=true = hedge mode).
- `MARGIN_CALL`: `{e,E,cw,p:[{s,ps,pa,mt,iw,mp,up,mm}]}` — предупреждение, не подтверждение закрытия.
- `listenKeyExpired`: `{e:"listenKeyExpired",E}` — после него событий нет до нового listenKey; с дисконнектом socket не связан.

### Ошибки (код в JSON-ответе `{"code":-NNNN,"msg":…}`)

- UNKNOWN-класс (после отправки финансовой команды трактовать как UNKNOWN, не как fail): HTTP 503, таймаут, `-1000 UNKNOWN`, `-1006 UNEXPECTED_RESP`, `-1007 TIMEOUT`.
- `-1001 DISCONNECTED`, `-1003 TOO_MANY_REQUESTS` (429; продолжение → 418 IP-ban), `-1015 TOO_MANY_ORDERS`, `-1022 INVALID_SIGNATURE`, `-1102 MANDATORY_PARAM_EMPTY_OR_MALFORMED`, `-1118 EMPTY_NEW_CL_ORD_ID`, `-2010 NEW_ORDER_REJECTED`, `-2011 CANCEL_REJECTED`, `-2013 NO_SUCH_ORDER`, `-2018 BALANCE_NOT_SUFFICIENT`, `-2019 MARGIN_NOT_SUFFICIENT`, `-2021 ORDER_WOULD_IMMEDIATELY_TRIGGER`, `-2022 REDUCE_ONLY_REJECT`, `-2024 POSITION_NOT_SUFFICIENT`, `-2027/-2028 LEVERAGE_RATIO`, `-4004 QTY_LESS_THAN_MIN_QTY`, `-4005 QTY_GREATER_THAN_MAX_QTY`, `-4014 PRICE_NOT_INCREASED_BY_TICK_SIZE`, `-4015 INVALID_CL_ORD_ID_LEN (>36)`.
- Прочее: `asterChain` в параметрах нужен только builder-ордерам (демо); обычные ордера официальный пример отправляет без него — этап 1 не добавляет; если testnet ответит `-1103/-1102` на отсутствие `asterChain`, добавить `asterChain=Testnet` параметром конфига (contingency).

---

## Critical files & anchors

- `ARCHITECTURE.md` §4 (границы модулей), §6 (planner/reconciliation), §7 (журнал команд), §11 (риск-политики), §12 (boot-порядок), §16 (состав этапа 1) — поведенческие правила, нарушать нельзя.
- `ASTER_API_RESEARCH_2026-08.md` §4 (формулы риска и post-fill алгоритм), §6 (ограничения rate limits/nonce/503), §8 (signed-чеклист — источник сценариев testnet-маркера).
- `wallet.txt` — формат импорта: блоки `label / evm adress: / api wallet adress: / privte key: / [main privte key:]` (буквальная опечатка «adress» сохранена автором файла), `main` = мастер; `main privte key` опционален и нужен для approve-builder.
- `https://raw.githubusercontent.com/asterdex/api-docs/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md` — эталон подписи (пример Python в разделе «Example of POST /fapi/v3/order») и таблиц параметров; при расхождении с этим планом — перепроверить и следовать доку.
- `DEV_PLAN_SKELETON.md` §6 — критерий прохождения сценариев (совпадение факта с target + 0 лишних команд при повторном reconcile).

---

## Verification

Предусловия: рабочая директория `/Users/jd_ebash/copytradeaster`; установлен `uv` (`~/.local/bin/uv`); faucet testnet пополнен для всех 5 аккаунтов сегодня (ручной визит `https://www.asterdex-testnet.com/en/faucet` под каждым кошельком); `.env` создан из `.env.example`, `CT_ENCRYPTION_KEY` сгенерирован: `python -c "import secrets; print(secrets.token_hex(32))"`.

1. Статика и юнит-контур:
   ```bash
   uv venv .venv --python 3.12
   uv pip install -e ".[dev]" --python .venv/bin/python
   .venv/bin/ruff check . && .venv/bin/ruff format --check .
   .venv/bin/pytest -m "not testnet" --cov=src/copytradeaster --cov-report=json --cov-fail-under=90
   ```
   Ожидание: все зелёные, покрытие ≥90%.
2. Импорт кошельков: `.venv/bin/python -m copytradeaster import-wallet wallet.txt` → вывод 5 строк с маскированными адресами (`main` role=master, `copyc1..4` followers), в БД `accounts`=5, `credentials`=5; ключи в выводе отсутствуют.
3. Smoke end-to-end на testnet (новое поведение, главный proof):
   ```bash
   .venv/bin/python -m copytradeaster run
   # ждать строку "READY"; затем во втором терминале:
   .venv/bin/python scripts/manual_master_order.py open BTCUSDT 0.002 0.5
   ```
   Ожидание в логе `run`: 4 строки вида `[copycN] OPEN BTCUSDT qty=… @≈… stop=…`; в `execution_commands` 8+ строк со статусом CONFIRMED (open+stop ×4); у каждого follower'а позиция и STOP_MARKET с `reduceOnly=true` (проверка `status`-команды или testnet UI).
4. Продолжение сценария: `partial-close BTCUSDT 30` → 4 сокращения; `move-stop` → 4×(place new stop, cancel old); `close BTCUSDT` → 4×flat, 0 открытых условных ордеров.
5. Идемпотентность: `.venv/bin/python -m copytradeaster reconcile` дважды подряд → оба раза `commands created: 0` по каждому follower.
6. Recovery: во время шага 3 убить процесс (`kill -9`) между отправкой и подтверждением → перезапуск `run` → в логе разрешение UNKNOWN (found/not found), дублей ордера нет (проверка openOrders), финальный `reconcile` → 0 команд.
7. Подписанный регресс: `CT_TESTNET=1 .venv/bin/pytest -m testnet -v` — все торговые сценарии зелёные; побочный продукт прогона — свежие fixtures в `tests/replay/fixtures/` через `capture_stream.py` (рекомендуется записать 60 сек живого стрима во время шага 3).
8. Builder e2e — ФИНАЛЬНЫЙ шаг этапа 1, после всех торговых тестов. Пользователь дописывает `main privte key:` в блоки `wallet.txt`, повторяет `import-wallet`, затем `.venv/bin/python -m copytradeaster approve-builder` → для каждого из 5 аккаунтов выведен ответ approveAgent/approveBuilder (code/msg); при успехе — один order с builder+feeRate и проверка builderFee в `GET /fapi/v3/builder/userTrades`. Отказ сервера из-за незарегистрированного билдера фиксируется как результат наблюдения и не влияет на принятую торговую приёмку (шаги 1–7).

Если пункт 3 недостижим из-за среды (faucet пуст, testnet лежит) — зафиксировать в отчёте выполнения вывод `pytest -m testnet` по доступным сценариям и причину по остальным; юнит- и интеграционный контур обязательны полностью.

---

## Assumptions & contingencies

Assumptions (решения пользователя; молчание = согласие):
1. Раскладка `src/copytradeaster/` по ARCHITECTURE.md §17 (утверждена 21.08.2026), а не `bot/` из AGENTS.md — этап 1 не содержит Telegram; AGENTS.md-упоминание устарело относительно архитектуры.
2. Ключи в `wallet.txt`: `privte key` — ключ API-wallet (signer, trading chainId 714); пользователь дописывает в каждый из 5 блоков строку `main privte key:` (ключ основного EVM-кошелька) для approve-builder прогона. Если какой-то main-ключ не добавлен — аккаунт пропускается в approve-builder с WARN, торговля не затрагивается.
3. Мастер торгует в One-way mode; бутстрап приводит все 5 тестнет-аккаунтов к one-way (set_position_mode при расхождении). Hedge-ветки planner'а реализованы, но не являются дефолтом.
4. Политики по умолчанию (подтверждены пользователем 21.08.2026): `risk_base=available_balance`, `risk_percent=0.01`, `max_leverage=20`, `max_entry_deviation_pct=0.01`, `fee_buffer_pct=0.001`, `workingType=MARK_PRICE`, `priceProtect=false`. Меняются в `copy_settings`/env без правки кода.
5. «Полное покрытие тестами» = один жёсткий гейт `--cov-fail-under=90` по всему пакету + матрица сценариев шага 5 (табличные юнит-тесты чистых модулей остаются полными; отдельный счётчик «100% на модуль» и скрипт-проверка удалены как дублирование).
6. Python 3.12 ставится через `uv` (системный python 3.14 не подходит под pin `<3.13`).
7. Белый список символов: копируются только BTCUSDT и ETHUSDT (решение пользователя); расширяется через `CT_SYMBOL_WHITELIST` без правки кода.

Contingencies (если реальность отличается — делать так, не останавливаясь):
- Подпись отклоняется (`-1022`) при верной структуре → проверить вариант body-mode (`params+signature` в form-body) и точную urlencode-эквивалентность строки подписи и строки запроса; крайний случай — дословный порт `demo.py` из официального репо.
- Testnet требует `asterChain` → добавить `asterChain=Testnet` в параметры каждого trading-запроса (config-флаг `include_aster_chain`).
- `STOP_MARKET reduceOnly` отклоняется на тестнете → перейти на `closePosition=true` для полных закрытий (флаг в конфиге planner'а), частичные остаются reduceOnly.
- Faucet не выдал средства части копиров → пометить аккаунт `enabled=0`, работать на остальных; в отчёте указать каких.
- WS-хост тестнета отличается (`fstream5.…`) → он уже конфигурируемый (`CT_WS_USER_BASE`).
- Ручная перестановка мастером Strategy OCO/OTOCO в UI не видна в WS → покрывается периодическим sweep (15s) + openOrders-снапшотом; поведение задокументировать в отчёте прогона как наблюдение этапа 1.
- Builder не зарегистрирован на тестнете (веб-вкладки нет, требование 100 ASTER закрывается faucet'ом вручную) → approveBuilder может вернуть ошибку: фиксируем ответ как результат прогона; approveAgent(with_builder=True) при этом всё равно валидирует registration-подпись; торговые ордера идут БЕЗ builder/feeRate до подтверждения регистрации.
- `approveAgent` отклоняется (-1022/-1102) → сверить капитализацию/типы полей с demo `01_approveAgent.js`; expired в миллисекундах; nonce тем же генератором, что и trading.
- Закрывающий MARKET отклонён из-за minNotional (остаток позиции < notional) → повторить полное закрытие через `closePosition=true` того же типа ордера; пыль позиции допустимо оставить, зафиксировать в логе.

