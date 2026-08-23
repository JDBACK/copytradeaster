# Отчёт о рефакторинге Stage 1 (по итогам review_stage1.md)

Журнал ведётся с 2026-08-22. Формат: инвентаризация проблем → ключевые решения →
журнал выполнения → финальный анализ. Файл предназначен для приложения к ревью.

## 1. Инвентаризация проблем из ревью (с привязкой к коду)

| # | Приоритет | Проблема | Где в коде | Статус |
|---|-----------|----------|------------|--------|
| P1 | P0 | Reconciliation не state-based: `_master_abs` — RAM процесса, обновляется ДО исполнения; restart с пропущенным partial close не сокращает follower | `trading/coordinator.py:77,106-108,317,340` | FIX |
| P2 | P0 | Idempotency key = payload: легитимный повтор команды (два одинаковых close, SL A→B→A) блокируется старым CONFIRMED | `trading/executor.py:76-79` | FIX |
| P3 | P0 | Journal recovery закрывает только UNKNOWN; SENDING/SUBMITTED не разрешаются; SUBMITTED считается терминальным успехом при re-submit | `trading/executor.py`, `resolve_boot_unknowns` | FIX |
| P4 | P0 | Timeout-ветка `_submit_order`: UnboundLocalError на `order`; httpx-таймауты не ловятся | `trading/executor.py:206-209` | FIX |
| P5 | P0 | `_resolve_unknown` ставит CONFIRMED без `_record_link` — recovered STOP/TP выпадает из order_links и own-protective guard | `trading/executor.py:268-276` | FIX |
| P6 | P0 | Nonce нарушает persist-before-send: sync next_nonce + персистенция через create_task | `credentials/service.py:141-151` | FIX |
| P7 | P0 | `pause` декоративен: CLI пишет app_state, Coordinator читает только policy | `cli/main.py`, `coordinator.py` | FIX |
| P8 | P0 | `max_entry_deviation_pct` не подключен; entry от исторического entry; post-fill cap не применяется | `coordinator.py`, `app.py` | FIX |
| P9 | P0 | Stop в прибыли признаётся NO_STOP (сравнение со старым entry) — перенос SL сломан | `coordinator.py:229-242` | FIX |
| P10 | P1 | Один тик движения SL игнорируется (`<= tick`) | `planner.py:120-123` | FIX |
| P11 | P0/P1 | Снапшот follower видит только *_MARKET; TP removal не исполняется; orphan conditionals на flat не убираются | `coordinator.py`, `planner.py` | FIX |
| P12 | P1 | HEDGE rebuild: защитой считается только reduceOnly/closePosition | `master/projector.py` | FIX |
| P13 | P0/P1 | Projector: drop по E теряет TRADE; dedup без trade id; entry от ap | `projector.py` | FIX |
| P14 | P1 | rebuild_full не синхронизирует copy_trades lifecycle; один trade_id на весь план | `projector.py`, `coordinator.py` | FIX |
| P15 | P1 | ONE_WAY: leverage=0 у WS-позиции; SET_LEVERAGE нет для новых позиций | `projector.py`, `planner.py` | FIX |
| P16 | P0 | `App._client_for`: неизвестный account → master client (fail-open) | `app.py:98-104` | FIX |
| P17 | P1 | Масштаб: ~90 weight/follower (openOrders дважды), WS ingest блокируется исполнением | `coordinator.py`, `app.py` | FIX (частично: вес/очередь; symbol-scope — этап 2) |

### Попутно найденные дефекты (не из ревью)

| # | Что | Где | Статус |
|---|-----|-----|--------|
| D1 | `rebuild_full` дважды звал openOrders (2×40 weight) | projector.py | FIX |
| D2 | `_stamp_trade_id` брал первый active trade всех таргетов — links чужих эпизодов | coordinator.py | FIX (per-action binding) |
| D3 | Fake в executor-тестах не возвращал отправленные ордера из query_order — маскировал stuck-SUBMITTED | tests executor journal | FIX |

## 2. Ключевые решения

