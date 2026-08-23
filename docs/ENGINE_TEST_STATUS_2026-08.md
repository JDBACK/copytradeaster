# Статус работ — торговый движок копитрейда (этап 1)

Журнал ведётся с 2026-08-22. Формат: дата → факт → статус. Ветка `codex/stage1-core`.

## Текущее состояние

- Движок работает на futures testnet (`fapi.asterdex-testnet.com`), HEDGE-режим;
  ONE_WAY официально поддержан и покрыт signed E2E через ядро (см. ниже).
- Аккаунты (wallet.txt): `main test` (0x235E…5176, мастер) + copyc1–copyc3.
  copyc4 удалён: его API-кошельки на стороне биржи вели на счёт мейна
  (пересечение 49/50 ордеров подтверждено allOrders).
- Актуальный suite: **254 passed / 12 skipped**, coverage **87%**, ruff check/
  format clean (без CT_TESTNET: testnet-сценарии скипаются маркером).
- Все реальные счёта плоские, открытых ордеров нет (REST-аудит после
  ONE_WAY E2E, 2026-08-23).

## Найденные и исправленные баги (живой тестнет)

| # | Баг | Корневая причина | Фикс |
|---|-----|------------------|------|
| 1 | Осцилляция таргетов свипами | трекер master-магнитуд обновлялся только на WS-дельтах | remember на каждом recompute (`5e32837`) |
| 2 | Deadlock «database is locked» | implicit transactions/cursor lifetime и конкурирующий nonce path при fan-out | autocommit, закрытие cursors, общий `Database.write_lock`, atomic nonce `INSERT…RETURNING`; проверено 30×100 live |
| 3 | Координатор молча пропускал аккаунты | import не создавал copy_settings | дефолтная политика при импорте (`7f4a8b0`) |
| 4 | Дорастивание позиций после partial-close | ветка b считала cap от близкого стопа по устаревшему состоянию | STALE_MASTER guard (<5 c) (`0ae383a`) |
| 5 | Копиры никогда не получают TP | `_targets_for` хардкодил `tp_price=None` | уровень TP мастера в таргете (`b2a892d`) |
| 6 | **Фантомное переоткрытие позиции** | `_apply_fill` брал направление из ноги (LONG→+1): закрывающий SELL = «покупка»; ACCOUNT_UPDATE дублировал дельту | направление только по `S`; AU мутатором не является (`63ac80a`) |
| 7 | Ранний return пропускал присвоение qty | слоты защит чистились до `current.qty = new_qty` | очистка inline, без return (`e9ac649`) |
| 8 | Ре-вход после своего сработавшего ТП | guard покрывал только стопы | OWN_PROTECTIVE_FILLED: стоп ИЛИ тейк активного эпизода (`ff7f490`) |
| 9 | STOP/TAKE_PROFIT (лимитные флейворы) игнорировались | проектор знал только *_MARKET | слоты принимают оба флейвора, тип ордера копируется (`f85c9a8`) |
| 10 | Лимитные защиты отклоняются (-1102) | не отправлялся обязательный `price` | planner выводит price = trigger ±0.05% (`bce8c5b`) |
| 11 | -4014 Price not increased by tick size | `quantize("0.10")` держит 2 знака вместо сетки 0.1 | выравнивание делением на тик (`d834e9c`) |
| 12 | **Stale защита после FAILED dependent cancel** | `_pick()` снапшота выбирал только новейший Stop/TP: при `PLACE B CONFIRMED → CANCEL A FAILED` следующий reconcile видел current==target и выдавал пустой план — старый уровень висел рядом с новым | снапшот отдаёт дубликаты слота (`extra_stops`/`extra_tps`), planner-правило 9 всегда их отменяет, включая flat-ветку |
| 13 | **Ретрай CANCEL/SET_LEVERAGE после FAILED невозможен** | `_submit_cancel`/`_submit_leverage` всегда делали INSERT и падали на `UNIQUE(command_key)` при повторе той же транзиции | существующая FAILED-строка переводится в PENDING и ретраится под исходным командным ключом (вскрыто e2e-тестом пункта 12) |

