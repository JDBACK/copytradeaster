# Журнал финального gate по внешнему код-ревью — 2026-08-23

Хронология работы по двум пунктам последнего независимого ревью
([review_stage1_final.md](review_stage1_final.md)). Формат — как в
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

## 6. Документация

- docs/DEV_PLAN_SKELETON.md — чеклист «Финальный gate по внешнему код-ревью».
- docs/ENGINE_TEST_STATUS_2026-08.md — баги #12/#13, раздел gate, счётчики suite.
- docs/ARCHITECTURE.md §6 — инвариант конвергенции защиты (правило 9).
- review_stage1_final.md — сохранённый текст ревью.

## Статус

Оба пункта gate закрыты с живыми signed-доказательствами → основание
зафиксировать Stage 1 = CLOSED (по критериям самого ревьюера) и переходить к
Stage 2. Неблокирующие хвосты без изменений: startup-gap resnapshot,
symbol-scoped follower snapshot к 100 копирам, Builder в Stage 3.