1. **Persisted basis** (`sync_basis`): per `(account_id, symbol, pos_side)` храним
   `master_ref_abs` + `revision` + `issued_target`. Basis обновляется ПОСЛЕ успешного
   submit плана (безопасное направление: crash → повтор той же редукции, planner даёт no-op).
   Restart: prev basis переживает процесс → `scaled_reduce(current, ref, new)` восстанавливает
   пропущенное сокращение.
2. **Идемпотентность = переход желаемого состояния**: `revision` в basis увеличивается,
   когда координатор выпускает target, отличный от последнего выпущенного. Повторные
   reconcile того же перехода сохраняют revision → тот же command_key. Два одинаковых
   close подряд — разные переходы → разные ключи. Per-action `trade_id` биндит каждое
   действие к эпизоду своей ноги (fallback — последний CLOSED).
3. **Recovery**: boot разрешает SENDING/SUBMITTED/UNKNOWN через query_order по coid;
   найденный ордер → CONFIRMED + record_link; не найденный → FAILED (reconciliation
   перевыпустит). SUBMITTED при повторном submit проверяется запросом.
4. **Nonce**: async NonceSource; App/CLI/скрипты на NonceStore (commit до возврата);
   SqliteCredentials без nonce-поверхности. Транспортные ошибки httpx → AsterError(-1006/-1007)
   на границе адаптера.
5. **Pause**: Coordinator читает app_state.allow_new_entries И policy.allow_new_entries.
6. **Риск**: expected entry = master mark_price; deviation gate до входа; hard clamp
   target ≤ post_fill_cap(follower_entry, эффективный stop); постфилл recompute по
   подтверждению MARKET_OPEN/CLOSE.
7. **Защита**: стоп-валидность от mark_price; для существующей позиции невалидный уровень
   мастера = «не трогать текущую защиту»; planner: tick-grid equality, wanted=None → CANCEL,
   flat+orphan → cancel-only; SET_LEVERAGE для новых ног.
8. **Проектор**: TRADE вне порядка, dedup c trade id, entry от L; rebuild_full:
   HEDGE-защита по «закрывает ногу», синхронизация эпизодов; периодический resnapshot.

## 3. Журнал выполнения

- [2026-08-22] Изучены AGENTS.md, docs/, код, review_stage1.md полностью. Все 17
  позиций ревью подтверждены в коде (§1). Создан план и todo.
- [2026-08-22] **Storage**: миграция `sync_basis`; `SyncBasisRepo`; `CopyTradeRepo.latest_trade/all_open`;
  `CommandRepo.in_flight` вместо unresolved_unknowns.
- [2026-08-22] **Nonce**: async порт; NonceStore в App/CLI/scripts/testnet-fixture;
  чистка SqliteCredentials; httpx-ошибки → AsterError. Юнит-тесты затронутых зон зелёные.
- [2026-08-22] **Executor**: revision-based command_key; переписанный `_submit_order`
  (timeout/SUBMITTED/unknown пути); `_recover_submitted`; links в recovery;
  boot-резолвер трёх статусов. Обновлён executor-journal suite (fake ищет placed).
- [2026-08-22] **Coordinator** переписан целиком: persisted basis (ref после submit),
  pause AND-семантика, стоп-санити от mark, deviation gate, post-fill clamp,
  per-action trade binding, один openOrders fetch, лимитные флейворы в снапшоте.
  Routing/reconciler/e2e тесты переведены на basis-репозиторий.
- [2026-08-22] **Planner**: tick-grid equality; снятие защиты; orphan cleanup на flat;
  SET_LEVERAGE для новых ног. Тесты, закреплявшие дефекты (one-tick noop,
  no-leverage-on-new), заменены контрактными.
- [2026-08-22] **App**: fail-closed `_client_for`; ingest-очередь (drop-oldest);
  периодический master resnapshot; постфилл recompute хук. `_make_client` на NonceStore.
- [2026-08-22] **Projector**: TRADE out-of-order, dedup t, entry math L,
  rebuild_full (один fetch, HEDGE-предикат, lifecycle эпизодов). Тест
  out-of-order-drop заменён контрактными (TRADE применяется, non-trade игнорируется,
  same-ms fills оба применяются). Сьют: 219 passed, ruff clean.

## 4. Финальный анализ (2026-08-22)

### Выполнено