## Известные ограничения (осознанные решения этапа 1)

1. **ACCOUNT_UPDATE информационный**: позицию мутирует только TRADE-дельта;
   пропущенный ивент = дрейф до reconnect (логируется, лечится rebuild_full).
2. **Вход копиров всегда MARKET**: лимитка мастера копируется в момент
   исполнения (fill-copy). Зеркалирование висящих лимиток — этап 2.
3. `availableBalance` тестнета колеблется сильнее wallet balance — сайзинг
   считается от него (policy risk_base=available_balance).
4. Возможны дублирующие стопы одного уровня у копира при быстрой конвергенции
   размера; биржа нормализует reduceOnly, переполнения не происходит
   (наблюдение 02:50 UTC; кандидат на полировку).

## Кампания полной проверки (12 пунктов) — 2026-08-22

| Пункт | Статус | Результат |
|-------|--------|-----------|
| 1. Покупка long | ✅ | копиры BUY + стопы, ~7 c, WS-путь |
| 2. Продажа short | ✅ | SELL + BUY-стопы, ~7 c |
| 3. Тейк-триггер | ✅ | TP @77942/77847 сработал мастеру и всем копирам в одну секунду |
| 4. Стоп-триггер | ✅ | стоп @78073 → TRADE l=0.002, все копиры вышли своими стопами |
| 5. Перенос стопа | ✅ | place-new→cancel-old, мелкие движения 10-15$ |
| 6. Перенос тейка | ✅ | move-tp 78200→77942, PLACE_TP+CANCEL ×3, ~5 c |
| 7. Несколько позиций | ✅ | BTC long + SOL long одновременно, независимые эпизоды |
| 8. Казусы | ✅ | kill -9 с позой → recovery без дублей; быстрый цикл эпизодов; ручная отмена защиты мастером → корректный NO_STOP |
| 9. Вход без стопа→set-stop→set-tp | ✅ | SKIP(NO_STOP) → вход после set-stop → PLACE_TP после set-tp |
| 10. Вход лимиткой | ✅ | висел 2.5 ч без действий копиров; филл → реакция за секунды |
| 11. Лимитные защитные (STOP) | ✅ | copyc1–3 держат type=STOP sp=78201 px=78161.90 (биржа) |
| 12. Сверка ордеров с биржей по clientOrderId | ✅ | каждый прогон |

## Инфраструктура прогонов

- Скрипт `scripts/manual_master_order.py`: open/open-nostop (long/short),
  set-stop/move-stop, set-tp/move-tp, partial-close, close.
- Движок — hub-процесс `engine`; журнал команд — SQLite `execution_commands`,
  события мастера — `master_events`; сверка — query_order/allOrders по coid.

## Рефакторинг по review_stage1 (2026-08-22)
Проведён рефакторинг по итогам внешнего код-ревью: persisted sizing basis
(restart после пропущенного partial close), идемпотентность по ревизии перехода,
boot-recovery SENDING/SUBMITTED/UNKNOWN с order_links, async persist-before-send
nonce, реальный pause, deviation/post-fill риск, перенос стопа в прибыли,
tick-grid равенство, TP removal/orphan cleanup, HEDGE-rebuild защита, TRADE вне
порядка + entry от L, fail-closed routing, decoupled WS ingest.
Полная инвентаризация и верификация — [REFACTOR_STAGE1_REPORT.md](../REFACTOR_STAGE1_REPORT.md).
На момент закрытия review-specific доработок герметичный сьют дал **240 passed /
10 skipped** без
`CT_TESTNET`, `ruff check` и `ruff format --check` — clean. Signed testnet:
**9 passed / 1 skipped**; отдельно после финальной правки dependency gate
повторены one-tick stop replace и ONE_WAY cycle — **2 passed**. Read-only
сверка после cleanup: `main` снова HEDGE, `main` и copyc1–copyc3 flat,
открытых ордеров нет. Единственный skip — Builder e2e, который не блокирует
торговое ядро Stage 1 и перенесён в контур Stage 3.

