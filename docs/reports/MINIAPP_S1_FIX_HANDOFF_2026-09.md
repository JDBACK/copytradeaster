# S1: исправления и закрытие G1 для реализатора

Дата: 2026-09-07. База findings: `9a0dd98`, ветка `feature/frontend`.
Статус: **READY TO IMPLEMENT; G1 OPEN. Исправления этим документом не выполнены.**

## 1. Задача и граница работ

Проект обслуживает 10–30 копиров, разработчик и трейдер — один человек.
Нужна достоверная история результата аккаунта фолловера, после чего продолжаем
скорость и интерфейс. Существующий trading engine, SQLite, worker и дизайн
сохраняются. Новый сервис, брокер, event-sourcing framework, многопроцессная
архитектура и повторный аудит всего репозитория не нужны.

**Этот документ задаёт актуальную приоритизацию и критерии закрытия данного
corrective G1.** Он уточняет более строгие требования §23–24
[UX-плана](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md) и повторного
[ревью](MINIAPP_S1_REVIEW_2026-09.md). Факты ревью сохраняются; одинаковая
блокирующая важность всех N1–N6 отменена после обсуждения масштаба с владельцем.
Повторное согласование с автором ревью и несколько независимых reviewer-моделей
для закрытия не требуются. Основание закрытия — проверяемое evidence из §6–7.

Минимальное чтение: правила репозитория, этот документ, перечисленные ниже
модули; подробный review открывать только для воспроизведения нужного finding.
Не начинать с перечитывания всех исторических планов. Перед правкой проверить
текущий HEAD: если finding уже устранён, подтвердить тестом и не переписывать.

## 2. Почему G1 не принят

Зелёная suite и проведённый live-сценарий не проверяли обычную работу истории
между несколькими обновлениями. На временной SQLite с настоящими production
классами получены следующие результаты:

| ID | Факт | Последствие | Срок |
|---|---|---|---|
| N1 | Fresh scheduled capture выбирает start=0 при наличии только последних семи дней coverage и получает `fill coverage has a gap` | Автоматическая история не стартует даже у одного аккаунта | Обязательно для G1 |
| N2 | BUY 2@100 → BUY 1@130 → SELL 3@140, с публикацией после каждого шага: три ref вместо одного, прежние входы пропадают из следующего episode | Неправильная история сделки и её результата | Обязательно для G1 |
| N3 | Поздний BUY/SELL за уже опубликованным cutoff сохранён отдельным import job; следующий capture получает пустой delta и не создаёт episode | Восстановленные исполнения отсутствуют в истории | Обязательно для G1 |
| N4 | Из 201 events детали отдают только первые 200 | Длинная история сделки обрезается | Локальная правка до S3 details; не блокирует G1 |
| N5 | Default recovery идёт через старый scan: из 1001 eligible event одно не доставляется и после второго boot | Потеря уведомления в сценарии восстановления большого хвоста | До включения нового event delivery; не блокирует G1 при явно записанном ограничении |
| N6 | Harness отменяет STOP по исходному label, хотя Executor выдал другой clientOrderId | PASS не доказывает отмену нужного ордера | Исправить перед повторным использованием live harness; не установлен дефект рабочего Executor |

R4/R5/R7/R9 сохраняются как принятые в исходном scope. Не переделывать
leg-keyed protection, proof-driven money, dormant links и snapshot feed.
Их корректность не устраняет ошибки накопления истории N1–N3.

## 3. Обязательное исправление: один согласованный пакет N1–N3

### 3.1 Карта ответственности

Пути ниже относительно корня репозитория; имена предлагаемых новых методов
можно адаптировать. Важны контракты и атомарность, а не точное написание имени.

| Файлы | Что менять |
|---|---|
| `src/copytradeaster/storage/history.py` | Coverage start; durable dirty timestamp и revision в транзакции импорта; чтение pending state |
| `src/copytradeaster/storage/database.py` | Только append-only migration, если existing schema не хранит необходимые поля |
| `src/copytradeaster/history/capture.py` | Bootstrap из coverage; scheduled eligibility учитывает dirty даже без роста frontier; передача согласованного replay context |
| `src/copytradeaster/history/replay.py` | Диапазон включает начало продолжающегося episode либо более раннюю dirty область |
| `src/copytradeaster/history/projection.py` | Один способ продолжения — replay; сохранение refs; проверка revision и atomic publication |
| `src/copytradeaster/storage/episodes.py` | Существующий commit seam: episodes/allocations/aliases/events/boundary плюс consumed dirty state |
| `src/copytradeaster/history/models.py` | Только небольшие typed поля/context, необходимые для различения anchor/ref и import revision |
| `src/copytradeaster/history/worker.py` | RAM hint ускоряет работу; storage остаётся источником pending work после restart |

