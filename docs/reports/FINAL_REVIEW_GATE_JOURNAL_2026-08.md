# Журнал финального gate по внешнему код-ревью — 2026-08-23

Хронология работы по двум пунктам последнего независимого ревью
([review_stage1_final.md](../reviews/review_stage1_final.md)). Формат — как в
HYBRID_LOAD_TEST_JOURNAL_2026-08.md: время (MSK) → действие → факт.

## 1. Изучение ревью и кода

- Разобран текст ревью: actionable = 2 пункта (P1 protective convergence,
  ONE_WAY E2E через ядро); startup gap / Builder / snapshot optimization /
  coverage-gate явно не блокирующие.
- Прочитаны planner.py, executor.py, coordinator.py, projector.py, app.py,
  domain/models.py, risk/engine.py, reconciler.py, fake_aster.py, e2e и
  signed-тесты. Подтверждён механизм дефекта: `_pick()` снапшота отдаёт только
  новейший ордер слота → `current==target` → пустой план при висящем дубле.

## 2. Реализация P1

- `domain/models.py`: `PositionState.extra_stops/extra_tps`.
- `coordinator.py`: `_pick_with_extras()` для позиции и orphan-ветки; мёртвый
  `_pick()` удалён.
- `planner.py`: правило 9 (cancel extras, приоритет 1, без зависимостей);
  flat-ветка тоже отменяет extras.
- **Побочный наход:** первые e2e упали на
  `UNIQUE constraint failed: execution_commands.command_key` — ретрай
  CANCEL/SET_LEVERAGE после FAILED физически невозможен (`_submit_cancel`/
  `_submit_leverage` всегда INSERT). Исправлено: существующая FAILED-строка →
  PENDING под исходным ключом. Это усилило фикс: без него stale-защита не
  ретраилась бы вообще.
- Юнит-тесты: `TestDuplicateProtectionConvergence` (4 сценария). Первый прогон
  поймал ошибку в ожидании теста (при замене уровня выбранный stop тоже
  отменяется) — ожидание исправлено на корректную семантику.

## 3. E2E на FakeAster

- Инжект FAILED dependent cancel после CONFIRMED replacement → оба уровня
  висят → следующий reconcile удаляет stale; журнал содержит CONFIRMED CANCEL
  по старому coid; повторный проход выдаёт 0 команд. Аналогично TP и flat.
- Именно здесь вскрылся баг ретрая (см. выше) — до фикса executor падал с
  IntegrityError вместо повторной отправки cancel.

## 4. ONE_WAY E2E через ядро

- Новый `tests/testnet/test_one_way_engine_e2e.py`: master open → stop →
  stop move → partial close → TP → master close; Projector→Coordinator→Risk→
  Planner→Executor→journal→REAL follower copyc1; журнальный аудит + восстановление HEDGE.
- Прогон 1 (в составе полного testnet-набора): FAILED на фазе partial reduction.
  Разбор: две ошибки сценария — baseline снимался ПОСЛЕ apply_raw (фоновый
  recompute успевал сократить позицию), и предикат требовал `0 < actual`,
  тогда как при минимальном размере копира пропорциональное сокращение по сетке
  даёт 0 и движок корректно закрывает всю ногу. Код движка дефекта не показал:
  фазы входа/замены стопа прошли live до падения.
- Прогон 2 после правки предикатов (ожидание = scaled_reduce-семантика):
  **PASSED за 36 c**. Аудит: 0 FAILED/UNKNOWN/BLOCKED, все закрывающие команды
  `reduceOnly=true` без `positionSide`, оба аккаунта flat, открытых ордеров нет.

## 5. Полные прогоны

- Офлайн: 254 passed / 12 skipped, coverage 87% (coordinator 90%, executor 89%,
  planner 98%), ruff check/format clean.
- Signed testnet CT_TESTNET=1: прежние 9 passed / 1 skipped (Builder skip — нет
  main private key) + ONE_WAY E2E passed.

(Счётчики зафиксированы на момент этого прохода; текущие — в разделе 8.)

## 6. Документация

- docs/DEV_PLAN_SKELETON.md — чеклист «Финальный gate по внешнему код-ревью».
- docs/ENGINE_TEST_STATUS_2026-08.md — баги #12/#13, раздел gate, счётчики suite.
- docs/ARCHITECTURE.md §6 — инвариант конвергенции защиты (правило 9).
- review_stage1_final.md — сохранённый текст ревью.

## 7. Второй проход ревью — dust zero-target edge case (2026-08-23)

Ревьюер проверил ветку в GitHub, принял оба пункта gate и нашёл последний
edge case: `scaled_reduce → 0` внутри живого эпизода мастера стирал basis
(`clear()`), и следующий post-fill recompute трактовал намеренный флэт как
дрейф — немедленный re-entry.

