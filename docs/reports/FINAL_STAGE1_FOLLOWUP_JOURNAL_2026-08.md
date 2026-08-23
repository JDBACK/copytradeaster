# Журнал финальных доработок Stage 1 — 2026-08

Цель: закрыть замечания `review_stage1_new.md`, проверить их тестами и провести
честную signed-проверку только на локальных testnet-кошельках AsterDex.

## 2026-08-22 — старт и инвентаризация

- Изучены `AGENTS.md`, архитектура, API-исследование, план Stage 1, отчёты
  `REFACTOR_STAGE1_REPORT.md` и `docs/ENGINE_TEST_STATUS_2026-08.md`.
- Проверено состояние git: ветка `codex/stage1-core`, рабочее дерево до начала
  доработок чистое.
- Полностью разобрано `review_stage1_new.md`. Подтверждены следующие задачи:
  1. не отменять старую защиту, если новая защитная установка не подтверждена;
  2. выдавать новую execution generation при фактическом drift при неизменном
     target;
  3. удалить дубликат `_pick_protective` и добавить HEDGE REST-rebuild кейс;
  4. принимать решения по stop/deviation на свежем mark price;
  5. делать немедленный authoritative resnapshot после MARGIN_CALL/liquidation;
  6. провести signed ONE_WAY campaign.
- Дополнительно включены в план: атомарная SQLite-выдача nonce и передача
  фактически выбранного leverage в margin check.

## Правило тестирования

Hermetic unit/integration tests используются только для детерминированной
проверки инвариантов и не выдаются за проверку биржи. Signed-сценарии запускаются
только с `CT_TESTNET=1`, endpoint testnet и ключами из локального `wallet.txt`;
mainnet и реальные средства не используются.

## 2026-08-22 — первая верификация кода

- Добавлен dependency gate в `ExecutionAction`/planner/executor: старый
  protective order не отменяется, пока новый не получил `CONFIRMED`.
- Добавлена новая revision generation для уже подтверждённой команды, если
  свежий follower snapshot снова показывает drift при том же target.
- Удалено дублирующее определение `_pick_protective`; HEDGE REST-предикат
  учитывает сторону ноги даже при `reduceOnly=false` и `closePosition=false`.
- Добавлен symbol-scoped master `positionRisk` refresh перед обработкой delta;
  критические `MARGIN_CALL`/liquidation-like события вызывают callback
  authoritative resnapshot + reconcile в `App`.
- `NonceStore` переведён на атомарный SQLite upsert с `RETURNING`; sizing
  получает фактически выбранное плечо, а не только лимит policy.
- При проверке найден и исправлен дополнительный race post-fill recompute:
  задача теперь ждёт окончания текущего account mutation plan, чтобы не увидеть
  промежуточное состояние до установки защиты.
- Проверка: `ruff check src tests scripts` — clean; выбранный контур —
  **99 passed**.

## 2026-08-22 — signed testnet verification

- Полный signed-набор с `CT_TESTNET=1`: **9 passed, 1 skipped**. Skip — только
  Builder e2e, поскольку в локальном хранилище нет main private keys.
- ONE_WAY proof после финальной правки dependency gate: **passed**. Цикл
  включал MARKET open, STOP place, cancel+new replacement, partial close,
  TAKE_PROFIT place/cancel и flatten.
- Финальный targeted signed rerun (one-tick replacement + ONE_WAY):
  **2 passed**.
- Read-only signed REST-сверка после cleanup: testnet endpoint; `main` снова в
  HEDGE; `main`, `copyc1`, `copyc2`, `copyc3` — flat, `open_orders=0`.

## 2026-08-22 — финальная повторная кампания

- После последней косметической правки Coordinator повторно проверен targeted
  hermetic-контуром: **26 passed**.
- Полный signed testnet-набор на окончательном состоянии ветки: **9 passed,
  1 skipped** (только Builder).
- Финальный signed REST snapshot снова подтвердил: endpoint testnet, `main`
  HEDGE, все четыре локальных аккаунта flat, открытых ордеров нет.

## 2026-08-23 — собственный full hybrid load gate

- Нагрузка выполнена на 30 слотах без фиктивных приватных ключей: 3 REAL
  signed testnet-аккаунта и 27 явно маркированных SHADOW-аккаунтов.
- Выполнено 100 циклов: 12 302 из 12 302 команд получили `CONFIRMED`,
  `ingest_dropped=0`, неразрешённых `FAILED`/`UNKNOWN`/non-terminal статусов
  нет; cleanup и независимый signed REST-аудит завершились чисто.
- В итоговый контур вошли общий SQLite write lock и закрытие cursor lifecycle,
  атомарная выдача nonce, timeout/unknown recovery и детерминированный SHADOW
  adapter.
- Подробности и артефакты прогона: [HYBRID_LOAD_TEST_REPORT_2026-08.md](HYBRID_LOAD_TEST_REPORT_2026-08.md).
