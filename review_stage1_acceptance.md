# Ревью №3 (финальное принятие) — codex/stage1-core после corrective pass

Дата: 2026-08-23. Текст ревьюера сохранён дословно.

---

Проверил ветку `codex/stage1-core` уже после третьего corrective pass. **Да — теперь я принимаю Stage 1.**

# **Stage 1 = CLOSED ✅**

Причина простая: три конкретных условия, которые я оставил в прошлом ревью, теперь действительно выполнены в коде, а не только описаны в отчёте.

### Flip / `zero_target` — закрыто правильно

В `TargetPosition` теперь есть явная семантика причины нулевого target:

```python
hold_flat_until_episode_end: bool = False
```

Она входит и в canonical target, поэтому sticky dust-zero и transitional flip-zero больше не могут случайно разделить один idempotency transition. Главное — flip-ветка явно создаёт `qty=0` с `hold_flat_until_episode_end=False`, поэтому старый `zero_target` больше не защёлкивается на LONG→SHORT.

Projector тоже исправлен фундаментально: sign flip теперь действительно означает

```text
LONG episode CLOSED
        ↓
SHORT episode OPEN
direction = -1
```

а `rebuild_full()` умеет обнаружить несовпадение направления после offline/restart. Это именно та episode semantics, которой не хватало.

### Регрессия flip покрыта нормально

Добавлены два хороших core E2E:

```text
LONG → SHORT
```

и более опасный:

```text
LONG
→ dust flat + zero_target
→ master flip
→ marker очищается
→ follower SHORT открывается
```

В тестах проверяется не только знак позиции, но и новый `trade_id`, `direction=-1`, отсутствие sticky marker после flip и устойчивость после повторных reconcile.

Это закрывает мой предыдущий blocker.

### Signed dust evidence теперь настоящее

Старый ложный green устранён.

Больше нет:

```python
if dust_possible:
    ...
```

Теперь scenario **обязательный**:

```python
assert expected_target > 0
dust_left = step
assert dust_left < abs(master_left) < open_qty
```

То есть если dust-сценарий физически не создан, весь test падает.

Ещё важнее: после настоящего signed SELL мастерское состояние берётся через:

```python
await projector.rebuild_full(client, "main")
```

То есть ошибка предыдущей версии «remaining qty подали как fill qty» исчезла полностью.

А затем live test действительно делает настоящий:

```text
master LONG +dust
        ↓
master SELL 2*dust, reduceOnly=False
        ↓
master SHORT -dust
        ↓
rebuild_full
        ↓
SHORT stop
        ↓
Coordinator / Risk / Planner / Executor
        ↓
REAL follower SHORT
```

В коде есть обязательный assert, что master реально оказался `< 0`, и затем ожидание `follower < 0`.

То есть **signed ONE_WAY теперь действительно проверяет и dust, и реальный sign-flip**.

---

## Нагрузочный gate

В финальном gate-журнале зафиксирован новый post-fix run:

* 100/100 циклов;
* 30 follower slots;
* `12 207 / 12 207 CONFIRMED`;
* 0 `FAILED / UNKNOWN / SUBMITTED / SENDING / BLOCKED`;
* `ingest_dropped=0`;
* cleanup errors = 0;
* 31/31 flat;
* offline `258 passed / 1 skipped`;
* signed testnet `10 passed / 1 skipped`.

Для вашей архитектуры это уже достаточный Stage-1 acceptance gate.

---

## Нашёл ли я ещё blocker?

**Нет.**

Есть две мелочи, но я **не считаю их основанием снова держать Stage 1 открытым**.

Первая — документация чуть расходится с буквальной реализацией. ARCHITECTURE говорит, что `hold_flat_until_episode_end=True` только для proportional dust reduction.

А фактический Coordinator выставляет его после итогового:

```python
dust_flat = target_abs == 0 and current_abs > 0
```

то есть потенциально sticky может стать и safety-reduction до нуля через risk cap.

Это скорее **консервативное safety-поведение**, а не опасный дефект: бот останется flat вместо нежелательного повторного увеличения позиции. Позже можно либо уточнить комментарий/docs, либо различить source-of-zero. Stage 1 из-за этого держать бессмысленно.

Вторая — migration #13 создаёт `copy_trades.direction` с default `0`.  Для уже существующего OPEN episode из старой БД snapshot не backfill'ит `0 → текущий sign`; mismatch определяется только если сохранённое direction уже ненулевое.  Это transient migration-hardening для старой БД. Для нового episode direction уже записывается правильно. Можно когда-нибудь добавить:

```python
if active_direction == 0:
    update direction = pos_sign
```

но это P2 polish, не Stage-1 blocker.

---

## Единственная документационная нестыковка

`FINAL_REVIEW_GATE_JOURNAL` уже содержит новый hybrid `20260823 / 12207 commands`.

Но основной `HYBRID_LOAD_TEST_REPORT_2026-08.md` всё ещё описывает **предыдущий** run:

```text
20260822T233258Z
12 302 commands
```

И новый `data/hybrid-load-20260823T075504Z.json` в GitHub не лежит — путь сейчас даёт 404. Поэтому числа последнего hybrid run я могу подтвердить как **зафиксированные журналом**, но не независимо пересчитать из raw JSON/SQLite.

Я бы просто обновил `HYBRID_LOAD_TEST_REPORT_2026-08.md` под последний run или добавил туда секцию «post-final-review rerun». **К коду это отношения не имеет.**

---

# Финальный вердикт

| Область                              | Итог  |
| ------------------------------------ | ----- |
| Архитектура core                     | ✅     |
| State reconciliation                 | ✅     |
| Planner / Executor / journal         | ✅     |
| Protection replacement               | ✅     |
| FAILED cancel recovery               | ✅     |
| Dust zero-target                     | ✅     |
| Manual drift recovery                | ✅     |
| ONE_WAY                              | ✅     |
| HEDGE                                | ✅     |
| LONG→SHORT flip                      | ✅     |
| Episode semantics                    | ✅     |
| Offline regressions                  | ✅     |
| Signed core E2E                      | ✅     |
| 30-way hybrid                        | ✅     |
| Критические открытые дефекты Stage 1 | **0** |

**Я бы больше Stage 1 не трогал.**

Не надо устраивать четвёртый рефакторинг и продолжать «полировать ядро до бесконечности». Все найденные нами опасные copytrade-инварианты теперь либо закрыты кодом, либо покрыты regression/live tests.

> **Stage 1 — CLOSED. ✅**
> **Можно переходить к Stage 2 — зеркалированию висящих лимитных ордеров мастера.**

А два пункта выше — backfill `direction=0` и актуализация hybrid report — можно оставить как маленький housekeeping-коммит, не открывая Stage 1 обратно.
