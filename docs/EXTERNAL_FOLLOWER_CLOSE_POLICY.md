# Политика ручного закрытия позиции follower

Статус: **IMPLEMENTED / OFFLINE + ASTER TESTNET VERIFIED**
Дата решения и реализации: **2026-09-12**

## Коротко

Если follower сам закрыл позицию, пока master всё ещё держит эту сделку, движок
не должен переоткрывать позицию этого follower. Закрытие считается отказом от
текущего master episode; повторный вход возможен только после новой master-сделки
или явного re-arm.

## Сценарий

Master открыл позицию, и она была скопирована на несколько follower-аккаунтов.
Один follower заходит в Aster и сам закрывает свою позицию, пока master всё ещё
держит ту же сделку. Причина закрытия не важна: это может быть фиксация прибыли
или эмоциональное закрытие убытка.

Правило относится к конкретному follower, символу и направлению позиции. Остальные
followers продолжают копирование без изменений.

## Подтверждённое прежнее поведение

До этой реализации authoritative follower snapshot видел только факт, что позиция стала flat.
При активном master это трактуется как расхождение `actual != target`, поэтому:

- на следующем событии master или периодической authoritative-сверке движок может
  снова отправить `MARKET_OPEN` для этого follower;
- стандартный интервал периодической сверки — `reconcile_interval_s = 15` секунд;
- размер новой позиции пересчитывается по текущему балансу, цене, Stop и risk policy,
  поэтому он не обязан совпасть с прежним;
- после входа движок снова приводит защитные Stop/TP к целевому состоянию.

Прежнее поведение было подтверждено зелёной offline-регрессией
`tests/integration/test_end_to_end_fake.py::TestDustReductionNoReentry::test_manual_close_with_nonzero_target_still_recovers`:
тест прямо требовал восстановить вручную закрытую позицию. После изменения
контракта этот исторический тест удалён и заменён обратной регрессией
`test_manual_close_blocks_reentry_until_new_episode_and_after_restart`.

Закрытие через собственный подтверждённый Stop/TP движка уже имеет отдельный
`OWN_PROTECTIVE_FILLED` guard и не должно переоткрывать позицию до новой master-сделки.
Dust-flat, намеренно созданный пропорциональным сокращением, также имеет отдельный
sticky-маркер. Эти случаи нельзя смешивать с ручным закрытием follower.

Follower private stream Mini App обновляет presentation snapshot и не является
торговым разрешением на повторный вход. Обнаружение расхождения выполняется через
authoritative reconciliation.

## Действующее product-правило

Ручное или иное внешнее полное закрытие follower при ещё активном master считается
явным отказом follower от текущей master-сделки, а не обычным drift для ремонта.

После такого закрытия движок должен:

1. оставить конкретного follower flat;
2. не выполнять автоматический повторный вход в рамках того же master episode;
3. сохранить этот запрет после периодических сверок и рестарта процесса;
4. разрешить повторный вход только после новой master-сделки или явного
   подтверждённого re-arm для этого follower.

P&L закрытия — положительный, отрицательный или нулевой — не меняет правило.
Новая master-сделка означает новый episode, а не просто очередную проверку того же
самого открытого master-положения.

## Реализация

- Migration #60 расширяет `sync_basis` полями `episode_trade_id`,
  `last_observed_qty`, `last_engine_close_key`, `external_close_blocked` и
  `external_close_reason`. Маркер тем самым привязан к account/symbol/position side
  и точному `copy_trades.trade_id`, переживает restart и не смешивается с dust.
- Coordinator обновляет observation только после полного follower snapshot
  (`balance + openOrders + positionRisk`). Ошибка любого REST-read не создаёт
  переход и не меняет маркер; private stream/UI не участвуют в решении.
- При `non-zero → flat` journal-cursor отличает новую подтверждённую
  `MARKET_CLOSE` движка, а её quantity должна покрывать предыдущий authoritative
  qty: соседняя partial-reduction не может объяснить полный flat. Для linked
  bot-owned Stop/TP сохраняется отдельная проверка `FILLED` и guard
  `OWN_PROTECTIVE_FILLED`. Если ни одна причина не доказана, безопасная
  классификация — `EXTERNAL_OR_UNKNOWN`, без re-entry.
- Блокированный flat получает zero-target: оставшиеся bot-owned Stop/TP
  отменяются, но latch не снимается ни этой отменой, ни очередным reconcile.
- `CopyTradeRepo.open_trade()` сбрасывает старый latch и observation baseline
  только на границе нового master episode. `SyncBasisRepo.rearm_external_close()`
  предоставляет persistence-seam для будущего отдельно подтверждаемого re-arm.
- Skip `EXTERNAL_FOLLOWER_CLOSE` создаёт понятное уведомление: позиция закрыта вне
  копитрейдинга, повторный вход отключён до следующей сделки master.
- Executor теперь сохраняет в journal per-action `trade_id`, который уже входил
  в command identity; ранее coordinator-планы ошибочно записывали nullable
  `plan.trade_id`, поэтому engine-close нельзя было надёжно связать с episode.

## Минимальная acceptance-матрица

| Сценарий | Результат / evidence |
|---|---|
| Ручное закрытие follower независимо от P&L | PASS offline + REAL Testnet: follower остаётся flat; P&L не является входом классификатора |
| Очередное событие master по другому символу | PASS offline: latch сохраняется, нового `MARKET_OPEN` нет |
| Периодическая authoritative-сверка | PASS offline + REAL Testnet: несколько reconcile оставляют follower flat |
| Рестарт процесса | PASS offline + REAL Testnet: новый Coordinator на той же SQLite восстанавливает latch |
| Master закрыл старую и открыл новую сделку | PASS offline + REAL Testnet: новый `trade_id` снимает старый запрет, вход проходит обычные gates |
| Явный re-arm в том же episode | Persistence-seam покрыт unit; публичный подтверждаемый UI/CLI action ещё не выставлен |
| Сработал собственный Stop/TP движка | PASS offline: остаётся `OWN_PROTECTIVE_FILLED`, external latch не ставится |
| Engine dust-close | PASS offline: `zero_target` ставится, external latch не ставится |
| Частичный engine-close рядом с полным внешним close | PASS offline: недостаточный engine-close не маскирует внешний переход в flat |
| Временная ошибка чтения follower | PASS offline: неполный snapshot не ставит marker; следующий полный read фиксирует переход |

Основная offline-регрессия:
`tests/integration/test_end_to_end_fake.py::TestDustReductionNoReentry::test_manual_close_blocks_reentry_until_new_episode_and_after_restart`.
Подписанный сценарий:
`tests/testnet/test_one_way_engine_e2e.py::test_external_follower_close_stays_flat_until_next_master_episode`.
Эксклюзивный Aster Testnet rerun 2026-09-12: **PASS за 68.86s**, включая direct
reduce-only close follower, restart Coordinator, реальную новую master episode и
финальный signed audit `main/copyc1 = flat + zero open orders`.
Повторный эксклюзивный прогон после финального аудита (вместе с margin-mode
сценарием) также прошёл: оба live-теста **2 passed за 173.23s**; bot после
`SIGCONT` снова достиг `READY`.

## Остаточное ограничение

Пользовательского same-episode re-arm пока нет в Telegram/Mini App/CLI. До появления
отдельного подтверждаемого control-plane action нормальный путь — ждать следующую
master-сделку. Liquidation/ADL безопасно попадают в `EXTERNAL_OR_UNKNOWN` и не
переоткрываются, но отдельная user-facing классификация этих причин не реализована.