### 3.2 N1: первый диапазон и право на запуск

1. Для `(account_id, environment, symbol)` найти непрерывный **complete**
   coverage interval, содержащий пригодный для capture frontier. Первый start
   равен началу этого interval. Не брать 0 и не использовать minimum другого symbol.
2. Нет достаточного coverage — defer до import/repair. Не создавать видимость
   пустой полной истории. Existing snapshot/reconciliation proof сохраняется;
   наличие coverage само по себе не доказывает исходную нулевую позицию.
3. Origin записывать только в успешной publication transaction. Legacy failed
   checkpoint с start=0 без принятого origin не должен закреплять неверный старт:
   заново вывести старт из coverage. Не переписывать уже доказанный origin вслепую.
4. `prepare_window` обязан учитывать unpublished dirty state. Условие
   `frontier <= captured_end` означает no-op **только если нет pending изменений**.
   Иначе N3 останется скрытым, даже если replay selector уже исправлен.

### 3.3 N2: сохраняем сделку через replay, а не qty-only continuation

Выбран путь **replay затронутого episode с доказанной стартовой границы**.
Не вводить параллельно новый финансовый accumulator: это второй алгоритм,
который придётся синхронизировать с уже существующим projector.

1. Latest qty/ref boundary недостаточно для продолжения незавершённой сделки.
   Если на latest boundary есть открытый episode, диапазон должен включать
   его исходные entry fills, partial exits и новые fills. Для HEDGE общий диапазон
   должен корректно восстанавливать обе legs. Использовать доступную доказанную
   границу до этого начала; простой корректный fallback — accepted origin.
   Не ограничивать replay семью днями: семь дней ограничивают REST-запрос.
2. Seed должен относиться к выбранной границе, а не к latest cutoff. Для partial
   open-start сохранять исходный partial seed и quality. При поздних фактах
   использовать существующий пересчёт origin seed из authoritative ending.
   Не объявлять недоказанные старые цены/входы известными.
3. Различать новый deterministic anchor (`fill:...`/placeholder) и уже выданный
   owned `public_ref`. `reserve_ref` вызывается для anchor; resolved ref повторно
   не резервируется. Проверять scope ownership, не угадывать тип по форме UUID.
4. Повторная проекция обновляет тот же episode и все его allocations. Суммы и
   средние выводятся из полного набора fills. Старые open rows не должны оставаться
   рядом с новым closed row. Existing aliases/silent correction сохраняются для
   ранее опубликованных refs; неоднозначную lineage нельзя связывать наугад.
5. В той же транзакции сохраняются episodes, allocations, aliases, events и
   boundary. Replay ранее известных transitions не рассылает их снова. Поздняя
   коррекция сохраняет существующую silent policy вместо новых «живых» открытий.
6. Если retention уже убрал необходимые facts, не публиковать complete с
   обнулённым basis: сохранить честное partial/deferred состояние и причину.
   Это не повод добавлять архивную подсистему в этот пакет.

Приоритет — правильный replay одного затронутого symbol. Оптимизация до
минимального интервала допустима в существующей модели; сложный индекс
checkpoints и новый планировщик для закрытия N2 не требуются.

### 3.4 N3: pending changes переживают import job и restart

Использовать существующую `history_scope_state` и `facts_revision`. Минимальное
добавление: nullable `dirty_from_ms` по той же scope/symbol; consumed revision
можно хранить в существующей boundary, если её контракт явно это гарантирует.
Не создавать отдельную таблицу для каждого вида очереди.

Контракт импорта:

```text
BEGIN под Database.write_lock
  сохранить новые facts и coverage
  если изменились projection inputs:
    facts_revision += 1
    dirty_from_ms = min(previous_dirty, earliest_changed_timestamp)
COMMIT
```

Coverage delta вычислять как изменение интервалов, включая заполнение внутренней
дыры и расширение interval, а не сравнением start уже слитых intervals.
Повтор того же полного импорта без изменения данных не создаёт вечный dirty loop.

Контракт публикации:

```text
прочитать pending revision R и dirty D
выбрать replay по origin/open episode/D; прочитать facts согласованно с R
проверить ending quantities и coverage
BEGIN под Database.write_lock
  проверить expected boundary version и current facts_revision == R
  при несовпадении: rollback/defer/retry, не публиковать устаревший результат
  записать episodes/allocations/aliases/events/boundary с consumed revision R
  очистить dirty, покрытый принятой публикацией
COMMIT
```

При новых facts после cutoff нельзя очистить ещё не опубликованный остаток.
Самый простой безопасный вариант с одним `dirty_from_ms` — сохранить dirty
консервативно до покрытия всех изменений соответствующей revision; лишний
повторный replay допустим. Проверка revision и очистка должны быть в **той же**
publication transaction. Не держать SQLite write lock во время REST/ожидания margin.

