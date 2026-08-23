# Каркас плана разработки копитрейд-бота на AsterDex

Статус: предварительный каркас (21.08.2026), видение — три этапа (раздел 4). Полный фактологический бэкграунд — в [ASTER_API_RESEARCH_2026-08.md](ASTER_API_RESEARCH_2026-08.md).

## 1. Стек

```text
Python 3.12
aiogram 3.x            — Telegram-бот (онбординг, команды, уведомления)
httpx (async)          — REST к fapi.asterdex.com / fapi.asterdex-testnet.com
websockets             — user-data stream мастера + market stream
eth-account            — EIP-712 подписи (ApproveAgent/ApproveBuilder, ордера)
SQLite (aiosqlite)     — MVP: один файл, WAL, без инфраструктуры
PostgreSQL (asyncpg)   — продакшн: переезд на тонкие репозитории, без ORM
pydantic v2            — схемы ордеров/событий/конфигов, валидация
```

Приватные ключи signer-ов — только зашифрованное хранилище (мастер-ключ в env), в SQLite/PG — только следы и торговые данные.

## 2. Что за бот и как должен работать

### Роли

- **Мастер (ты):** один аккаунт, на котором открываются/закрываются сделки, переставляются стопы/тейки.
- **Фолловеры:** подписчики, аккаунты которых копируют действия мастера.

### Поток работы

1. Бот подключается к user-data WebSocket мастера (listenKey).
2. Мастер открыл/закрыл/переставил SL/TP/сократил позицию → бот получает событие (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, MARGIN_CALL).
3. Бот пересчитывает целевое состояние по каждому фолловеру: размер депозита + процент риска на стоп → риск-ревард → размер позиции.
4. Бот выставляет ордера на аккаунтах фолловеров (открытие, закрытие, частичное закрытие, SL/TP, смена плеча).
5. Уведомления о действиях и статусах — в Telegram.

Подробности по API-возможностям — разделы 2–6 ресерча.

## 3. Что собрали по Астеру (со сносками на ресерч)

