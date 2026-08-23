# Внешнее код-ревью Stage 1 — финальный gate (2026-08-23)

> Текст независимого ревьюера сохранён как есть. Итог: два пункта до
> `Stage 1 = CLOSED` — P1 protective convergence и ONE_WAY E2E через ядро.
> Реализация и проверка отражены в docs/DEV_PLAN_SKELETON.md («Финальный gate
> по внешнему код-ревью»), docs/ENGINE_TEST_STATUS_2026-08.md и
> FINAL_STAGE1_FOLLOWUP_REPORT_2026-08.md.

## Вердикт

**Архитектурно Stage 1 уже готов. Переписывать ядро, менять SQLite, вводить
Redis/PostgreSQL/очереди/микросервисы — не нужно.** Для текущего масштаба это
был бы вредный overengineering. Но перед `Stage 1 = CLOSED` остаются **два
конкретных пункта**, причём только один из них — реальный дефект торговой
конвергенции. Второй — недостающий live-proof.

### Судьба шести блокеров из `review_stage1_new.md`

| Пункт прошлого ревью | Сейчас | Оценка |
| --- | --- | --- |
| Новый Stop FAILED → старый Stop нельзя отменять | Явная `PLACE → CANCEL` dependency; Executor требует `CONFIRMED` | ✅ закрыто |
| Неизменный target + новый actual drift | Новая revision при свежем расхождении | ✅ закрыто |
| HEDGE REST rebuild без `reduceOnly` | Дубликат `_pick_protective` убран; сторона LONG/SHORT учитывается | ✅ закрыто |
| Stop-in-profit со старым mark | Symbol-scoped `positionRisk` refresh перед delta | ✅ закрыто |
| MARGIN_CALL / ADL / autoclose | Immediate authoritative rebuild + reconcile | ✅ закрыто |
| Atomic nonce + effective leverage | Оба исправлены | ✅ закрыто |
| Signed ONE_WAY | Живая биржа принимает payloads, но copy-core не прогонялся | ⚠️ частично |

## 🔴 Единственный настоящий дефект: failed CANCEL после успешного PLACE

Защищён сценарий `PLACE B failed → не отменяем A`. Обратный — нет:

```text
Stop A = 64 500; master меняет → B = 64 000
PLACE B -> CONFIRMED
CANCEL A -> FAILED
```

На бирже остаются оба ордера. Следующий reconciliation делает snapshot,
`_pick()` выбирает только самый новый Stop (B), planner видит
current == target → пустой план → **A = 64 500 висит вечно**. Для long цена
падает → 64 500 сработает раньше → follower выйдет там, где master хотел
держать до 64 000. Это fail-safe (голым follower не останется), но нарушает
целевое состояние.

Требование: инвариант «после reconciliation на одной follower-leg остаётся
один актуальный bot-owned Stop и один bot-owned TP». Тест:
`old A exists; PLACE B CONFIRMED; CANCEL A FAILED; next reconcile → B remains,
A canceled, ровно один bot-owned Stop`. То же для TP. Приоритет: **P1 /
последний code blocker Stage 1**.

## 🟠 ONE_WAY: отчёт завышает уровень доказательства

`test_one_way_signed_cycle` вызывает `AsterClient` напрямую и не проходит цепочку
Master event → Projector → Coordinator → Risk → Planner → Executor → journal →
REAL follower. Он доказывает, что testnet принимает ONE_WAY payloads, но не то,
что ядро корректно копирует сделку REAL follower-у в ONE_WAY. При этом
`.env.example` держит ONE_WAY дефолтом. Решение — один signed E2E через
настоящий App/Coordinator/Planner/Executor с хотя бы одним real follower
(open / stop / move stop / partial close / TP / close / flat audit). Либо явно
зафиксировать HEDGE-only для первой версии и снять пункт с блокеров.

## Что ревьюер НЕ блокирует

- Startup gap (первый snapshot до user-stream): достаточно операционного
  правила «не торговать до READY»; позже — дешёвый resnapshot после первого
  handshake.
- Builder: однозначно Stage 3 (onboarding/credentials, не торговое ядро).
- 30-slot hybrid ≠ 30 REAL wallets: отчёты это честно фиксируют;
  symbol-scoped/dirty snapshot optimization — к 100 копирам, не сейчас.
- Coverage 87% достаточна; искусственный 90% gate возвращать не нужно.

## Итоговые оценки ревьюера

| Область | Оценка |
| --- | --- |
| Архитектура / Risk / Planner / Coordinator / Executor / Projector / SQLite | 9/10 |
| Tests breadth | 9.5/10 |
| HEDGE confidence | 9/10 |
| ONE_WAY code confidence | 8.5/10 |
| ONE_WAY signed E2E core confidence | 7/10 |
| 1–10 copiers | готово |
| 10–30 copiers | готово с разумным REST profile |
| 100 copiers | архитектура подходит, нужна snapshot optimization |

**После двух пунктов — `Stage 1 = CLOSED` без нового большого код-ревью.**
Если первая рабочая версия фиксируется как HEDGE-only, пункт 2 перестаёт быть
блокером автоматически.