Migration должна учитывать существующие facts без pending marker: пометить
существующие scope для одного консервативного replay доступного coverage или
эквивалентно вывести pending по revision. Старое повреждённое состояние не
объявлять complete только потому, что marker появился после upgrade.
Rebuild на upgrade проходит через обычные silent publication/alias правила;
не удалять history/outbox/БД ради «чистого старта». Индекс миграции определить
по актуальному HEAD, не менять ранее выпущенные migrations.

## 4. Небольшие сопутствующие исправления

Можно выполнить сразу отдельным небольшим коммитом, но N4/N5 не расширяют
обязательный corrective gate из §6.

| ID | Конкретная правка | Достаточный тест |
|---|---|---|
| N4 | `application/service.py:_family_events`: реальный `after_seq` передать в repository; SQL IN по owned alias family с общим seq и `limit+1` либо корректный merge. Убрать чтение первых 200 с последующей Python-фильтрацией | 201 events через public details API, страницы по 50: все 201 доступны ровно один раз; alias family включена; fills cursor независим |
| N5 | `application/engine.py:recover_notification_outbox`: False — только legacy command recovery без user-event sweep/progress; True — только существующий durable consumer. Не оставлять старый capped user-event scan как fallback | Default False не enqueue user events; enabled обрабатывает >1000 событий через бюджеты/повторный boot без потери; имеющиеся outbox/cursor rollback tests сохранить |
| N6 | В `tests/testnet/test_history_acceptance_live.py` брать cancel target из placement journal row. Проверять отмену именно этого coid; cleanup созданных protective orders и финальные flat + open_orders=[] | Offline fake exchange отвергает ошибочный target и оставляет STOP активным; после верной отмены соответствующий ордер отсутствует. Один CONFIRMED после -2011 не считается доказательством |

N5 deferred означает известное ограничение default recovery, а не утверждение,
что False уже корректно отключает этот старый путь. Реализатор фиксирует это
явно и не включает новый event delivery до исправления. Trade links остаются
выключенными до готовности S3 route, как уже реализовано.

N6 исправить перед следующим signed запуском. Его найденный дефект не требует
изменять Executor cancellation semantics: idempotent already-absent там допустим.

## 5. Компактная матрица обязательных тестов

Использовать существующие fixtures в `tests/unit/history/` и acceptance seam в
`tests/unit/application/test_history_acceptance.py`. Новые тесты проверяют
наблюдаемый результат через реальные repositories/capture/worker. Подменять можно
exchange/clock; подменять готовый projector result в этих регрессиях нельзя.
Не создавать отдельный framework и не перебирать всю матрицу live.

| Тест | Сценарий | Обязательные assertions |
|---|---|---|
| T1 — bootstrap | Fresh DB → обычный import job → `run_scheduled`; затем вариант failed checkpoint start=0 | Origin из complete coverage правильного symbol; publication успешна; отсутствие gap от epoch; unrelated symbol не влияет |
| T2 — continuation | BUY 2@100; BUY 1@130; SELL 1@140; SELL 2@140, отдельный scheduled capture после каждого шага | Один canonical ref на всех шагах; entry_qty=3, average_entry=110; после partial open_qty=2; финальный exit_qty=3, gross=70 при заданных exchange realized fields; все 4 allocations; одна closed row без stale open rows |
| T2b — parity/restart | Те же facts batch и incremental; reopen DB между increase и partial; ещё один no-op capture | Денежные/позиционные totals и логические transitions равны; внутри incremental ref стабилен после restart; повтор не дублирует events/allocations. UUID независимых БД буквально сравнивать не нужно |
| T3 — lost dirty | Принять flat до t=100; отдельным import сохранить BUY в t=40 и SELL в t=60; reopen DB; следующий import delta пустой; frontier не вырос | Scheduled capture всё равно запускается; появляется ровно один closed episode; повтор no-op; dirty очищен только после publication |
| T4 — atomicity | Импорт новых facts между чтением projection inputs и commit; отдельно сбой внутри publication transaction | Старый расчёт не принимается при новой revision; dirty не потерян; rollback не оставляет половину episodes/events/boundary; повтор успешно публикует |
| T5 — coverage/upgrade | Internal coverage gap стал complete; открыть БД предыдущей схемы с существующими facts и применить migration | Dirty range включает gap; upgrade не теряет facts/ref и назначает необходимый replay; недоступная старая история не превращается в выдуманную complete |
| T6 — public read | После T2 запросить existing service/API surfaces для владельца и чужого scope | Один trade_ref ведёт в те же totals/details/events; foreign пуст/denied по существующему контракту; денег другого аккаунта нет |

