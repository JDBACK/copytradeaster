# Pre-Mainnet Hardening — handoff для следующей сессии

Дата фиксации: 2026-09-04

## Репо

- Репозиторий: `JDBACK/copytradeaster`
- Локальный путь: `/Users/jd_ebash/copytradeaster`
- Отправная точка checkpoint: `918bf46`
- Перед началом следующей сессии проверить фактический HEAD командой
  `git rev-parse --verify HEAD`.

## Ветка

`codex/live-copy-tests-hardening`

Рабочее дерево на момент checkpoint было чистым. Все новые изменения нужно
продолжать в этой ветке; Mainnet/live trading не разрешать.

## План

- Исходный план: [plan_live_test.md](../plan_live_test.md)
- Подробный append-only журнал: [PRE_MAINNET_HARDENING_PROGRESS_2026-09.md](PRE_MAINNET_HARDENING_PROGRESS_2026-09.md)
- Сводный статус тестов: [ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md)
- Архитектурный контракт: [ARCHITECTURE.md](../ARCHITECTURE.md)

Порядок пакетов: A → B → C → D → E → F. Не перескакивать к Mainnet profile,
пока Package B не закрыт тестами и evidence.

## Что сделано

- Прочитаны корневые правила, карта docs, архитектура, исходный план и
  релевантные production/test call sites.
- Package A / PM-B1 закрыт: введены `FOUND / PROVEN_NOT_EXECUTED / UNRESOLVED`,
  durable recovery context, history/position proof, residual-only retry,
  единая runtime/boot policy и Builder-safe fallback.
- Добавлена migration #33 для `execution_commands.recovery_context`, history API
  (`allOrders`) и Shadow/FakeAster support.
- Добавлены unit, integration и signed Testnet сценарии для accepted-but-response-
  lost, unavailable recovery, partial fill и SIGKILL в `SENDING`.
- До начала Package B пройден полный offline/coverage gate; актуальные счётчики
  находятся только в `ENGINE_TEST_STATUS_2026-08.md`.
- Для Package B сначала воспроизведены красные race-тесты: delayed TRADE после
  snapshot, WS receive во время callback, disconnect, queue overflow и stale
  startup entry.
- В Package B частично добавлены generation/dirty boundary в `MasterProjector`,
  concurrent receive в `UserStream`, disconnect/overflow recovery и process-local
  Coordinator entry admission.
- Текущее состояние и все известные незакрытые пункты записаны в
  `PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`; исходный план помечен статусами.

## Что осталось

- Довести Package B до зелёного состояния:
  - исправить READY transition, чтобы повторный boundary не выдавал лишние READY
    notifications;
  - обновить app-lifecycle fakes под `mark_dirty`, generation envelope и
    `clear_degraded`;
  - повторно запустить все шесть focused race-тестов после production-изменений;
  - добавить negative/repeated reconnect cases и проверить startup close-only →
    entry-reconcile порядок;
  - после focused green прогнать полный `pytest -q -m "not testnet"` с coverage,
    `ruff check`, `ruff format --check`, `git diff --check`.
- Package C: immutable `AsterNetworkProfile` и Mainnet preflight.
- Package D: zero-target reason, naked-position flatten и liquidation/leverage safety.
- Package E: production snapshot envelope, health convergence, symbol-aware
  reconciliation и измеренный launch cap.
- Package F: финальная signed Testnet qualification, restart/SIGTERM/SIGKILL,
  failure isolation, load и final audit.

## Особенности работы

- WS — только быстрый сигнал; REST snapshot/reconciliation — authoritative state.
- Любой ambiguous economic outcome остаётся `UNRESOLVED` и не создаёт новый COID.
- Dirty/generation и entry admission должны быть fail-closed: при gap, overflow,
  disconnect, stale snapshot или unresolved state нельзя увеличивать follower exposure.
- `CT_SHADOW_ACCOUNTS_ENABLED=true` не означает `CT_LOAD=1`; shadow load и signed
  Testnet evidence не смешивать.
- Не считать Package B готовым по одному lint или самописному тесту: читать полный
  результат релевантного test file и проверять error/edge/repeat/concurrency пути.
- Счётчики тестов не дублировать в новых docs; единственная точка истины —
  `ENGINE_TEST_STATUS_2026-08.md`.
- После добавления/перемещения Markdown запускать `python scripts/check_doc_links.py`.
- Для продолжения сначала прочитать верхушку `ENGINE_TEST_STATUS`, затем этот
  handoff, подробный progress journal, `plan_live_test.md` и код Package B.