## Hybrid load campaign — 2026-08-23

Проведён отдельный testnet-only нагрузочный гейт перед ревью:

- `scripts/hybrid_load_test.py`, 30 follower slots, 100 циклов;
- 3 REAL подписанных follower-а (`copyc1`–`copyc3`) + 27 явно помеченных
  SHADOW rows; fake credentials не создавались;
- **12 302/12 302 `CONFIRMED`**, `FAILED/UNKNOWN/non-terminal=0`,
  `ingest_dropped=0`, cleanup errors=0;
- run-БД до/после cleanup: 31 account flat/no-orders;
- независимый signed REST после прогона: `main` и copyc1–copyc3 flat,
  `open_orders=0`;
- `CT_LOAD=1` production-path integration: **1 passed** (30×100 shadow
  cycles), full suite после изменений: **247 passed / 11 skipped**.

Финальный JSON: `data/hybrid-load-20260822T233258Z.json`. Полная матрица
evidence и ограничения shadow-контура: [HYBRID_LOAD_TEST_REPORT_2026-08.md](../HYBRID_LOAD_TEST_REPORT_2026-08.md).

## Финальный gate по внешнему код-ревью — 2026-08-23

Текст ревью: [review_stage1_final.md](../review_stage1_final.md). Два пункта:

### 1. P1 — защитная конвергенция (последний code blocker)

Сценарий ревьюера воспроизведён и закрыт на трёх уровнях:

- юнит (`test_planner_transitions.py::TestDuplicateProtectionConvergence`):
  extras отменяются при совпадающем target, рядом с обычной заменой (без
  двойного cancel), для TP и в flat-ветке;
- e2e на FakeAster (`test_end_to_end_fake.py::TestDuplicateProtectionConvergence`):
  инжект FAILED dependent cancel после CONFIRMED replacement → оба уровня
  висят → следующий reconcile удаляет stale, журнал содержит CONFIRMED CANCEL
  по старому coid, повторный проход выдаёт 0 команд; то же для TP и flat;
- **побочный наход e2e:** ретрай CANCEL/SET_LEVERAGE после FAILED падал на
  `UNIQUE(command_key)` — зависимый cancel не мог повториться вообще. Исправлено
  (FAILED → PENDING под исходным ключом), покрыто тем же e2e.

### 2. ONE_WAY signed E2E через ядро

Новый `tests/testnet/test_one_way_engine_e2e.py`: master open → stop →
stop move (замена защиты) → partial close → TP → master close, всё через
Projector→Coordinator→Risk→Planner→Executor→journal на REAL follower `copyc1`.
Живой прогон 2026-08-23: **PASSED за 36 с**. Аудит журнала: 0 FAILED / UNKNOWN /
BLOCKED; все закрывающие команды с `reduceOnly=true` и без `positionSide`;
финал — оба аккаунта flat, открытых ордеров нет. Первый запуск вскрыл ошибки
самого сценария (снятие baseline после события, неверное ожидание при
минимальном размере копира) — предикаты приведены к семантике движка.

### Итог прогона

- Полный офлайн-suite: **254 passed / 12 skipped**, coverage 87%; ruff check +
  format clean.
- Signed testnet (CT_TESTNET=1): прежние сценарии **9 passed / 1 skipped**
  (Builder — нет main private key, Stage 3) + новый ONE_WAY E2E **passed**
  (36 c) = 10 passed из 10 исполняемых.
- Оба пункта ревьюера закрыты → основание зафиксировать `Stage 1 = CLOSED`
  без нового большого код-ревью (по шкале самого ревьюера).
