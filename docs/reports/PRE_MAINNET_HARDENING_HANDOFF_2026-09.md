# Pre-Mainnet Hardening — задача следующей сессии

Дата фиксации: 2026-09-04

## ОБЯЗАТЕЛЬНЫЙ КОНТРАКТ

ВСЯ PRE-PRODUCTION QUALIFICATION ПРОХОДИТ НА РЕАЛЬНОМ ASTER TESTNET С
РЕАЛЬНЫМИ TESTNET-КОШЕЛЬКАМИ. UNIT, FAKEASTER, MOCKTRANSPORT И
SHADOW-ONLY ПРОГОНЫ НЕ ЗАСЧИТЫВАЮТСЯ КАК ACCEPTANCE И НЕ ЗАКРЫВАЮТ
PACKAGE.

Они остаются supplemental regression-тестами. Для response-loss,
429 и network/query failure допустим fault injector вокруг реального
`AsterClient`, но ордера, REST/WS, positions и exchange state должны быть
реальными Testnet. Shadow разрешён только в исходном hybrid-load
gate вместе с 3–5 REAL followers.

## Репо и ветка

- Репозиторий: `JDBACK/copytradeaster`.
- Локальный путь: `/Users/jd_ebash/copytradeaster`.
- Ветка: `codex/live-copy-tests-hardening`.
- Точка до этой коррекции: `350dd43`; перед работой проверить
  фактический `HEAD` и чистоту worktree.
- MAINNET ЗАПРЕЩЁН.

## План и source of truth

- Основной owner plan: [plan_live_test.md](../plan_live_test.md).
- Append-only журнал: [PRE_MAINNET_HARDENING_PROGRESS_2026-09.md](PRE_MAINNET_HARDENING_PROGRESS_2026-09.md).
- Счётчики и gates: [ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md).
- Архитектура: [ARCHITECTURE.md](../ARCHITECTURE.md).

## Сделанный код

- Package A / PM-B1 реализован: `FOUND / PROVEN_NOT_EXECUTED /
  UNRESOLVED`, durable recovery context, `allOrders` + position proof,
  residual-only recovery, единая runtime/boot policy, Builder-safe fallback.
- Package B / PM-B2–PM-B3 реализован: completed-generation cutover,
  Aster server-time cutoff, dirty/overflow/backlog recovery, concurrent WS receive,
  bounded boundary passes, close-only → final snapshot → entry admission.
- Offline unit/integration/replay и full suite green только как regression-
  evidence. Они не закрывают A и B.

## Уже пройденные REAL ASTER TESTNET сценарии

- Package A: MARKET_OPEN accepted, HTTP response discarded, current-order lookup
  hidden, order найден через реальный `allOrders`; один economic order.
- Package A: отдельный worker убит настоящим `SIGKILL` после
  принятого MARKET; boot/WAL recovery не создал второй order.
- Package B: production `App` на backup SQLite прошёл live WS-gated
  READY, master MARKET + STOP, convergence 4 REAL followers, forced listenKey
  reconnect/resnapshot, follower qty/Stop не изменились и не
  дублировались; финальный flat cleanup. Focused result: `1 passed`.

## Главная задача следующей сессии

НАЧАТЬ QUALIFICATION ЗАНОВО С PACKAGE A. НЕ ПЕРЕХОДИТЬ К PACKAGE C,
ПОКА ВСЕ LIVE-СЦЕНАРИИ A И B НЕ ПРОЙДЕНЫ И НЕ ЗАПИСАНЫ.

1. Повторно прочитать root `AGENTS.md`, текущий owner plan, этот
   handoff и верх progress/test-status.
2. Создать свежую `sqlite3.backup`-копию рабочей БД; не запускать
   второй App/bot параллельно; до и после каждого scenario делать
   signed flat/no-open-orders audit.
3. Package A: на REAL Testnet закрыть MARKET_OPEN, MARKET_CLOSE,
   query-unavailable, hidden current/FILLED history, partial outcome и
   SENDING/UNKNOWN SIGKILL. Fault injection разрешен только вокруг
   реального `AsterClient`; не заменять exchange FakeAster.
4. Package B: на REAL Testnet закрыть WS TRADE в queue во время
   REST rebuild, overflow, delayed stale TRADE, missed fill при reconnect,
   failed reconnect resnapshot, final REST equality, slow startup, master change
   между snapshots и zero stale follower entry.
5. После каждого live-сценария обновлять checkbox в
   `plan_live_test.md` и append-only журнал; статус `CLOSED` давать
   только после полной live-матрицы.
6. После A и B повторить тот же принцип для C, D, E и
   только затем выполнить Package F как повторный полный final
   campaign.

## Особенности работы

- Предыдущие offline-green результаты не удалять: это regression-
  защита, но не pre-production verdict.
- WS — trigger, REST — authoritative state; после gap/reconnect сверять
  проектор и follower state с реальным REST.
- Любой ambiguous outcome остаётся `UNRESOLVED` и не может создать
  новый COID.
- При gap, overflow, disconnect, stale snapshot и unresolved state новая
  follower exposure запрещена; closes/reductions/protection должны оставаться
  доступными.
- Mainnet order writes до полного gate запрещены.