| Тема | Суть | Сноска в ресерче | Ссылка (URL) |
|---|---|---|---|
| Версия API | V3 — Recommended; V1 — Legacy (новые V1 не выдаются с 25.03.2026) | §1 | [API Docs index](https://github.com/asterdex/api-docs/blob/master/README.md) |
| Ключи V3 | user + signer + private key + nonce + EIP-712; два signing context (trading 1666/714, registration 56/101) | §1, §5 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md), [API Wallet](https://www.asterdex.com/en/api-wallet) |
| Права агента | canSpotTrade / canPerpTrade / canWithdraw / expired / ipWhitelist; canWithdraw=false для копитрейда | §1 | [Agent endpoints](https://asterdex.github.io/aster-api-website/asterCode/endpoints/) |
| Торговля | balance, positionRisk, leverage, marginType, open/close, partial close, SL/TP, trailing | §2, §4 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) |
| Перестановка SL/TP | Обычный — cancel+new; Strategy OCO/OTOCO — API умеет, WS-поведение при ручной смене требует теста | §2, §3 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md), [User Data Streams](https://asterdex.github.io/aster-api-website/futures-v3/user-data-streams/) |
| Слежение за мастером | user-data WebSocket: ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, ACCOUNT_CONFIG_UPDATE, MARGIN_CALL, listenKey 60 мин + keepalive | §3 | [User Data Streams](https://asterdex.github.io/aster-api-website/futures-v3/user-data-streams/) |
| Копирование | По состоянию/филлам, не по командам; partial fill → target-state reconciliation | §3 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) |
| Риск от стопа | % от депозита на стоп → размер позиции; расчёт делает бот, не Aster | §4 | [Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) |
| Sub-account | Изоляция средств, V3 keys, withdrawal постоянно off; лимиты 10–50 шт | §1 | [Sub Accounts](https://docs.asterdex.com/trading/sub-accounts) |
| Testnet | fapi.asterdex-testnet.com, chainId 714, faucet 1000 USDT + 1000 ASTER/день | §7 | [Testnet API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-testnet.md), [TestNet docs](https://docs.asterdex.com/aster-chain/testnet), [Faucet](https://www.asterdex-testnet.com/en/faucet), [Testnet API Wallet](https://www.asterdex-testnet.com/en/api-wallet) |
| Builder (Aster Code) | Онбординг: юзер подписывает ApproveAgent+ApproveBuilder, signer создаёт билдер; feeRate кап 0.001; на тестнете маршруты живут, веб-вкладки нет | §9 | [Aster Code docs](https://docs.asterdex.com/program-and-rewards/aster-code), [Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/integration-flow/), [Futures Auth](https://asterdex.github.io/aster-api-website/asterCode/authentication/), [API demo](https://github.com/asterdex/API-demo/tree/main/aster-code-demo) |

Прочие справочные URL:

- [Aster API Changelog](https://github.com/asterdex/api-docs/blob/master/CHANGELOG.md)
- [Aster V3 Deposit & Withdrawal](https://asterdex.github.io/aster-api-website/futures-v3/deposit%26withdrawal/)
- [Aster legacy API management](https://www.asterdex.com/en/api-management)
- [Aster Code Spot Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/spot-integration-flow/)
- [Aster Code Builder frontend demo](https://github.com/jupiter-hongc/aster-code-builder-demo)
- [Live mainnet exchangeInfo](https://fapi.asterdex.com/fapi/v3/exchangeInfo)
- [Live testnet exchangeInfo](https://fapi.asterdex-testnet.com/fapi/v3/exchangeInfo)
- [Aster Skills Hub](https://github.com/asterdex/aster-skills-hub)

Подробная аргументация и полная матрица доказательств — [ASTER_API_RESEARCH_2026-08.md](ASTER_API_RESEARCH_2026-08.md).
## 4. Видение: три этапа

Функционал не завязан на бота: бот — обвязка (онбординг + уведомления), всё ядро гоняется скриптами. Порядок: скрипты → бот с прямым вводом ключей → Builder.

### Этап 1 — Скрипты, без бота (весь бэкенд)

- Стрим мастера: user-data WS (открытие, закрытие, перестановка SL/TP, частичное закрытие, MARGIN_CALL), listenKey keepalive, reconnect + snapshot.
- Риск: % депозита на стоп → размер позиции, риск-ревард.
- Исполнение на копиках: MARKET, SL/TP, partial close, плечо.
- Reconciliation: target-state, идемпотентность повторов.
- Builder-подписи скриптом: approveAgent + builder одной подписью, feeRate (тестнет).
- Уведомления — в консоль/лог. Всё на тестнете; ключи из wallet.txt (main + 4 копика).

### Этап 2 — Бот + прямой ввод ключей

- aiogram: приём V3-набора (user + signer + private key + права), проверка подключения копика (валидация canPerpTrade / ipWhitelist), настройка риска.
- Торговое ядро этапа 1 подключается как библиотека; креды пользователя отделены от торговли.
- Уведомления в Telegram.

### Этап 3 — Builder (Aster Code)

- Онбординг через ApproveAgent + ApproveBuilder вместо прямого ввода ключей; signer-ы создаёт и хранит билдер.
- Монетизация: builder fee (кап 0.001 futures).
- Торговое ядро и стрим не меняются — меняется только источник кредов.

**Статус (2026-08-22): этап 1 выполнен и проверен на testnet.**
Реализовано всё перечисленное выше; детали и журнал полной проверки движка
(12 сценариев: long/short/тейк/стоп/переносы/мультипозиции/казусы/лимитный вход,
биржевая сверка каждого ордера по clientOrderId) — в
`ENGINE_TEST_STATUS_2026-08.md`. Найденные живым прогоном баги исправлены
(фантомное переоткрытие позиции из-за направления филла, двойной учёт
ACCOUNT_UPDATE, отсутствие копирования TP, STALE-guard против гонок,
лимитные флейворы STOP/TAKE_PROFIT).
Builder-e2e (approveBuilder ждёт `main private key` в wallet.txt) оставлен для
Stage 3; зеркало висящих лимиток мастера — кандидат в этап 2.

Критерий перехода: этап 1 закрыт, когда проходят все signed-сценарии раздела «Как проводить тесты»; этап 2 — когда онбординг и уведомления работают на тестнете с ключами, введёнными через бота.

### Финальный Stage 1 gate — follow-up по `review_stage1_new.md` (2026-08-22)

- [x] Safety sequencing: при неуспешном `PLACE_STOP/PLACE_TP` не выполнять
  зависимый `CANCEL_ORDER` старой защиты.
- [x] Reconciliation generation: при неизменном target, но свежем
  `actual != target`, не переиспользовать исторический `CONFIRMED` command.
- [x] HEDGE REST recovery: убрать дубликат `_pick_protective` и покрыть защиту
  `positionSide=LONG/SHORT` без `reduceOnly/closePosition`.
- [x] Fresh market reference: перед stop/deviation decisions обновлять mark
  price затронутых master symbols через authoritative REST `positionRisk`.
- [x] Liquidation recovery: `MARGIN_CALL`, `CALCULATED`, autoclose/ADL и
  неизвестные position-changing execution types запускают немедленный
  authoritative master resnapshot и reconciliation.
- [x] Nonce/margin hardening: атомарно резервировать nonce в SQLite и проверять
  entry margin по фактически выбранному leverage.
- [x] Signed proof: выполнить и записать полноценный ONE_WAY цикл на testnet с
  локальными кошельками, включая cleanup; mainnet не используется.

Builder signed e2e не является блокером торгового ядра Stage 1 и остаётся в
контуре Stage 3, где реализуется Aster Code onboarding.

### Финальный hybrid load gate — 30 follower slots (2026-08-23)

- [x] Добавлен явный `execution_mode=REAL|SHADOW`; shadow rows не получают
  credentials и разрешены только на testnet при opt-in.
- [x] Production `App`/coordinator/planner/executor/journal прогнаны на 30
  follower slots: 3 signed REAL testnet followers + 27 SHADOW.
- [x] Выполнено 100 циклов open → protection → close; финал — 12 302/12 302
  `CONFIRMED`, ingest drops 0, cleanup и REST audit flat/no-orders.
- [x] SQLite write path усилен autocommit, cursor lifecycle и общим write lock;
  nonce allocation атомарен на том же aiosqlite connection.
- [x] Полный suite: 247 passed / 11 skipped, coverage 87%, ruff и diff-check
  clean. (Счётчик той кампании; актуальный — в блоке третьего прохода ниже.)

Подробности, включая неуспешные промежуточные прогоны и ограничения shadow
evidence: [HYBRID_LOAD_TEST_REPORT_2026-08.md](reports/HYBRID_LOAD_TEST_REPORT_2026-08.md)
и [HYBRID_LOAD_TEST_JOURNAL_2026-08.md](reports/HYBRID_LOAD_TEST_JOURNAL_2026-08.md).
27 shadow slots подтверждают масштаб внутреннего pipeline, но не заменяют 27
независимых signed Aster accounts для 30-real acceptance.

### Финальный gate по внешнему код-ревью (2026-08-23)

Два пункта последнего независимого ревью (см. `REVIEW_STAGE1_FINAL_2026-08.md`):

- [x] **P1 — защитная конвергенция.** Инвариант «одна нога = один bot-owned
  Stop + один TP»: снапшот координатора отдаёт дубликатные условные ордера
  слота (`extra_stops`/`extra_tps`), planner-правило 9 всегда их отменяет
  (приоритет 1, без зависимостей → гарантированный ретрай), включая flat-ветку.
  Попутно закрыт латентный баг: ретрай `CANCEL_ORDER`/`SET_LEVERAGE` после
  FAILED падал на `UNIQUE(command_key)` и физически не мог повториться.
- [x] **ONE_WAY E2E через ядро.** Новый signed-сценарий
  `tests/testnet/test_one_way_engine_e2e.py`: master open → stop → stop move →
  partial close → TP → close через Projector→Coordinator→Risk→Planner→Executor→
  journal→REAL follower (`copyc1`), с аудитом журнала (0 FAILED/UNKNOWN/BLOCKED,
  `reduceOnly=true`, без `positionSide`) и восстановлением режима.
- [x] Полный офлайн-suite на момент того прохода: 256 passed / 12 skipped,
  coverage 87%; ruff check/format clean. (Текущий счётчик — см. ниже,
  третий проход.)

### Dust zero-target edge case — второй проход ревью (2026-08-23)

- [x] **Dust-сокращение до нуля больше не переоткрывает позицию.**
  `sync_basis.zero_target` (миграция #12) персистит намеренный флэт внутри
  живого эпизода мастера; входная ветка skip'ает `INTENTIONAL_FLAT`; маркер
  снимают новый эпизод мастера и `mark_zero_target` (со сбросом
  `issued_target` против глотания идемпотентностью).
- [x] Контрольный тест: ненулевой таргет + ручное закрытие follower'а →
  drift-recovery по-прежнему восстанавливает позицию.
- [x] Live dust-фаза в signed ONE_WAY E2E (phase 4b).

### Третий проход ревью: flip-регрессия + честное dust-evidence (2026-08-23)

Ревьюер вскрыл: (1) фикс №14 клеймил ЛЮБОЙ ноль при живом мастере —
транзитный ноль flip'а блокировал открытие обратной стороны навсегда;
(2) dust-фаза signed E2E молча пропускалась (`0.003*0.005 < step`) и
передавала остаток вместо fill-qty — «live proof» ничего не доказывал.

- [x] **`TargetPosition.hold_flat_until_episode_end`** — явная причина нулевого
  таргета; sticky только для same-direction редукции в ноль, flip-ноль
  транзитный; флаг в `_canonical_target` (разные command keys) (`79be6b1`).
- [x] **Episode-семантика флипа**: flip = close старого episode + open нового с
  `direction=±1`; миграция #13 `copy_trades.direction`; snapshot-синк детектит
  смену знака после офлайна; новый эпизод снимает sticky-маркеры (`8ea3dbd`).
- [x] Офлайн `TestMasterFlipEpisodeSemantics`: plain flip + «dust-flat → flip».
- [x] Signed E2E: dust-фаза ОБЯЗАТЕЛЬНА (assert вместо skip; первый прогон
  честно упал на новом assert'е), остаток мастера = ровно один step,
  `rebuild_full` вместо синтетики, явный recompute_follower в харнессе,
  фаза 4c — реальный signed flip LONG→SHORT через полное ядро (`7122a28`).
- [x] Прогон после фиксов: офлайн 258 passed / 1 skipped (87%); signed testnet
  10 passed / 1 skipped (E2E PASSED 155 c); hybrid gate 100/100 циклов ×
  30 слотов — 12207/12207 CONFIRMED, 0 не-ConFIRMED статусов, 31/31 flat.


## 5. MVP vs Продакшн

### MVP (проверить весь бэкенд)

- Telegram-бот: подключение ключей (прямой ввод V3-набора), настройка риска (% депозита на стоп), уведомления.
- Слежение за мастером через user-data WS (события ордеров/позиций/баланса).
- Исполнение на аккаунтах фолловеров: открытие, закрытие, частичное закрытие, SL/TP, плечо.
- Расчёт размера позиции от риска на стоп; риск-ревард.
- Хранение: SQLite (WAL) + зашифрованное хранилище ключей.
- Вся работа на тестнете, затем малый реальный объём.

### Продакшн (обвязка)

- Онбординг через Aster Code / Builder: мини-апп/страница с кошельком, подпись ApproveAgent+ApproveBuilder.
- Монетизация: builder fee, вывод комиссий (Builder Center) — при доступности на mainnet.
- Sub-accounts для изоляции copy-средств.
- PostgreSQL, бэкапы, мониторинг, rate-limit менеджмент, авто-переподключение со snapshot-recovery.
- Масштаб: лимиты весовых rate limits Астера → пулы/очереди команд.

## 6. Как проводить тесты

### Принцип

Тесты делятся на: (а) без подписи — только проверка маршрутов (400/404), (б) с подписью — полный сценарий на тестнете. Всё гоняется на futures testnet с faucet-средствами, без реальных денег.

### Базовый signed testnet-план (из §8 ресерча, ключевые пункты)

1. Создать master и follower agents (canPerpTrade=true, canWithdraw=false).
2. Прочитать balance/position/open-orders обоих.
3. Открыть master MARKET → скопировать фолловеру MARKET.
4. Проверить фактический fill фолловера и расхождение риска.
5. Частичное закрытие 25/30% (One-way reduceOnly; Hedge — positionSide).
6. SL/TP: создать STOP_MARKET/TAKE_PROFIT_MARKET, переставить cancel+new.
7. OTOCO: placeStrategyOrder → updateStrategyOrder → ручная смена в UI Астера, записать WS payloads.
8. listenKey 60 мин, принудительный disconnect, restore snapshot.
9. Идемпотентность повторов, нехватка маржи, minQty/minNotional, leverage brackets.
10. Builder end-to-end: approveAgent+builder одной подписью → order с feeRate → builderFee в userTrades.

### Критерий прохождения

После каждого сценария фактические позиции и защитные ордера фолловера совпадают с рассчитанным target state; повторная reconciliation не создаёт лишних команд.

## Открытые вопросы (не блокеры)

- Поведение user-data stream при ручной перестановке Strategy OCO/OTOCO в UI Астера — подтверждается signed testnet-тестом.
- Builder-веб-интерфейс на тестнете отсутствует (только API-маршруты) — для теста онбординга нужен свой мини-апп или скрипт.
- Финальные лимиты rate limits на тестнете могут отличаться от mainnet (sentinel -2) — смотреть mainnet exchangeInfo перед продом.

---

Источники: [ASTER_API_RESEARCH_2026-08.md](ASTER_API_RESEARCH_2026-08.md) — status 20–21.08.2026; [REPORTS_FACTUAL_COMPARISON_2026-08.md](reports/REPORTS_FACTUAL_COMPARISON_2026-08.md).