Реализация:
- миграция #12: колонка `sync_basis.zero_target`;
- `SyncBasisRepo.mark_zero_target/is_zero_target/clear_zero_target`; при
  маркировке сбрасывается и `issued_target` (отладка e2e вскрыла: без этого
  следующий ненулевой таргет совпадал с нулевым в ревизии, новый MARKET_OPEN
  получал command_key исторического CONFIRMED OPEN и глотался
  идемпотентностью);
- `_persist_refs`: `t.qty == 0` + мастер в позиции → `mark_zero_target`,
  реальное закрытие эпизода → `clear` как раньше;
- входная ветка координатора: skip `INTENTIONAL_FLAT` (новый SkipReason);
- `CopyTradeRepo.open_trade`: новый эпизод мастера снимает маркеры ноги.

Тесты: `TestDustReductionNoReentry` (2 сценария: dust-флэт держится через 3+
reconcile/stop-move до нового эпизода; контрольный — ненулевой таргет +
ручное закрытие → восстановление той же величины). Live: phase 4b добавлена в
signed ONE_WAY E2E. Полный офлайн: 256 passed / 12 skipped, coverage 87%.

## 8. Третий проход ревью — flip-регрессия + фальшивое dust-evidence (2026-08-23)

Ревьюер (уже после «Stage 1 закрыт») перечитал итоговый код и нашёл два дефекта:

1. **Фикс №14 сломал ONE_WAY flip**: `_persist_refs` клеймил любой `qty=0` при
   живом мастере как intentional-flat. Транзитный ноль ветки flip («мастер
   перевернулся — закрой эту сторону, обратную открой следующим проходом»)
   получал sticky-маркер → SHORT не открывался никогда.
2. **Dust-фаза signed E2E не выполнялась**: guard `dust_left >= step` при
   `0.003*0.005=0.000015 < step=0.001` молча пропускал всю фазу; в событие
   передавался остаток позиции вместо fill-qty. «PASSED за 38 c» ничего не
   доказывал.

Исправление — глобальное, по требованию ревьюера:

- `TargetPosition.hold_flat_until_episode_end` — явная причина нулевого таргета;
  sticky только для same-direction редукции в ноль; флаг в `_canonical_target`
  (разные command keys) (`79be6b1`).
- Episode-семантика флипа: `_apply_fill` при flip = close+open нового эпизода с
  `direction`; миграция #13; snapshot-синк детектит смену знака после офлайна;
  новый эпизод снимает sticky-маркеры (`8ea3dbd`).
- Офлайн-регресс `TestMasterFlipEpisodeSemantics`: оба сценария ревьюера (`c374589`).
- Signed E2E переписан: dust-фаза обязательна (assert вместо skip), остаток
  мастера = ровно step, `rebuild_full` вместо синтетики, явный recompute_follower,
  фаза 4c — реальный signed flip через полное ядро (`7122a28`).

Промежуточные прогоны журналируются честно:
- run 4: E2E упал на НОВОМ assert'е dust-фазы (`dust_left=0 < step`) — прямое
  доказательство того, что раньше фаза никогда не исполнялась;
- run 5: таймаут dust-фазы — вскрыто, что `rebuild_full` не эмитит delta, а у
  голого харнесса нет reconcile-цикла; добавлен явный recompute_follower;
- run 6: flip-ордер с `reduce_only=True` обрезался биржей до флэта; фазы 5–6
  захардкожены под лонг (-2021 на шорте) — направленная нейтральность;
- run 7: **PASSED за 155 с** — все фазы, включая обязательную dust и live-flip.

Финальные прогоны: офлайн 258 passed / 1 skipped (87%); signed testnet
10 passed / 1 skipped; hybrid gate 100/100 × 30 слотов — 12207/12207 CONFIRMED,
0 FAILED/UNKNOWN/SUBMITTED/SENDING/BLOCKED, ingest_dropped=0, cleanup errors=0,
shadow_signed_requests=0, 31/31 flat до и после. Артефакты:
`data/hybrid-load-20260823T075504Z.{json,sqlite3}`.

## Статус

Все пункты трёх проходов ревью закрыты: protective convergence (+retry),
ONE_WAY через ядро, dust zero-target, flip-регрессия, честное dust-evidence —
каждое с офлайн-регрессом и живым signed-доказательством. Решение о статусе
этапа (закрытие Stage 1 / переход к Stage 2) — за владельцем проекта и
ревьюером, не за исполнителем.
Неблокирующие хвосты без изменений: startup-gap resnapshot, symbol-scoped
follower snapshot к 100 копирам, Builder в Stage 3.