Все 17 позиций ревью закрыты (§1, статус FIX), плюс попутные дефекты D1–D3 из §3
и один найденный при живой отладке:

| # | Что | Где | Статус |
|---|-----|-----|--------|
| D4 | Post-fill recompute вызывался инлайн из `_confirm` внутри удерживаемого per-account `asyncio.Lock` → deadlock (asyncio.Lock нереентерабелен). Исправлено: отдельная таска с дебаунсом | coordinator.py | FIX |

### Верификация

| Проверка | Результат |
|---|---|
| Герметичный сьют (`pytest tests`) | 232 passed, 9 skipped (testnet без CT_TESTNET), ~5 c, процесс завершается детерминированно |
| Ruff (src/tests/scripts) | clean |
| Живой тестнет (`CT_TESTNET=1`, signed-ключи wallet.txt) | 8 passed, 1 skipped (builder — нет main-ключей): server drift, reads, listenKey, master open/partial/stop-replace cycle (HEDGE), insufficient margin, double identical close → обе команды исполнены биржей, one-tick stop replace → замена подтверждена, TP removal → отмена |
| Acceptance-gate e2e (hermetic) | restart-basis recovery; идемпотентность переходов; boot-resolution SENDING/SUBMITTED без дублей (+links); pause блокирует входы; deviation gate; post-fill cap по факту; TP removal; orphan cleanup; стоп-в-прибыли; fail-closed routing |

Примечание к живому прогону: первая попытка «двойного close» воспроизвела баг
ревью в обратную сторону — повторный запуск сценария переиспользовал ключи
(revision=0) и честно пропустил второй close как уже исполненный. Это
подтверждает контракт: тест переведён на per-run epoch-ревизии.

### Ключевые инварианты после рефакторинга

1. Restart-восстановление детерминировано: exchange state + persisted basis → target;
   basis обновляется только после успешного исполнения плана.
2. Идемпотентность = identity перехода (account|episode|kind|revision|payload).
3. Все in-flight статусы журнала разрешимы на boot через query_order c записью order_links.
4. Nonce персистентен до отправки подписанного запроса (async NonceStore).
5. Неизвестный account никогда не маршрутизируется в master (fail-closed).
6. Защита follower не исчезает: removal → cancel; невалидный уровень мастера →
   сохранение текущей защиты; STOP/TAKE_PROFIT флейворы видны в снапшоте и REST-rebuild.

### Осознанные ограничения (не блокеры Stage 1)

- Symbol-scoped snapshot-кэш для 30–100 followers — этап 2 (сейчас 50 weight/follower,
  ingest decoupled очередью).
- Зеркалирование висящих лимиток мастера — этап 2 (как зафиксировано ранее).
- Builder e2e — единственный skip: ждёт main private key в wallet.txt.
- ONE_WAY signed-cycle отдельно не гонялся в этой сессии; логика покрыта юнит/e2e;
  перед этапом 2 прогнать CT_POSITION_MODE=ONE_WAY кампанию на тестнете.

## Последующая проверка масштаба — 2026-08-23

После закрытия review-gate выполнен отдельный hybrid load campaign. Production
`App`/coordinator/planner/executor/journal прогнаны на 30 follower slots:
3 REAL signed testnet followers и 27 явно помеченных SHADOW rows. За 100
циклов получено 12 302/12 302 `CONFIRMED`, dropped events — 0, cleanup и
независимый REST-аудит исходных `main`/`copyc1`–`copyc3` — flat/no-orders.

27 shadow slots не считаются 27 signed Aster accounts: они проверяют только
масштаб внутреннего pipeline. Полный результат, включая найденные на
промежуточных прогонах SQLite/timeout/protective-fill race и их исправления,
зафиксирован в [HYBRID_LOAD_TEST_REPORT_2026-08.md](HYBRID_LOAD_TEST_REPORT_2026-08.md).

Примечание: ограничение про отсутствие отдельного ONE_WAY signed-cycle выше
относится к историческому срезу этого отчёта и закрыто последующим
`FINAL_STAGE1_FOLLOWUP_REPORT_2026-08.md`; hybrid load campaign не заменяет
отдельную signed-проверку позиционного режима.