Существующие HEDGE/flip/partial-history, money proof, protection, dedup и
recovery тесты остаются в suite. Если выбранная правка меняет их путь, дополнить
один соответствующий regression, а не повторять все возможные сочетания.
Комиссии в fixtures задавать явно, сравнивать Decimal; неизвестный net остаётся None.

## 6. Что достаточно для закрытия G1

**Все пункты ниже должны быть фактически выполнены:**

- [ ] N1–N3 исправлены в production composition, а не только в ручном capture helper.
- [ ] T1–T6 зелёные; regression сначала воспроизводит ошибку на исходном коде
  либо сохранено точное before/after evidence. Существенные assertions из таблицы есть.
- [ ] Пройден один полный offline gate после финальных изменений:

```bash
PYTHONPATH=src:. .venv/bin/pytest -q -m 'not testnet'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
python3 scripts/check_doc_links.py
git diff --check
```

- [ ] Есть запись §7 с commit, тестами, before/after и статусами N4–N6.
  Новые migration/test files отражены в соответствующих картах. Счётчики suite
  обновлены только в `docs/ENGINE_TEST_STATUS_2026-08.md`.
- [ ] Deferred items имеют срок включения: N4 до details, N5 до event-delivery
  rollout, N6 до повторного live harness. Ни один из них не назван исправленным
  без evidence. Новых необъяснённых ошибок достоверности истории нет.

После выполнения чеклиста **реализатор может сам записать G1 CLOSED и продолжить
S2/S3**. Отдельный новый вердикт этой дорогой модели не нужен. Если тест выявил
конкретный нерешённый дефект N1–N3, G1 остаётся OPEN с этим дефектом, а не с
абстрактным «требуется ещё review».

### Что не является дополнительным обязательным gate

- Повторный полный frontend/design audit: этот пакет не меняет UI.
- 100-account load/soak, новые telemetry services, второй performance framework.
- Новый signed прогон исключительно ради повторного approval этого corrective.
  Existing live PASS хранится с его ограничениями. SL fill и проверку отмены
  правильного STOP завершить в общей end-to-end приёмке **до production rollout
  новой истории**, совместив с S3/G3; не называть их уже доказанными.
- Все пункты старого расширенного §23/§24 автоматически: для этого исправления
  сроки и обязательность определяет настоящий документ.

Для масштаба 10–30 полезен один bounded smoke существующего worker с fake exchange:
показать, что конечная очередь обслуживается и backlog убывает. Не считать direct
capture 552 ms скоростью всей очереди. 100-account benchmark и новый latency SLA
не блокируют G1; возвращаться к оптимизации при наблюдаемой проблеме.

## 7. Как зафиксировать факт закрытия

Ниже шаблон, **не готовое свидетельство PASS**. Добавить заполненную запись в
[журнал кампании](MINIAPP_FUNCTIONAL_PROGRESS_2026-09.md), обновить текущий TL;DR
в ENGINE_TEST_STATUS и checklist G1 в DEV_PLAN_SKELETON. При необходимости
дополнить ARCHITECTURE только фактически изменившимся контрактом history.

```text
Дата / ветка / tested commit:
Scope: corrective N1–N3 по MINIAPP_S1_FIX_HANDOFF_2026-09.md.
Изменённая архитектура: replay start/seed/ref; dirty revision и atomic commit.
Migration: номер либо «не понадобилась» + причина.

N1: CLOSED — test nodeid, first scheduled publication/origin before → after.
N2: CLOSED — test nodeid, один ref, entry=3/avg=110/exit=3/gross=70,
    все allocations, no stale opens; restart и no-op подтверждены.
N3: CLOSED — test nodeid, отдельный import→restart→same-frontier capture,
    late episode опубликован; conflict/rollback не теряет pending changes.
T1–T6: nodeids/команда и путь к краткому логу.
Полный offline gate/lint/format/doc-links: PASS; counters в ENGINE_TEST_STATUS.

N4: CLOSED + test / DEFERRED до S3 details.
N5: CLOSED + test / DEFERRED до event delivery, известное ограничение записано.
N6: CLOSED (offline target/cleanup) + test / DEFERRED до live harness.
Live: не запускался / конкретный сценарий и evidence; недоказанное перечислено.
Отложено до production rollout: фактический SL fill + STOP cleanup evidence.

Вердикт: G1 CLOSED по §6 / OPEN — конкретный failing assertion.
Следующее действие: продолжить S2 по общему плану (или согласованный slice).
```

В финальном ответе владельцу достаточно ссылки на эту запись, commit и статуса.
Не запускать новый цикл общего ревью после успешного чеклиста без новой
ошибки, изменения требований или явного запроса владельца.
