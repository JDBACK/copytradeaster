# Статус работ — copytradeaster (этапы 1–3)

Журнал ведётся с 2026-08-22. Формат: дата → факт → статус. Итоговая S3/G3
кампания зафиксирована в `feature/frontend`; ветка Stage 3 части 2
`codex/stage-3-mini-app-part2` смержена ранее.

> **Примечание (2026-09-10):** вердикт ниже покрывает дерево `f7df60b`; коммиты `58c10f7` / `ffa2848` (поведенческие, `scripts/dev-up.sh`) приземлились позже — повторная квалификация pending; покрытие этих коммитов не утверждается.
> **Текущая ветка (2026-09-10):** на `codex/miniapp-live-updates` полный offline gate прогонялся уже
> на актуальном дереве (включая `58c10f7` / `ffa2848`): backend **1549 passed / 2 skipped / 87 deselected**,
> frontend **34 файла / 400 passed**, ruff/format, doc links/budgets/facts и `git diff --check` чисто;
> браузерная приёмка Mini App — top-блок ниже и live-план §8. Это не переоценка вердикта по слиянию в
> `main`: он остаётся привязанным к своему дереву.
> **Corrective (2026-09-10, позже в тот же день):** закрыты два хвоста live-плана — история
> `copyc1/BTCUSDT` (один биржевой поток держал обе эпохи position mode, из-за чего окно не
> публиковалось; replay публикует книгу своего authoritative boundary) и liveness public
> mark-стрима (SUBSCRIBE-ack больше не считается доказательством котировок). Детали,
> причина и проверка — live-план §8, «corrective pass».
> **Merge-review corrective (2026-09-10, вечер):** по результатам полного merge-review ветки
> `codex/miniapp-live-updates` против `main` закрыты 7 × P1 и 8 × P2: потеря книги истории при
> смене position mode, глобальный (вместо посимвольного) health/fallback mark-price и вердикт
> `check_mark_stream.py`, возврат private stream в `live` после смерти сокета, сброс backoff
> `UserStream` на рукопожатии, невозрастающая лестница self-heal во фронтенде, скрытые
> keep-alive вкладки как live-потребители, отсутствие L4-coalescing котировок и cadence, а также
> восемь пунктов P2 (кнопка generic retry, дублирование rail, истечение Agent evidence как
> «потеря связи», a11y тикающего rail, отчёт pull-to-refresh раньше истины, whitespace-дрейф,
> docs-факты). Актуальный полный offline gate: backend **1570 passed / 2 skipped / 87
> deselected**, frontend **35 файлов / 412 passed**; `ruff check`/`format --check` (312 файлов),
> doc links/budgets/facts и `git diff --check` чисто. Детали каждого пункта — live-план
> «merge-review corrective».

> **TL;DR (2026-09-09, integrated requalification):** локальная ветка
> `codex/integrate-hardening-frontend` собрана из hardening tip `4c0d1a1` и
> `feature/frontend` `d6d5791`; merge diff просмотрен, конфликтные границы
> сохранены. Исправлены только live-harness assumptions: effective leverage,
> post-fill baseline/cleanup, one-way scratch DB + quantity sizing, explicit
> manual admission seam и Activity-переход на канонические
> `user_activity_events`.
> Offline backend: **1525 passed / 2 skipped / 87 deselected**, coverage
> **87%**; Ruff/format/compileall/strict doc-links чистые. Shadow scale:
> **2 passed за 69.47s**. Frontend: **28 files / 349 passed**, oxlint,
> `tsc -b` и Vite build PASS.
> Signed Testnet requalification: leverage **4/4**, one-way **2/2**, Mini App
> Activity **1/1**, Notifications/Positions/Risk **3/3**, HEDGE **7/7**;
> полный hybrid load **1 passed за 4085.39s (1:08:05)** — 30 циклов,
> restart/SIGTERM/SIGKILL, 3 REAL + 10 SHADOW, journal **1988 CONFIRMED / 0
> FAILED / 0 UNKNOWN / 0 SENDING**. Независимый flat audit после всех прогонов:
> `main`, `copyc1`, `copyc2`, `copyc3`, `tg-8264892202` — zero positions / zero
> open orders.
> **Вердикт: GO для fast-forward/push в Git `main`.** Это не разрешение
> mainnet-торговли: `CT_ALLOW_MAINNET=false` и сетевой fail-closed guard
> сохраняются. Старые TL;DR ниже сохранены как исторические snapshots.

> **TL;DR (2026-09-07, QUALIFIED + чистый load):** остаточный post-fill race
> re-review `ac161b2` закрыт: master/TTL теперь выбираются после `fr.lock` +
> executor mutation lock; reviewer-repro и штатные race/TTL регрессии green.
> Повторный затронутый live: **L1 post-race GREEN 88.18s**. Ранее также
> подтверждены **F3 clean GREEN 131.8s**,
> LC1 clean GREEN 137s, A5 green 8.13s. **Load 0.3 clean GREEN: 1 passed
> за 3989.74s, exit 0, журнал 1981 CONFIRMED / 0 FAILED**, все фолт-фазы,
> P8a/P8b внутри. Офлайн **1159 passed, 2 skipped**, ruff чист.
> Фронт-стенд убит по приказу owner'а; **-4226 доказан интерференцией**
> (в тихом окне 0 за ~2000 команд). Финальный flat audit чист.
> Открыто: coverage 86→87, 3 honest shape-skip.
> **Вердикт: full QUALIFIED (owner scope 0.3). Mainnet — решению owner'а.**
>
> **2026-09-06, corrective qualification (load переквалифицирован чисто 2026-09-07, см. выше):**
> live-матрица исправленного дерева закрыта: UNKNOWN 6/6 (A4 переведён на
> ETHUSDT — все BTCUSDT allOrders-окна забиты 1000/1000), network 3/3, LC1–LC6
> 6/6, совместимость 22 nodeids green + 2 honest skip (dust2 бюджет фолловера,
> limit-reduction full-fill; F3+L1 из 2 failures закрыты зелёными rerun),
> load 0.3: 30/30 циклов + restart/SIGTERM/SIGKILL + метрики,
> финальный FLAT-ALL. Офлайн: **1147 passed, 2 skipped**, coverage **86%**
> (документарная цель 87% открыта). Ruff check/format чистые.
> Найдено и починено вживую 3 прод-бага: 1x unset-marker как ready
> (`leverage.observe/prepare/seeding`, F3), протухший post-fill кэш мастера
> (осцилляция open/close, L1), неразрешимый UNKNOWN у SET на row-omitting
> клиентах (brick shadow-входов, load cycle 2).
> Решения owner'а (зафиксированы, не умолчаны): load scope 100→30
> (CT_LOAD_SCALE=0.3; «100» было эмоциональной цифрой), P8a-B (залеченные
> детерминированные реджекты вместо стерильности журнала + moot-by-flat),
> приёмка load7 по evidence + coded moot-анализу без зелёного exit-кода.
> Открыто: полный load 1.0 (отложен), 3 honest shape-skip (dust2 бюджет,
> L2 residual, limit-reduction full-fill), coverage 86→87, кодбук -4226
> (недокументированный транзиентный реджект тестнета, нет в официальном
> списке кодов Aster).
> **Вердикт: corrective qualification COMPLETE (owner scope). Mainnet этим
> сообщением НЕ разрешается — релиз только явным решением owner'а.**
> Ниже — история предыдущих кампаний; их счётчики и GO относятся к прежним деревьям.


## Текущее состояние

- **Pre-Mainnet Package E — LIVE-COMPLETE (2026-09-05,
  ветка `codex/live-copy-tests-hardening`):** E1 prod DONE + live-проверен
  нагрузкой (metrics p50/p95/p99 percentiles собираются в load gate,
  symbol-aware scope, max_followers=10 cap подтверждён envelope-замером,
  health convergence gate, ONE_WAY-only v1 mainnet boot refusal, удаление
  priceProtect из planner contract — wire его никогда не нёс, 2 offline pins
  удалены); E2 live: LIMIT **2 passed** (L1 entry, L3 reduction/convergence) +
  L2 honest skip (биржа дважды исполнила full — нужен real partial для
  residual row), isolation **3/3 LIVE-COMPLETE за 633s** (F1 429-transport,
  F2 −2015 + degraded, F3 control-plane-denial). Каждый live test
  заканчивается flat/zero-open-orders.
- **Pre-Mainnet Package D / PM-B5 + PM-B6 + PM-B7 — LIVE-COMPLETE (2026-09-05,
  REAL Aster Testnet, один 386s run):** prod ZeroReason taxonomy (sticky ТОЛЬКО
  `PROPORTIONAL_REDUCTION_DUST`), PM-B6 naked-flatten, PM-B7 operator cap +
  liquidation invariant + fresh-entry leverage gate
  (`LIQUIDATION_MMR_CUSHION=0.01`); live **6/6 green** (Dust-1 с master 0.008
  shaping, Dust-2, Dust-3 SHORT-leg fix, Protection-cancel/naked,
  liquidation-hold). Key finds: S1 open→flatten livelock на ~200x найден+fixed
  entry gate; liquidation-violation flatten ACTION доказан unshapeable в
  cross-only multi-asset реальности (theorem sketch: risk >~100.5% vs RiskPolicy
  cap 100%; isolated отвергнут с -4168 на copyc1 И tg-8264892202, probed live)
  → тест утверждает честный hold contract + live evaluation evidence; TEMP probe
  добавлен+удалён, ноль TEMP sites. Offline hygiene: 3 routing assertions
  200→44, priceProtect удалён + 2 pins, 75 passed на двух затронутых файлах.
- **Pre-Mainnet Package C / PM-B4 — LIVE-COMPLETE (2026-09-05, REAL Aster
  Testnet):** prod `AsterNetworkProfile` (testnet 714 / mainnet 1666,
  mixed-combo fail-closed) + unified mainnet preflight + onboarding
  `from_profile`; live **3/3 green за 12s** (composition, mode-failure block,
  in-flight block), без order writes; harness fix: явный
  `testnet_ip_whitelist_override=False` kwarg для MAINNET-profile Settings
  (live `.env` override сохранён, prod guard untouched).
- **Pre-Mainnet Package G — LIVE-COMPLETE (2026-09-05, REAL Aster Testnet):** HEDGE
  dual-leg **7/7 green** в `tests/testnet/test_pre_mainnet_hedge_live.py` (G0 pre-check +
  G1 LONG+SHORT + G2 asymmetry + G3 stop replace + G4 leverage + G5 partial + G6 close);
  по пути пофикшен прод-баг #27 (мусорный liquidationPrice=25.5M → wrong-side guard в
  `Coordinator._liquidation_violated`); ONE_WAY-only boot refusal СНЯТ из
  `src/copytradeaster/app.py` после полного прогона и финального FLAT-ALL аудита;
  вердикт v1: GO (ONE_WAY + HEDGE).
- **Pre-Mainnet Package F — LIVE-CLOSED (2026-09-05, REAL Aster Testnet):**
  hybrid load 3 REAL + 10 SHADOW **1 passed за 3:39:15** (runner `loadgate8`,
  `CT_LOAD_SCALE=1.0`, 100 циклов, restart/SIGTERM/SIGKILL, journal чист, метрики
  p50/p95/p99, flat везде). Phantom-fill 65000 устранён синком REST-marks
  (`App._sync_shadow_marks`). Package F CLOSED. Launch cap v1 = 10 (замер, не мнение).
- **Pre-Mainnet Package B / PM-B2 + PM-B3 — LIVE-COMPLETE (2026-09-05,
  ветка `codex/live-copy-tests-hardening`, финальное дерево):**
  authoritative master boundary публикует generation только после completed
  REST snapshot + episode sync, delayed current-generation frames сверяются с
  Aster server-time cutoff (`E <= cutoff` fail-closed); queue generation,
  overflow, backlog `> 5s`, dirty state и bounded coalescing закрывают local
  entry gate; startup/reconnect — master snapshot → close-only follower sweep →
  финальный snapshot → entry reconcile. Signed Testnet matrix на финальном
  дереве: S1 rebuild-holds-queue 100s, S2 stale-cutoff 44s, W1 reconnect-gap
  83s, W2 failed-resnapshot 68s, P1 startup-transient 45s, REAL-06
  App/WS/reconnect 134s (run DB `data/pm-b-live-20260905T002000Z.sqlite3`) —
  все green. Harness fixes: settle gate (partial-fill race), `_wait_stable`
  quiescence gates, HEDGE nonzero-key counting, deterministic-COID revisions.
  Каждый live test заканчивается flat/zero-open-orders, второй App не
  запускался. Детали: `reports/PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`.

- **Pre-Mainnet Package A / PM-B1 — LIVE-CLOSED
  (2026-09-04, HEAD `388f0e7`, REAL Aster Testnet, run DB
  `data/pm-a-live-20260904T234500Z.sqlite3`):** unified campaign закрыл всю
  UNKNOWN live-matrix. Новый `tests/testnet/test_pre_mainnet_unknown_live.py` —
  **5 passed**: A1 CLOSE response-lost через real `allOrders` (7.7s-class, ровно
  1 send, `history:FILLED`); A2 all-reads-down UNKNOWN hold под исходным COID,
  zero retry; A3 partial через real `positionRisk` delta, только residual
  (`position-progress`); A4 proven-not-executed ровно один retry с новым COID на
  copyc3 (`send_attempts == 2`, `real_sends == 1`); A5 настоящий SIGKILL в UNKNOWN
  window → boot CONFIRM без resend (`confirmed == 1, unresolved == 0`, один
  `order_id`). Pre-existing proofs re-ran: response-lost PASS, SIGKILL-SENDING PASS,
  REAL-05 boot PASS на copyc3 за 10.53s. Исправленный
  `tests/testnet/test_builder_stale_corrective_live.py` (Package B admission gate
  `32fe56d`, entry leg заменён прямыми REAL seeded legs, reduction assertions
  intact) — **1 passed за 53.67s**. Saturation note: main/copyc1 BTCUSDT
  `allOrders` 1000/1000 — negative-proof legs идут на copyc3 (67 rows), resolver
  fail-closed UNRESOLVED by design. Фикс REAL-05 тронул только
  `tests/testnet/test_signed_scenarios.py` (aid params + ретаргет на copyc3), prod
  code не менялся. Deterministic-COID lesson: `command_key_of` игнорирует planner
  coid — тесты с одинаковой формой payload используют distinct revision (1–5).
  Каждый новый live test заканчивается `finally` flatten + signed flat/zero-orders
  audit — flat/zero везде. Полный контракт —
  `reports/PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`.

> **TL;DR (2026-09-09):** S1/G1 — **CLOSED**; S2/G2 — **ACCEPTED**;
> S3/G3 — **CLOSED в согласованном scope**: Chromium, Telegram Desktop WebView
> и iPhone Mirroring. Android по решению владельца исключён из scope и verdict.
> Offline/backend/frontend, §19.7 performance, error/history matrix и signed
> testnet N6 зелёные. Полная карта — [MINIAPP_UI_ACCEPTANCE_INDEX](reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).
> Предыдущий `REQUEST CHANGES` на `9a0dd98` и промежуточные OPEN/WIP записи ниже
> сохранены как история кампании. N4, N5 и N6 закрыты итоговой S3-проверкой.
> Offline **1439 passed / 2 skipped / 45 deselected** (точно на `84cabfb`, 2026-09-07), миграции **0..58**, ruff check/format clean, doc links clean (49 файлов).
> S2 frontend current WIP: **27 test files / 316 passed**, oxlint 0/0, TypeScript + Vite production build PASS; main JS **336.56 kB / 96.46 kB gzip**; recorded G2 browser evidence: cold 4 POST, warm root p95 1.0 ms/160, nested p95 1.9 ms/120, 300 ms RTT p95 6.0 ms/160, все warm 0 reads; browser DOM/listeners bounded, long tasks/errors 0. Финальные corrective после этого browser trace затронули transport validation/cancellation/error edges, поэтому latency/DOM числа ниже остаются reference evidence того G2 прогона, а не заявлением о новом CDP rerun.
> **S3 WIP snapshot (2026-09-07):** backend history/event slice — **59 targeted tests passed**; `tsc -b && vite build` — PASS; oxlint — PASS с 2 предупреждениями; полный frontend Vitest после перехода на новые Home/Positions/Activity/history contracts — **272 passed / 44 failed (27 files)**. Это промежуточный срез, G3 остаётся OPEN; детали и TODO — [MINIAPP_S3_PROGRESS](reports/MINIAPP_S3_PROGRESS_2026-09.md).
> **S3 verification pass (2026-09-09):** obsolete fixtures переведены на контракты S3 (feed/menu/user-event delivery), offline gate — **1449 passed / 2 skipped / 45 deselected**, frontend — **28 files / 349 passed**, oxlint clean, production build PASS, ruff check/format и doc links clean. N5 закрыт тестами (production factory True/False, два boot >1000 событий, crash до/после atomic commit, toggle-границы, остановка recurring task до DB close, retry sweep, command/user duplicates). N6 закрыт: harness исправлен (cancel по фактическому journal coid, query/order-list отсутствие, cleanup flat + `open_orders=[]`, scheduled captures, durable-boundary проверка capture-окна) и **signed live на testnet PASSED** — реальный `STOP_MARKET` fill доказан как отдельное exchange-исполнение (тик-прижатый ратчет; immediate-trigger отвергается −2021), teardown-утечка aiosqlite закрыта. Свежий S3 §19.7 trace — **PASS** (cold `4` read POST, `160` warm root samples p95 `5,3 ms`, delayed `300 ms` p95 `4,4 ms`, `200` nested samples, long tasks/errors `0`); blocked/throttled matrix `11/11` и alias/split/partial history — **PASS**. Mini App UI дополнительно проверен в Chromium, Telegram Desktop WebView и iPhone Mirroring. Android по решению владельца исключён из согласованного scope; **G3 CLOSED**. Факты собраны в [UI acceptance index](reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md), S3 §§19–26.
> **Mini App live UX de-noising (2026-09-10):** frontend — **34 файла / 400
> passed**, oxlint clean, production build PASS; backend — **1549 passed / 2
> skipped / 87 deselected**; ruff и `git diff --check` clean. Убраны все ручные
> обновления (routine re-read больше не рисуется как staleness, ошибка лечится
> авто-retry, rail появляется только когда данные реально перестали обновляться),
> добавлен pull-to-refresh как единственный ручной путь, разрыв транспорта читается
> одним голосом (rail вместо дублирующих плашек «Проверьте интернет», проверено
> остановкой бота при открытой странице: `Нет связи · 41 сек назад`, плашек 0,
> самовосстановление за 21 с), экран говорит один раз (плашка о снимке уступает rail,
> «Загружаем…» и затемнение кнопки — только по нажатию пользователя: замер Закрытых
> сделок 15 → 5 мутаций за 95 с и 0 миганий кнопки), а строки пропусков в
> Активности больше не печатают коды движка (третий проход: `format_user_event`
> отдаёт русскую причину, неизвестный код не печатается). Живой Chrome-замер
> (180 s, `scripts/audit_miniapp_browser.mjs`): меняются только числа PnL и
> минута в подписи свежести. Контракт и остаток —
> `reports/MINIAPP_LIVE_MODE_PLAN_2026-09.md` §8.
> Ниже — append-only журнал кампаний; старые секции — снимки момента. Обновлять этот блок при каждом gate.

## Текущее состояние

- **S3 verification pass (2026-09-09):** миграция старых тестов на S3-контракты
  выполнена, один общий offline gate зелёный: backend **1449 passed / 2 skipped
  / 45 deselected**, frontend **28 files / 349 passed**, ruff check/format и
  doc links (55 файлов) clean. Мигрированы `test_stage2_adapters`,
  `test_adapters`, `test_service` (activity feed), `test_renderers`,
  `test_notifications`, `test_history_acceptance` (+live), `test_builder_resume_live`;
  presenter-копия и private-chat gate приведены к коду. N5 подтверждён новыми
  тестами в `test_user_event_delivery_c6` (crash до/после atomic commit,
  disable→event→enable, prepare→toggle→commit, no-op enable, disable→FAILED
  outbox, монотонный курсор) и `test_engine` (factory True/False, два boot
  >1000 событий без дублей, cancel delivery task до DB close, retry sweep,
  legacy command suppression). N6 закрыт: harness исправлен (cancel по
  `execution_commands.client_order_id`, query/order-list отсутствие, cleanup
  flat + `open_orders=[]`, scheduled captures между шагами, durable-boundary
  проверка окна публикации, тик-прижатый ратчет для реального SL fill) и
  signed live на testnet **PASSED** (`1 passed`, чистый выход без виса;
  утечка aiosqlite-потока в teardown закрыта реестром фикстуры).
  Chromium, Telegram Desktop WebView и iPhone Mirroring фактически прошли
  отдельные UI-сценарии; свежий S3 §19.7 trace, error matrix и history cases
  закрыты в §§24–26. Подробная матрица и точные границы перечислены в [UI
  acceptance index](reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md). Android
  исключён владельцем из согласованного scope; G3 **CLOSED**.
  Детали реализации и журнала — [S3 progress](reports/MINIAPP_S3_PROGRESS_2026-09.md).

- **S3 implementation handoff (2026-09-08):** menu/feed/push migration,
  production durable delivery и owned split/link/detail changes реализованы.
  По просьбе владельца тестовые suites и browser/live не запускались.
  TypeScript compilation и ruff по изменённым Python-файлам проходят.
  Прежние counters ниже — baseline, не доказательство текущего diff.
  Старые command Activity/recovery tests требуют миграции на новые контракты;
  список и порядок — [S3 testing handoff](reports/MINIAPP_S3_TESTING_HANDOFF_2026-09.md).
  N6 harness не изменялся; G3 OPEN.

- **S3 cache bounds (2026-09-08):** history regressions — **9 passed**;
  TypeScript и oxlint PASS. Проверены три inactive filters, mounted Back target
  и whole-slice eviction после release. Полный suite после этого не повторён.

- **S3 history/shell gate (2026-09-08):** полный Vitest — **325 passed,
  28 files**; oxlint clean. Trades regression доказывает dedup, сохранение
  cursor при TTL probe и explicit restart; shell render keys переведены на
  React state, scroll/Back bookkeeping сохранён. G3 OPEN.

- **S3 TTL pages (2026-09-08):** history resource regressions — **6 passed**,
  TypeScript PASS. TTL probe сохраняет накопленные detail events и cursor;
  изменённая первая страница требует explicit refresh. Общий rerun впереди.

- **S3 revision gate (2026-09-08):** history resource regressions — **5 passed**,
  TypeScript PASS. Проверен запрет автоматической замены отвергнутой ревизии
  до explicit refresh. Полный suite после этой правки ещё не повторён.

- **S3 history retry (2026-09-08):** `historyResources.test.ts` — **4 passed**,
  TypeScript PASS. Добавлен regression сохранения events pages и независимого
  fills cursor при retry. Полный suite после этой правки пока не повторён.

- **S3 shell corrective (2026-09-08):** полный frontend Vitest — **321 passed,
  28 files**; shell — **43 passed**. TypeScript PASS. Это offline evidence,
  не закрытие S3/G3: история/details/delivery и browser/live acceptance впереди.

- **S3 shell migration (2026-09-08):** targeted
  `appShellKeepAlive.test.tsx` — **29 passed / 14 failed** после миграции
  mocks на activityFeed и новых названий навигации. TypeScript PASS.
  Полный suite после этой правки ещё не запускался. Оставшиеся сценарии:
  Home overview вместо preview, stale/retry, pagination и UI markers.

- **S3 Home/Settings/display follow-up (2026-09-08):** полный frontend Vitest
  **292 passed / 29 failed, 28 files**; единственный красный файл —
  `appShellKeepAlive.test.tsx`. Home/format/Settings и Settings navigation /
  AccountBuilder targeted проверки PASS, TypeScript PASS. G3 OPEN.

- **S3 Activity follow-up (2026-09-08):** полный Vitest — **282 passed /
  37 failed, 28 files**. Activity contract/auth/scope и history regressions:
  **17 passed**; TypeScript PASS. Oxlint: одно оставшееся предупреждение
  `AppShell` ref access during render. Возвращён Activity TTL 60 s.

- **S3 continuation (2026-09-08):** history invalidation causal fence —
  `historyResources.test.ts`: **2 passed**, TypeScript build check PASS.
  Полный frontend suite после этого исправления пока не запускался; предыдущий
  результат выше остаётся baseline, причины всех падений ещё требуют разбора.

- **Mini App S2 / G2 — CLOSED (2026-09-07, `feature/frontend`, base
  `b10f01a`):** session-scoped shared resources, weak/strong singleflight,
  read/write/invalidation fencing, hard invalid-session scope reset, root
  keep-alive sharing, foreground/TTL, bounded Settings prefetch, nested cached
  first paint, session mutation locks, unknown-outcome reconciliation,
  nested scroll/focus + modal lifecycle. Frontend **27 files / 316 tests PASS**,
  oxlint 0/0, build PASS. Production-bundle CDP: cold 4 reads; root p95 1.0 ms
  (160), cached nested p95 1.9 ms (120), 300 ms RTT root p95 6.0 ms (160),
  warm API delta 0; Settings prefetch peak 2; 50-cycle DOM/listener counters
  unchanged, post-GC heap +3.25%, 0 long tasks/page errors. Explicit signed
  `status(true)` 5 reads: 1.45–2.21 s, median 2.05 s, fresh age=0/valid
  permissions. Поздние blocker-only аудиты дополнительно нашли causal-order race и
  неполную retained-unknown симметрию: ambiguous pause/resume/AccountBuilder и
  Risk/Notifications recovery теперь fence'ят pre-mutation read invalidation
  barrier'ом, а double-unknown остаётся locked через Back/remount до более позднего
  authoritative recovery без replay. Финальный post-fix независимый blocker-only
  re-review: **NONE**; полный verdict хранится в
  `MINIAPP_S2_PROGRESS_2026-09.md`. Device-only
  Telegram/WebKit/haptics acceptance остаётся S3.5/G3.
- **UX-кампания Mini App (план 2026-09), slice «шкала риска» — браузерная
  часть CLOSED (2026-09-06):** первый vertical slice по
  `MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md` §14/§21 (S2.5 input lifecycle +
  S3.4 motion): control выделен в `components/RiskRuler.tsx` + pure
  `riskRuler.ts`; wheel gating/deltaMode/границы, pointer-capture drag со
  scoped suppression, прерываемое доведение 140–220 ms, scrollend+fallback
  settle, save из ref-draft (надпись ≡ payload), constant-geometry
  highlight, edge fade, reduced-motion, `hapticSelectionChanged`.
  Gates: frontend Vitest **224 passed / 21 файл** (новые
  `riskRuler.test.ts`, `riskRulerBehavior.test.tsx`), oxlint +
  `tsc -b && vite build` clean; браузерная приёмка **15/15 PASS** +
  независимая перепроверка (вердикт «принимать») —
  `docs/reports/MINIAPP_RISK_RULER_2026-09.md`. Backend/торговля не
  затронуты, offline/testnet counters без изменений. Устройство-зависимая
  часть (Telegram haptics, BackButton, WebKit-инерция, snap-stop A/B)
  **отложена на потом — доступа к устройствам нет**; возврат в S3.5/G3.
  G1–G3 кампании не закрывает.
- **Stage 3, часть 2 (P2.6 — Account + Builder attestation) — ACCEPTED /
  CLOSED (2026-09-03, ветка `codex/stage-3-mini-app-part2`):** nested control screen поверх
  `UserApplicationService.account_builder()` — persistent `identity_source`
  + Builder snapshot (одна append-only migration, backfill `direct`),
  authoritative verified только одновременными signed `GET /agent` +
  exact-match `GET /builder` (typed `BuilderDTO`, без float), typed
  `AccountBuilderDTO` (только masked, state verified/expiring/stale/
  blocked/unavailable/not_applicable, remediation refresh/reauthorize/null),
  race-gate `account_version` + `IDENTITY_CHANGED`, same-signer
  reauthorization (`reauth-`, paused сохраняется), local-only disconnect с
  атомарной инвалидацией proof, единый trust source `isBuilderVerified()`
  для Account/Home/Status. P2.5 не рефакторился; engine/P2.7 не тронуты.
  1. **Offline gate:** offline pytest **977 passed / 2 skipped / 40
     deselected**, coverage **87%** (порог >= 87%); ruff check/format
     clean. Новые кейсы (117): storage identity (6), aster builder_api (9),
     attestation exact-match (13), reauthorize same-signer (12),
     application DTO (34), adapter reauth durability (16), credentials
     lifecycle (4), HTTP account_builder_server (23).
  2. **Frontend gate:** Vitest **167 passed / 17 файлов**
     (`accountBuilderBehavior.test.tsx` 11 + обновлённые settings/home/
     systemStatus), lint + `tsc` + build clean.
  3. **Live gate (Aster Testnet, scratch `sqlite3.backup`-копии прод-БД,
     прод-БД не тронута):**
     `tests/testnet/test_live_account_builder_acceptance.py` — attestation
     gate + local disconnect (exchange-авторизация пережила disconnect —
     local-only доказан) + verified-onboarding **`LIVE onboarded
     source=builder state=verified`** (fresh faucet-funded wallet,
     стандартный Part 1 flow, atomic paused commit, оба trust rail
     `builder_verified=true`). Live-факты биржи: never-deposited счёт —
     `-5050 after deposit` / `-1000 No agent found`; self-signed management
     отвергается; env-политика HEDGE.
     Детали — `STAGE3_PART2_PROGRESS_2026-09.md` (раздел P2.6).
- **Stage 3, часть 2 (P2.7 — Builder fee в copy-engine) — ACCEPTED / CLOSED
  corrective review пройден 2026-09-04, ветка
  `codex/stage-3-mini-app-part2`):** account-specific Aster Builder
  attribution в production copy-engine: `PlaceOrderRequest` + optional
  `builder`/`feeRate` (wire names, пара-инвариант, Decimal canonical string,
  NaN/Infinity/negative запрещены, absolute Futures cap `0.001` fail-closed,
  direct-сериализация byte-compatible), маленький
  `BuilderAttributionProvider` на composition boundary (DIRECT /
  BUILDER_VALID / BUILDER_UNAVAILABLE, durable P2.6 proof + current
  signer + Agent permissions/actual expiry + config match persisted
  address/fee vs `CT_BUILDER_ADDRESS`/`CT_MAX_FEE_RATE`, UI TTL и
  EXPIRY-warning не являются execution expiry), Executor применяет snapshot
  после journal identity под per-account mutation lock (command key и journal
  payload не зависят от metadata, `_with_coid` сохраняет attribution, retry
  сохраняет snapshot, UNKNOWN query-before-retry без fallback, boot recovery
  без Builder metadata), safety fallback (entry fail-closed до HTTP,
  close/Stop/TP без fields + degraded signal, cancel/leverage не блокируются).
  Planner/coordinator/reconciler не менялись. Frontend не менялся.
  1. **Offline gate:** offline pytest **1075 passed / 2 skipped / 42
     deselected**, coverage **87%** (порог >= 87%); ruff check/format
     clean. Новые кейсы (73): `test_builder_attribution.py` (60: payload +
     routing + strict tri-state permissions + time_in_force) + `test_builder_executor.py` (13: idempotency/fallback/race/
     emergency/security).
  2. **Frontend gate:** Vitest **168 passed / 17 файлов**, oxlint clean,
     `vite build` clean (без изменений Mini App).
  3. **Live gate (Aster Testnet, scratch копия прод-БД, прод-БД не
     тронута):** `test_builder_engine_attribution_live.py` — один
     production engine доказывает Builder и direct routing
     (`Projector -> Coordinator -> Planner -> Executor -> journal -> real
     AsterClient`, typed spy перед реальным `place_order`): Builder MARKET
     entry + STOP_MARKET + partial reduction + TAKE_PROFIT_MARKET + full
     close + final flat/no-orders, каждый Builder order с exact approved
          builder и Decimal-equal fee, direct всегда без fields; journal payload
     без `builder`/`feeRate`; честный above-cap probe: CONTROL с approved fee принят
     и отменён, тот же shape с превышением fee отвергнут именно -5026; слабый
     standalone-тест удалён, в `PlaceOrderRequest` добавлено optional `time_in_force`;
     `/builder/userTrades` с bare builder identity не queryable — честный non-gating
     limitation (как в Part 1 e2e). Full testnet suite: **41 passed / 1 skipped**
     — полный gate зелёный (review corrective: strict permissions, честный probe,
     стабилизация positions-теста с убранной INTENTIONAL_FLAT-интерференцией).
     4. **Owner acceptance (2026-09-03):** Computer Use-driven live-стенд, prod-БД: **9/9 PASS** (Home / Positions read-only / Activity / Risk-линейка без сохранения / Status ready / Account+Builder direct verified / Notifications вкл+выкл / Pause+Resume с confirm / регресс P2.0-P2.6); в прод-БД 0 builder-строк — builder trust покрыт P2.6 acceptance и scratch-live P2.7; стенд возвращён (active, риск 2,0%, уведомления выкл); скриншоты: docs/reports/acceptance-p27/ (12 PNG).
     Детали — `STAGE3_PART2_PROGRESS_2026-09.md` (раздел P2.7).
- **Stage 3 Part 2 post-closure Builder execution safety corrective (2026-09-04):**
  stale BUILDER_VALID (локальный proof valid, remote maxFeeRate ниже) отвергался -5026
  и без fallback блокировал close/Stop/TP/emergency. Fix: только -5026 классифицируется
  как authoritative Builder rejection; durable invalidation clear_builder_snapshot
  (identity_source остается builder → resolve BUILDER_UNAVAILABLE и после restart);
  ENTRY fail-closed без DIRECT retry; reduction — один DIRECT retry тем же command_key
  с новым COID, чистым journal и degraded builder-rejected:-5026; остаток plan
  переключается в UNAVAILABLE; DIRECT retry наследует UNKNOWN query-before-retry.
  Файлы: trading/builder_attribution.py, trading/executor.py, app.py wiring.
  Gates: offline 1089/2/42 coverage 87%, ruff clean, frontend 168/17 build clean,
  unit corrective 14, live corrective 1 passed, full testnet 42/1. Биржа flat/0 orders,
  прод-БД не тронута. Stage 3 Part 2 corrective = CLOSED, Stage 3 Part 2 = CLOSED.
- **Stage 3, часть 2 (P2.5 — System Status) — ACCEPTED / CLOSED
  (2026-09-03):** read-only control-plane slice поверх
  `UserApplicationService.status()` — новый typed `StatusDTO`
  (`state/system_state/copy_state/aster_state/permissions_state/masked
  identities/positions_count/permissions/snapshot_captured_at/last_check_at/
  age_seconds/reason/remediation`), precedence
  blocked → degraded → stale → ready, stale lifecycle на существующем
  runtime cache без minted timestamps, `POST /miniapp/api/status` со
  strict boolean boundary, `SystemStatusScreen` с transport-stale honesty
  и race guard, Telegram presenter на новом контракте. P2.6/P2.7 не
  начинались, Builder не verified.
  1. **Offline gate:** offline pytest **855 passed / 2 skipped / 34
     deselected**, coverage **87%** (порог >= 87%); ruff check/format
     clean, `git diff --check` clean. Новые кейсы:
     `tests/unit/application/test_status.py` (18),
     `tests/unit/miniapp/test_status_server.py` (19).
  2. **Frontend gate:** Vitest **149 passed / 15 файлов**
     (`systemStatus.test.ts` 8 unit + `systemStatusBehavior.test.tsx`
     13 behavioral + `settings.test.tsx` с 5 chevron-кнопками), oxlint
     clean, `tsc -b && vite build` clean.
  3. **Live gate (Aster Testnet):**
     `tests/testnet/test_live_status_acceptance.py` — **1 passed за
     7.54s** на `sqlite3.backup`-копии прод-БД (прод-БД не тронута):
     настоящие signed snapshot + permission reads → `ready/none`.
  4. **Browser walkthrough:** preview engine на копии прод-БД
     (`copyc1`, :8085, 390×844) — fresh + degraded, overflow 390/390.
  5. **Owner Telegram WebView acceptance P2.5 — COMPLETE (2026-09-03,
     iPhone, боевой бот + туннель, прод-БД):** fresh open; offline →
     transport-stale (данные целы, amber) → online → fresh; degraded
     («Система требует проверки»); Resume под degraded отклонён, после
     самоисцеления sweep'ом resume прошёл (аккаунт active, ready).
     Детали — `STAGE3_PART2_PROGRESS_2026-09.md` (раздел P2.5).
     Статус P2.5: ACCEPTED / CLOSED.
- **Stage 3, часть 2 (P2.4 — Risk on Stop) — CLOSED
  (2026-09-03, implementation/live/owner acceptance complete + external
  review corrective applied):**
  1. **Backend Application:** public `RiskDTO` больше не содержит
     `max_leverage`/`max_entry_deviation_pct` — только `risk_percent`,
     `min`/`max`/`step`/`presets`, `risk_base` (backend authority, все
     проверки через `Decimal`). Policy утверждена владельцем как developer
     (отклонение от чернового плана: min `0.005` = 0,5%, max `0.05` = 5%,
     step `0.005` = 0,5%, presets `0.01/0.02/0.03/0.05` = 1/2/3/5%).
     `update_risk()` меняет только `risk_percent`: range + step enforced,
     invalid/off-step/out-of-range/NaN/Infinity/float/bool → typed
     `ValidationError`, forged leverage/deviation fail closed, persistence
     через существующий `SettingsRepo`, control audit сохранён. Legacy
     `RiskPolicy`/storage/engine для торгового ядра нетронуты.
  2. **Transport:** `POST /miniapp/api/risk` (чтение) и
     `POST /miniapp/api/risk/percent` (mutation) поверх существующего
     HMAC/initData boundary. Строгая single-key граница: только
     `risk_percent` строкой; missing/bool/null/number/object/array/NaN/
     Infinity/malformed и любые лишние поля (legacy/forged/account_id) →
     `400 invalid_input`; в ответе только safe DTO, без exception text.
  3. **Frontend (`miniapp/`):** `RiskDTO` в `types.ts`;
     `api.risk`/`api.setRiskPercent`; Settings-строка «Риск на Stop»
     кликабельна с chevron; `{ kind: 'risk' }` рендерит новый `RiskScreen`
     (отклонение от `05-risk-settings.png` по решению владельца 2026-09-03:
     вместо пресетов и ручного ввода — интерактивная шкала-линейка в панели:
     `RiskRuler` со свайпом/drag/колесом/стрелками, snap, px-точным
     центрированием `fitRulerPadding`, живым трекингом rAF, fish-eye
     чёрточек и фиксированным курсором; крупное значение следует за
     выбором, `Сейчас` держит confirmed, тост `Сохранено · X` после save,
     линейка заморожена на время POST, legacy off-grid значение даёт
     баннер с явным перевыбором вместо молчания).
     Pure `riskView.ts` (string-math, без float); confirmed меняется
     только по server response.
  4. **Offline gate:** offline pytest **816 passed / 2 skipped / 32
     deselected**, coverage **87%** (порог >= 87%);
     ruff check/format clean. Новые кейсы:
     application policy matrix (min/max/step accepted, below/above/
     off-step/NaN/Infinity/non-Decimal/forged rejected, durability,
     audit) и HTTP boundary (auth/read/round-trip/19 malformed-forged
     payloads/non-object/ValidationError→safe 400, no legacy fields).
  5. **Frontend gate:** Vitest **127 passed / 13 файлов** (`risk.test.tsx`,
     20 тестов: enumerate/format/policy-gate/dirty/demo/nearest/ruler/
     nested route; новый `riskBehavior.test.tsx`, 5 behavioral: tap→dirty→
     single POST→confirmed+toast, failed save держит confirmed, freeze
     линейки mid-save, scroll/settle race перед POST отброшен, legacy
     banner; `settings.test.tsx` — 4 chevron-кнопки), oxlint 0/0,
     `tsc -b && vite build` clean.
  6. **Live gate (Aster Testnet):**
     `tests/testnet/test_live_risk_acceptance.py` — **1 passed за
     33.97s** на throwaway `sqlite3.backup`-копии прод-БД (рабочая БД
     доказано не изменилась: fingerprint + `copy_settings` до/после).
     Production path: HTTP read policy → set A=`0.01` → master LONG
     0.002 + Stop 73296.8 → reconcile → signed follower qty **0.004**
     == Decimal-расчёт; cleanup → set B=`0.03` → эквивалентная сделка →
     qty **0.012** == расчёт (ratio 3.0x); realized stop-loss обоих legs
     в пределах одного lot step от configured; forged `max_leverage`/
     `max_entry_deviation_pct` → 400. Cleanup flat best-effort.
  7. **Browser walkthrough + owner acceptance:** preview engine на копии
     прод-БД (`copyc1`, порты 8095/8096): Risk clean (save disabled) →
     тап по линейке (dirty `Сейчас 1,0% После 4,0%`, демо пересчёт) →
     double-tap save → single mutation → тост `Сохранено`, Settings
     показывает новое; края `0.005/0.05` и центр `0.03` — offset 0px;
     mobile 390×844 — иерархия по макету. Owner acceptance закрыт
     2026-09-03 в реальном Telegram WebView на iPhone (боевой бот +
     временный туннель, прод-БД): линейка, вибрация, fish-eye, края,
     тост — «Работает». Журнал —
     `STAGE3_PART2_PROGRESS_2026-09.md`.
  8. **External review corrective (remote `09a4d74`, оценка 8.8/10, P0/P1
     нет):** (а) legacy proof — все 5 строк `copy_settings` рабочей БД
     на новой сетке (`0.01`×4, `0.015`; read-only проверка) + UI-страховка
     `nearestRiskStep` с баннером и явным сейвом, без тихой перезаписи;
     (б) линейка `disabled` на время save (кнопки + scroll/wheel/drag/
     keyboard, `aria-disabled`); (в) living docs приведены к факту
     (см. пп. 3/5/7, PROGRESS, SKELETON, adapted plan deviation note).
     Follow-up ревью `40b8323` (последний P2-low): полный freeze —
     при `disabled` отменяются pending rAF/trailTimer, их колбэки
     перепроверяют флаг через ref, контейнер физически лочится
     `.risk-ruler[aria-disabled='true'] { overflow-x: hidden }`, после
     выхода из saving — гарантированный `centerFraction(active)` +
     repaint; regression-тест гонки (scroll→save до срабатывания
     колбэков, stale settle обязан не тронуть selection; негативный
     контроль: без фикса тест падает) — Vitest 127/13.
- **Stage 3, часть 2 (P2.3 — Settings root и Notifications) — CLOSED
  (2026-09-02, implementation/live/owner acceptance complete):**
  1. **Backend Transport:** `POST /miniapp/api/notifications` (чтение) и
     `POST /miniapp/api/notifications/enabled` (mutation) в
     `miniapp/server.py` поверх существующих `NotificationSettingsDTO`,
     `notification_settings()` и `set_notifications()` — новый
     service/repository/таблицы не создавались. На HTTP boundary принимается
     только настоящий boolean: missing/string/number/list/object →
     `400 invalid_input`; application-level strict-bool check и существующий
     control-audit boundary `set_notifications` сохранены. user_id только из
     проверенного initData (или testnet dev-header). Проводной DTO-сурфейс:
     `enabled`, `last_delivery_at`, `pending`, `failed`; chat_id,
     account_id, payload, last_error и другие outbox-internal поля никогда
     не покидают backend. Отключение уведомлений меняет только
     `telegram_users.notifications_enabled` (router-гейт доставки) и не
     затрагивает торговый путь.
  2. **Frontend (`miniapp/`):** interim `StubScreen` заменён настоящим
     `SettingsScreen` по эталону `06-settings.png`: статус-панель
     «Копирование» и capability-список с секциями (Настройки копирования /
     Подключение и безопасность / Поддержка): Риск на Stop, Уведомления,
     Статус Aster, Аккаунт и Builder, Безопасность, Помощь, Среда=Aster
     testnet. Chevron только у реально открывающихся в P2.3 строк
     (Уведомления, Безопасность, Помощь); Риск/Статус/Аккаунт+Builder
     (P2.4/P2.5/P2.6) и статическая Среда — read-only строки без chevron.
     Значения из authoritative `DashboardDTO` (`risk_percent`,
     `system_state`, `snapshot_captured_at`, `notification_enabled`,
     `account_masked`); stale/degraded никогда не подписаны «Всё
     работает», а «Статус Aster» читает «Всё работает» только при
     `ready` **и** непустом `snapshot_captured_at` — ready без snapshot
     показывает «Ожидание данных»; Builder — нейтральный masked wallet
     без «Проверено» до attestation (P2.6).
     Вложенный `NotificationsScreen` через существующий typed route stack +
     Telegram BackButton (новый nested kind `security`); toggle — button
     `role="switch"` с `aria-checked`/`aria-busy`, UI меняется только по
     DTO, возвращённому сервером; при ошибке сохраняется последнее
     подтверждённое состояние + error box; double tap заблокирован
     синхронным in-flight lock `createToggleGate()` до запуска API
     (disabled во время сохранения и reducer-гейт `save_requested` —
     render-бэкстопы). Безопасные delivery-факты: последняя доставка,
     в очереди, не доставлено. Pure view-models `settingsView.ts`
     (copySummary/systemStatusValue/notificationsValue) и
     `notificationsView.ts` (toggle state machine + toggle gate).
     AppShell рендерит nested screens (`notifications`/`security`/`help`)
     через `NestedRoute`.
  3. **Offline gate:** offline pytest **773 passed / 2 skipped / 32
     deselected** за 17.78s, coverage **87%**; ruff check/format clean,
     `git diff --check` clean. Новые HTTP-кейсы
     (`tests/unit/miniapp/test_server.py`): auth для обоих endpoint'ов,
     safe-DTO сериализация без outbox-internal полей, strict boolean
     (8 вариантов невалидного payload → 400 invalid_input), malformed
     body → 400, проброс реального boolean в application, typed errors
     (ValidationError→400 invalid_input, ServiceUnavailableError→503).
  4. **Frontend gate:** Vitest **97 passed / 11 файлов** — новые
     `settings.test.tsx` (11: view-model маппинги, honest tones для
     stale/degraded, порядок секций и строк, ровно 3 кнопки-chevron,
     отсутствие «Проверено» и внутренних идентификаторов, aria-label
     статус-панели) и `notifications.test.tsx` (10: toggle state machine —
     double-tap reference-равенство, серверный DTO как источник истины,
     сохранение confirmed при ошибке; a11y switch; loading frame),
     `navigation.test.ts` +1 (notifications/security/help на стеке);
     oxlint 0/0, `tsc -b && vite build` clean.
  5. **Live gate (Aster Testnet + реальный Telegram Bot API):**
     `tests/testnet/test_live_notifications_acceptance.py` — **1 passed за
     26.99s**. Production-композиция: `NotificationRouter` + App +
     `build_stage2_composition().application` (общий application layer с
     control audit) + production aiohttp transport + настоящий
     `TelegramOutboxWorker` с реальным ботом из `.botkey`; follower
     `tg-8264892202` (реальная пара user/chat 8264892202). Фазы: чтение
     через production HTTP → disable через HTTP (DB row=0) → реальный
     master BTCUSDT LONG + STOP_MARKET → production copy engine
     скопировал позицию (signed `positionRisk` follower ≠ 0) при
     **ноле новых outbox rows для пользователя** → enable через HTTP
     (DB row=1) → реальный partial close мастера → durable outbox row
     (id=130, PENDING) → `worker.process_once()` доставил через настоящий
     Telegram Bot API → **SENT** («Закрыта позиция BTCUSDT…», chat
     8264892202) → read endpoint вернул `enabled=true` и
     `last_delivery_at`. Cleanup: оба счёта flat, toggle восстановлен.
     FakeAster/MockTransport/fake sender не использовались.
  6. **Bounded impeccable + browser walkthrough:** detector по изменённым
     UI-файлам — 0 findings; один walkthrough мобильного viewport
     (390×844) на `preview_miniapp_engine.py` (копия prod-БД, порт 8093,
     production application + реальные DTO): секции/строки/chevrons,
     toggle-цикл вкл→выкл→вкл с обновлением UI по серверному DTO
     (подтверждено и в preview SQLite), Help/Security static screens,
     tab-switch сбрасывает nested stack. Corrective pass не потребовался.
  7. **Owner acceptance:** закрыта владельцем в реальном Telegram WebView на
     commit `fe84d2c`: Settings root и Notifications открываются штатно,
     переключатель фактически изменяет durable state через production HTTP →
     application → SQLite. В `control_audit` подтверждены три ручные mutation
     `disabled → enabled → disabled` в 21:08:51–21:08:53. Владелец явно принял
     P2.3; отдельный screenshot как дополнительный артефакт не требуется.
  8. **Corrective pass (2026-09-02, ревью P2.3 — три замечания):**
     1. *Storage-изоляция live-гейта:* `test_live_notifications_acceptance.py`
        работает на throwaway-копии реальной БД (`sqlite3.Connection.backup`
        в pytest `tmp_path` + `Settings.model_copy` для `db_path`/`log_path`):
        флаги, RiskPolicy, удаление execution_commands, telegram_users и
        outbox мутируются только в копии. Реальные Aster Testnet сделки и
        реальный Telegram Bot API сохранены. Re-run: **1 passed за 28.92s**
        (боевой экземпляр на время теста остановлен и поднят обратно через
        `scripts/dev-up.sh` — иначе прод-бот по WS дублировал бы копирование
        и перехватил phase-5 close); изоляция подтверждена: row 8264892202
        в прод-БД (`notifications_enabled`, `updated_at`) не изменился,
        контрольных `set_notifications` записей тест не оставил.
     2. *Синхронный double-tap lock:* `createToggleGate()` в
        `notificationsView.ts` закрывается до запуска `api.setNotifications`
        и открывается в `finally` после settle; render-проверки
        (`state.saving`, reducer `save_requested`) остаются бэкстопом.
        Regression-тесты (2): два одновременных тапа → ровно один admitted,
        лишний `end()` не ломает lock. Browser-проверка на новом бандле
        (`preview_miniapp_engine.py`, порт 8094): два `click()` по switch в
        одном синхронном тике → ровно **1** POST
        `/miniapp/api/notifications/enabled`, `aria-checked` перевернулся
        один раз (true→false), hint обновился по серверному DTO.
     3. *Статус требует snapshot:* `systemStatusValue(state,
        snapshotCapturedAt)` — «Всё работает» только при `ready` +
        непустом `snapshot_captured_at`; ready без snapshot
        (missing/null/`''`) → «Ожидание данных» (muted). Тесты: ready со
        snapshot → «Всё работает»/success; ready без snapshot →
        «Ожидание данных» (unit + render-контракт SettingsView);
        stale со snapshot → «Данные устарели»/warning (не «Всё работает»).
     4. *Corrective gate:* targeted frontend tests **26 passed**
        (notifications 12 + settings 14); полный frontend — **Vitest 102
        passed / 11 файлов** (было 97), oxlint 0/0, `tsc -b && vite build`
        clean; ruff check/format по изменённому Python-файлу clean;
        `git diff --check` clean. Полный offline suite не перезапускался —
        scope ревью ограничен изменёнными файлами; счётчики offline suite
        и live gate из пунктов 3/5 относятся к commit `fe84d2c`.

- **Stage 3, часть 2 (P2.2 — read-only Activity) — CLOSED (2026-09-02,
  live acceptance на Aster Testnet через production copy engine):**
  1. **Backend Application & Transport:** `ActivityCategory` (trade/system),
     `ActivityStatus` (completed/pending/failed), `ActivityFilter`
     (all/trade/system) и `ActivityItem` (category / title / status /
     symbol / occurred_at + optional pending_age / remediation) в
     `application/models.py`; старые `action`/`result`/`detail`
     удалены, `command_key`, `account_id`, addresses, nonce,
     `client_order_id`, raw Aster code, payload, traceback и внутренние
     exception-строки фильтруются server-side и никогда не попадают в
     DTO; `_activity_rows` и `AppStage2RuntimeAdapter.read_activity`
     выбирают `payload` дополнительно, чтобы backend извлекал `symbol`
     без раскрытия остального JSON; category predicate выполняется в
     SQL до `ORDER BY/LIMIT/OFFSET`, поэтому `trade`/`system` пагинация
     работает по отфильтрованному множеству и не теряет старые строки;
     неизвестные journal kinds остаются в fail-safe категории `system`;
     `POST /miniapp/api/activity` с
     валидацией Telegram initData (или testnet dev-header), body
     `{"page": N, "filter": "all|trade|system"}`, невалидный page/filter →
     400, прочие `ApplicationError` → соответствующие коды.
  2. **Frontend (`miniapp/`):** `types.ts` (`ActivityItemDTO`,
     `ActivityPageDTO`); `api.activity(auth, filter, page)`; pure
     view-model `activityView.ts` (`dayBucket` в UTC,
     `groupActivityByDay`, `activityStatusLabel`/`Tone`,
     `activityEmptyCopy`); `ActivityScreen` заменил interim
     `StubScreen` в `AppShell.tsx`; заголовок «Активность», подзаголовок
     «Безопасный журнал действий», фильтры Все/Сделки/Система, timeline
     с группами Сегодня/Вчера/Ранее, глифы completed/pending/failed,
     pending-age секунды, remediation под failed-строками, loading /
     error / empty / pending states, стабильная пагинация «Показать ещё»
     через `page+1` без потери уже отрисованных строк. Строго read-only:
     ни торговых кнопок, ни raw-кодов, ни технических payload.
     CSS: `.segmented`, `.activity-timeline`, `.activity-row`,
     `.activity-day-label`, `.activity-marker`/`.activity-connector`,
     `.activity-status` (tone-*), `.activity-remediation`,
     `.activity-safe-note`.
  3. **Offline gate (после P2.2 corrective pagination):** offline pytest
     **759 passed / 2 skipped / 31 deselected** за 11.96s, coverage
     **87%**; новые кейсы:
     `test_activity_safe_mapping_filters_and_sorts` (полный набор
     journal rows: open/close/stop/tp/cancel/leverage + pending + failed
     + unknown; safe DTO shape; trade/system/all filters;
     pending_age 40–60s окно; типизированная remediation copy;
     проверка отсутствия 17 запрещённых полей, в т.ч. command_key,
     account_id, address, client_order_id, nonce, private_key, -1003,
     boom), `test_activity_pagination_boundary` (25 строк, точная
     пагинация 10/10/5 через `page=0/1/2`, has_next/has_previous);
     `test_activity_category_filter_paginates_the_filtered_set` и
     `test_real_adapter_pushes_activity_category_filter_into_sql`
     фиксируют смешанный journal, непустые страницы обеих категорий,
     отсутствие пропусков/дубликатов и SQL pushdown на реальном SQLite;
     `test_activity_paging_validation_and_notification_toggle` дополнен
     `ValidationError` на невалидный filter. Ruff check/format и
     `git diff --check` clean.
  4. **Frontend gate (после P2.2):** Vitest **75 passed / 9 файлов** (11
     новых в `activity.test.tsx` — pure view-model + production
     `ActivityScreen` render: title, brand, testnet badge / wallet chip,
     loading state, отсутствие `<button` и торговых строк, отсутствие
     command_key/client_order_id/account_id/private_key/nonce/payload/
     traceback), oxlint 0/0, `tsc -b && vite build` clean.
  5. **Live gate (Aster Testnet):** `tests/testnet/
     test_live_activity_acceptance.py` — production copy engine: master
     открыл BTCUSDT LONG + Stop Market, `rebuild_full` +
     `reconcile_follower("copyc1")`, journal на `copyc1` через
     production `Executor`/`SQLite`, `POST /miniapp/api/activity` через
     production aiohttp path вернул safe DTO с
     `category: trade` / `title: "Открыта позиция BTCUSDT"` /
     `title: "Поставлен защитный Stop · BTCUSDT"` / `status: completed`
     / `symbol: BTCUSDT`; trade-filter показал те же записи с
     `category == "trade"` и `symbol == "BTCUSDT"`; production
     `Executor` выполнил реальный `SET_LEVERAGE` на testnet и записал
     его в journal, после чего system-filter вернул непустую отдельную
     выборку с `category == "system"` и заголовком `Плечо…`;
     partial close от мастера добавил `MARKET_CLOSE` row → Mini App
     endpoint вернул `"Закрыта позиция BTCUSDT"`; ни в одном item на
     проводе нет command_key / client_order_id / account_id / payload /
     address / nonce / private_key или raw Aster code; cleanup flat
     подтвердил сохранение safe DTO shape. `preview_miniapp_engine.py`
     на порту 8091 подтвердил отдачу safe DTO через тот же endpoint и
     HTML `/miniapp/` с собранным bundle. Corrective live gate
     **passed за 27.15s.**
     **Owner Telegram WebView acceptance закрыт 2026-09-02:** владелец
     проверил current HEAD на Builder-follower `0x3c45…F975` во время
     реального production copy-cycle. Экран Positions показал BTCUSDT
     LONG, актуальные Entry/Mark/PnL, подтверждённый Stop, риск до Stop
     и leverage; Activity показал подтверждённые `MARKET_OPEN` и
     `PLACE_STOP` в safe timeline без внутренних payload/secrets.
     После приёмки master был закрыт, production engine подтвердил
     `CANCEL_ORDER` + `MARKET_CLOSE` для `copyc1`/`copyc2`/`copyc3` и
     `tg-8264892202`; последующий reconciliation вернул 0 действий по
     всем четырём followers.

- **Stage 3, часть 2 (P2.1 — read-only Positions) — CORRECTIVE ROUND 2
  (2026-09-02, commit `111d2e5` под внешним ревью):**
  1. **Backend Application & Transport:** `PositionSummaryDTO`,
     `PositionPageDTO`, `PositionSide`, `PositionProtectionState`;
     `PositionState` расширен безопасным `unrealized_profit`; метод
     `service.positions(...)` и эндпоинт `POST /miniapp/api/positions` с
     Telegram initData защитой и Decimal-логикой `risk_to_stop_usdt` только
     по свежему snapshot и полностью подтверждённому Stop.
  2. **Freshness (corrective):** `AppStage2RuntimeAdapter` кэширует
     `(AccountState, captured_at)` с injectable clock; `positions()` бондирует
     свежесть TTL `max(2 × reconcile_interval_s, 30s)` — просроченный кэш
     помечается `is_stale=True` с `risk_to_stop_usdt=None`; при неудачном
     refresh выполняется fallback на кэш как stale с human-readable message.
  3. **Stop validation (corrective):** сторона Stop строго противоположна
     позиции (missing/unknown side → `UNCONFIRMED`); требуется
     `reduce_only`/`close_position`; quantity-based Stop обязан покрывать
     `orig_qty >= abs(qty)` — нулевой/невалидный объём → `UNCONFIRMED`,
     неполный положительный → `PARTIAL_STOP`.
  4. **Frontend UI (PositionsScreen):** настоящий `PositionsScreen` по
     эталону `03-positions.png` (секция «Открытые позиции · N», карточки с
     PnL, входом, текущей ценой, Stop/TP, риском до Stop, плечом «N× авто»,
     warning-блоками и stale-статусом); `accountMasked` пробрасывается в
     PositionsScreen и сохраняется сразу после onboarding (wallet chip);
     Home делает последовательный fetch (dashboard refresh → positions из
     кэша) без двойного signed snapshot.
  5. **Offline & Frontend Gates (после corrective):** offline pytest **749
     passed / 2 skipped / 30 deselected** за 36.7s, coverage **87%** (вкл.
     integration `test_resume_snapshot_tuple_contract.py` — P0-регресс с
     настоящим `AppStage2RuntimeAdapter`, TTL fresh/expired и
     refresh-failure fallback unit-тесты, строгие Stop validation кейсы);
     Frontend Vitest **64 passed / 8 файлов** (новый
     `positions.test.tsx` — `renderToStaticMarkup(PositionCard)`: рендер
     карточки, warning copy всех protection states, отсутствие торговых
     кнопок, скрытие риска при null); Oxlint clean; tsc + Vite build clean;
     Ruff check/format и `git diff --check` clean.
  6. **Signed Aster Testnet Live Gate:** `tests/testnet/
     test_live_positions_acceptance.py` — 8/8 фаз через реальный production
     copy engine (master `App`/`MasterProjector`/`Coordinator`/`Executor` →
     follower `copyc1` → Mini App aiohttp API → сверка с signed
     positionRisk/openOrders): master open + Stop, engine copy, positions DTO
     query, перестановка Stop + добавление TP, partial close, warning при
     снятии Stop, cleanup flat) — **повторно прогнан после corrective-коммита
     (2026-09-02): PASSED за 68.0s**. Production Pause/Resume через реальный
     Mini App HTTP (`test_builder_resume_live.py`: L7 pause→resume,
     L8 diagnostic snapshot) — **2 passed за 17.7s** после P0-фикса tuple
     unpacking в `_resume_account`.
- **Stage 3, часть 1 — CLOSED (2026-09-01):** все обязательные
  исправления R1–R9 из
  `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` выполнены.
  Offline, frontend, полный state-changing signed Aster Testnet suite и
  ручная Telegram Mini App приёмка владельца зелёные.
  1. **Unknown remote outcome:** transport/5xx/`-1000/-1006/-1007` после
     `approveAgent` сохраняют `APPROVAL_SENT`; recovery сначала проверяет Agent
     idempotent signed GET и не делает blind повтор POST.
  2. **Recovery без второй подписи:** session публикует только безопасный
     `onboarding_state`; `POST /miniapp/api/builder/resume-attempt` разрешён без
     подписи только для server-owned `APPROVAL_SENT`/`APPROVED`. `PREPARED`
     по-прежнему требует wallet signature.
  3. **Durable lifecycle:** после paused account commit записывается
     `COMMITTED`; успешная runtime activation очищает attempt, а activation
     failure восстанавливается лениво и идемпотентно.
  4. **Home/UI/logs:** первый Home load требует fresh snapshot; account
     `updated_at` больше не выдаётся за snapshot time; `Sheet` снова является
     modal overlay с Escape/focus management; raw exception payload не пишется
     в Builder/Mini App logs.
  5. **Live harness:** L3 действительно отправляет Testnet `approveAgent`, затем
     теряет локальный success response; L7 использует aiohttp Mini App API,
     намеренно stale seed и реальные signed account/Agent permission reads;
     order POST имеет unique client id и разрешает
     unknown outcome только через query. Fake-only evidence больше не выдаётся
     за signed live.
  6. **Актуальный offline/frontend gate:** offline pytest **740 passed / 2
     skipped / 30 deselected** за **17.02 s**, total coverage **87%**; Ruff
     check/format и `git diff --check` clean. Frontend: Vitest **53 passed**
     (Part 2 P2.0: navigation и corrective trust/accessibility cases;
     baseline Part 1 — 41 passed), Oxlint clean, TypeScript + Vite production
     build passed.
  7. **Актуальный полный signed Testnet gate:** в едином запуске
     все **29 обязательных state-changing/live сценариев passed** за
     **756.35 s** против `https://fapi.asterdex-testnet.com`. Пройдены
     Builder onboarding/recovery, Mini App Pause/Resume, builder+feeRate order,
     over-cap rejection, leverage control-plane, ONE_WAY/HEDGE, WebSocket,
     emergency, boot recovery, protective-order convergence и flat cleanup.
     Один legacy-дубликат, требовавший хранить main private keys в
     production credentials, был skipped в этом запуске и затем удалён:
     его полностью заменяет прошедший `test_builder_mini_app_e2e.py`.
  8. **Incident-resilience live evidence (настоящий Aster Testnet):** во время
     частичного инцидента signed
     `balance`, `positionRisk` и symbol-scoped `allOrders` отвечают `200`, тогда
     как `positionSide/dual`, `openOrders`, `listenKey`, `leverageBracket` и
     `agent` во время инцидента отвечают `-1000`. Добавлены только
     доказуемые fallback-пути: `openOrders(symbol) -> allOrders` с фильтром
     `NEW/PARTIALLY_FILLED`; REST master snapshot на каждом sweep при
     недоступном stream; unanimous current-boot follower leverage из
     `positionRisk`; 24h bounded permission cache только для явного Testnet
     override и только после unknown Agent-read failure. Частичные/расходящиеся
     данные, mainnet и deterministic permission rejection остаются fail-closed.
     После восстановления routes полный live-suite прошёл без этих
     fallback-путей как подмены assertions.
  9. **Финальный production runtime после live-suite:** `READY`, master
     user-stream запущен, Telegram polling и новая публичная Mini App
     отвечает `HTTP 200`. Startup/post-stream/sweep дали по `0` действий
     на `copyc1`–`copyc3` и
     `tg-8264892202`. Production Mini App flow `session -> confirmation ->
     resume` вернул `HTTP 200`, `copy_state=active`, `system_state=ready` для
     `tg-8264892202`; leverage readiness подтверждена реальными
     `positionRisk`: BTC/ETH=200, SOL=100. Оставленная emergency-тестом
     глобальная пауза снята; `allow_new_entries=true`, все пять аккаунтов
     enabled. Это отдельное transport/application evidence.
  10. **Owner manual Telegram acceptance:** production-лог подтверждает
      ручной WebView flow владельца: `13:08:42 pause attempt` →
      `13:08:44 pause success/state=paused` → `13:08:49 resume attempt` →
      real Testnet leverage confirmation BTC/ETH=200, SOL=100 →
      `13:08:58 resume success/state=resumed`. Последующие sweep дают
      `tg-8264892202: 0`. Ручной UI-gate закрыт.
- **Stage 1 CLOSED (corrective acceptance 2026-08-23):** новых P0/P1 по
  заявленному testnet scope не осталось; код можно фиксировать и пушить для
  перехода к Stage 2. Mainnet по-прежнему запрещён guard-ом Stage 1.
- **Stage 2 CLOSED (2026-08-24):** Telegram-бот, direct V3 onboarding,
  управление риском, notification outbox, общий Bot/Mini App application layer,
  а также полный цикл ручной приёмки владельцем (WP9b), включая живой
  `resume/copy cycle` (открытие BTCUSDT LONG, авто-плечо 200x, стоп, доставка
  Telegram-уведомлений `MARKET_OPEN`, `PLACE_STOP`, `CANCEL_ORDER`, `MARKET_CLOSE`,
  полный flat всех подписчиков), успешно выполнены и проверены на Aster Testnet.
- **Ручной WP9b ПРИНЯТ:** владелец подтвердил captcha, выдачу доступа, admin
  approval, direct onboarding/подключение, notifications toggle, изменение
  risk percent, итоговое inline-меню, а также полный живой resume/copy trading
  cycle на Telegram-follower.
- Все аккаунты (`main`, `tg-8264892202`, `copyc1`–`copyc3`) после цикла находятся
  в чистом `flat` (0 позиций, 0 открытых ордеров).
- Движок работает на futures testnet (`fapi.asterdex-testnet.com`), HEDGE-режим;
  ONE_WAY официально поддержан и покрыт signed E2E через ядро (см. ниже).
- Аккаунты (wallet.txt): `main test` (0x235E…5176, мастер) + copyc1–copyc3.
  copyc4 удалён: его API-кошельки на стороне биржи вели на счёт мейна
  (пересечение 49/50 ордеров подтверждено allOrders).
- **Leverage control-plane ПРИНЯТ (2026-08-25, внешний acceptance `69fa148`):**
  баги #25/#26 и весь leverage-этап закрыты цепочкой
  `86a51ec → 6f01917 → 54279d8 → 69fa148`: P0 dependency gating, flat-leverage,
  hot path без leverage REST, unknown-outcome без blind retry, crash recovery
  SET через authoritative positionRisk, ambiguous → fail-closed, boot-порядок
  catalog→reconcile, lifecycle worker'а. Дословные ревью:
  `docs/reviews/review_leverage_control_plane.md`; счётчики гейтов выше.
- Актуальный offline gate после внедрения leverage control-plane и финального acceptance (баги #25–26,
  `trading/leverage.py`, `69fa148`): **666 passed / 2 skipped / 21 deselected**, coverage
  **87.29%** при пороге 87%; ruff check/format clean. Signed testnet gate
  после изменений плеча (2026-08-25, `CT_TESTNET=1`): **16 passed / 1 skipped**
  (skip — Builder Stage 3). E2E ONE_WAY прошёл полный цикл через новый
  leverage-путь: fixture подтвердил candidate BTCUSDT=200 подписанным bracket
  GET + `POST /leverage` на copyc1, вход исполнился без холодного SET; первый
  прогон упал на транзитном `-1007 transport timeout` в polling-хелпере фазы 5
  (флак тестнета, ретрай не предусмотрен харнессом) — повторный прогон чистый
  за 178s.
- Ревью-гейт leverage control-plane (2026-08-25, независимый review `6f01917`):
  закрыты четыре находки. (1) `_prepare_locked`: unknown-outcome POST
  (`-1000/-1006/-1007`, 503/504 и любые не-биржевые ошибки) больше не
  триггерит слепой fallback — сначала авторитетный positionRisk: совпало с
  target → confirm без записей; доказуемо не применено → безопасный
  account-maximum fallback; чтение не удалось → fail closed. (2) Boot
  recovery для journaled SET_LEVERAGE без coid: исход разрешается через
  positionRisk — совпадение → CONFIRMED, расхождение → FAILED (reconcile
  переподготовит), нечитаемо → остаётся in-flight fail-closed. (3)
  Account-specific readiness после fallback: проверено воспроизведением —
  observe() УЖЕ сохраняет подтверждение той же revision (early-continue);
  несогласие с ревью зафиксировано, контракт заморожен регрессионными
  тестами (global=200/follower=100 → prepare → observe(100) → still ready;
  бамп ревизии инвалидирует). (4) Lifecycle: убран дубль task_names,
  выбрасывавший `_leverage_task`; shutdown-test с живым worker'ом
  доказывает остановку до закрытия HTTP/DB; initial refresh_catalog()
  переставлен ДО startup reconciliation (ARCHITECTURE §12 обновлён), чтобы
  рестарт-реконструкция из positionRisk валидировалась против живой
  генерации без лишних POST. Offline gate: **665 passed / 2 skipped**,
  coverage 87%; signed gate: **20 passed / 1 skipped** за 579s.
- Live-дополнение (2026-08-25, `tests/testnet/test_leverage_control_plane_live.py`,
  `CT_TESTNET=1`, 4 passed): закрыты лайв-дыры, недоказуемые офлайн-фейками —
  (1) завышенный candidate отвергается биржей (-4028) и fallback подтверждает
  подписанный максимум аккаунта; (2) неготовый control-plane режет вход
  LEVERAGE_NOT_READY без единого ордера, живой worker-цикл доводит готовность;
  (3) реально отклонённый SET блокирует зависимый MARKET_OPEN в executor'е,
  риск-снижающий MARKET_CLOSE при том же провале исполняется (P0-контракт на
  реальном -4028); (4) реальный дауншифт 200→20 держит пару durably-unready:
  закрытие мастера течёт, повторный вход скипается с enqueue подготовки и
  после prepare_account исполняется. Изоляция: торговые репо во временной БД
  на тест, общая локальная БД — только ключи/nonce. Полный signed gate после
  добавления: **20 passed / 1 skipped** за 571s. Вывод пробы: через полный
  coordinator-пайплайн отказной SET недостижим по конструкции — авторитетный
  снапшот лечит RAM-дивергенс до планирования; поэтому P0 доказан на границе
  executor'а с настоящим отказом биржи.
- Независимый signed REST-аудит после полного набора и двухчасового soak:
  `main` и `copyc1`–`copyc3` в HEDGE, позиции 0, открытых ордеров 0.
- Runtime leverage follower — RAM control-plane (`LeverageManager`,
  `trading/leverage.py`): глобальный кандидатный каталог по symbol
  stale-while-revalidate + account-specific подтверждённое плечо,
  подтверждаемое `POST /fapi/v3/leverage` (reject → account bracket GET →
  POST максимума аккаунта). Подготовка — на onboarding/resume/boot-recovery и
  в bounded background worker; hot path сделки читает только RAM. Рестарт
  восстанавливает состояние из authoritative positionRisk
  (`AccountState.symbol_leverages`, flat symbols включительно). Смена
  максимума биржей инвалидирует готовность нового входа до переподготовки;
  закрытие/сокращение/защита не зависят от плеча. Stage 2 по-прежнему не
  меняет `marginType`; isolated enforcement отложен.

## Найденные и исправленные баги (живой тестнет)

| # | Баг | Корневая причина | Фикс |
|---|-----|------------------|------|
| 1 | Осцилляция таргетов свипами | трекер master-магнитуд обновлялся только на WS-дельтах | remember на каждом recompute (`5e32837`) |
| 2 | Deadlock «database is locked» | implicit transactions/cursor lifetime и конкурирующий nonce path при fan-out | autocommit, закрытие cursors, общий `Database.write_lock`, atomic nonce `INSERT…RETURNING`; проверено 30×100 live |
| 3 | Координатор молча пропускал аккаунты | import не создавал copy_settings | дефолтная политика при импорте (`7f4a8b0`) |
| 4 | Дорастивание позиций после partial-close | ветка b считала cap от близкого стопа по устаревшему состоянию | STALE_MASTER guard (<5 c) (`0ae383a`) |
| 5 | Копиры никогда не получают TP | `_targets_for` хардкодил `tp_price=None` | уровень TP мастера в таргете (`b2a892d`) |
| 6 | **Фантомное переоткрытие позиции** | `_apply_fill` брал направление из ноги (LONG→+1): закрывающий SELL = «покупка»; ACCOUNT_UPDATE дублировал дельту | направление только по `S`; AU мутатором не является (`63ac80a`) |
| 7 | Ранний return пропускал присвоение qty | слоты защит чистились до `current.qty = new_qty` | очистка inline, без return (`e9ac649`) |
| 8 | Ре-вход после своего сработавшего ТП | guard покрывал только стопы | OWN_PROTECTIVE_FILLED: стоп ИЛИ тейк активного эпизода (`ff7f490`) |
| 9 | STOP/TAKE_PROFIT (лимитные флейворы) игнорировались | проектор знал только *_MARKET | слоты принимают оба флейвора, тип ордера копируется (`f85c9a8`) |
| 10 | Лимитные защиты отклоняются (-1102) | не отправлялся обязательный `price` | planner выводит price = trigger ±0.05% (`bce8c5b`) |
| 11 | -4014 Price not increased by tick size | `quantize("0.10")` держит 2 знака вместо сетки 0.1 | выравнивание делением на тик (`d834e9c`) |
| 12 | **Stale защита после FAILED dependent cancel** | `_pick()` снапшота выбирал только новейший Stop/TP: при `PLACE B CONFIRMED → CANCEL A FAILED` следующий reconcile видел current==target и выдавал пустой план — старый уровень висел рядом с новым | снапшот отдаёт дубликаты слота (`extra_stops`/`extra_tps`), planner-правило 9 всегда их отменяет, включая flat-ветку |
| 13 | **Ретрай CANCEL/SET_LEVERAGE после FAILED невозможен** | `_submit_cancel`/`_submit_leverage` всегда делали INSERT и падали на `UNIQUE(command_key)` при повторе той же транзиции | существующая FAILED-строка переводится в PENDING и ретраится под исходным командным ключом (вскрыто e2e-тестом пункта 12) |
| 14 | **Dust-сокращение до нуля переоткрывало позицию** | при `scaled_reduce → 0` внутри живого эпизода мастера `_persist_refs` стирал строку basis; следующий recompute видел «мастер в позиции + follower flat» и трактовал это как дрейф — немедленный re-entry сразу после намеренного закрытия | флаг `sync_basis.zero_target` (миграция #12): намеренный флэт персистится до конца эпизода мастера; входная ветка skip'ает `INTENTIONAL_FLAT`; флаг снимают новый эпизод мастера (`open_trade`) и `mark_zero_target` (сброс `issued_target`, чтобы следующий ненулевой таргет получил новую ревизию) |
| 15 | **Фикс №14 ломал ONE_WAY flip** | `_persist_refs` клеймил ЛЮБОЙ `qty=0` при живом мастере как intentional-flat: транзитный ноль ветки flip («мастер перевернулся — закрой эту сторону») получал маркер и навсегда блокировал открытие обратной стороны | явная причина нуля: `TargetPosition.hold_flat_until_episode_end=True` только для same-direction редукции в ноль; flip-ноль — транзитный, basis очищается; флаг входит в `_canonical_target` (разные command keys) (`79be6b1`) |
| 16 | **Flip не был границей эпизода** | `_apply_fill` открывал новый episode только при `active is None`: после флипа LONG→SHORT жил старый эпизод, а снапшот-синк вообще не смотрел на знак позиции | flip = закрыть старый episode + открыть новый с `direction=±1` (миграция #13 `copy_trades.direction`); snapshot-синк детектит рассинхрон направления после офлайна; новый эпизод автоматически снимает sticky-маркеры (`8ea3dbd`) |
| 17 | **Fixed Stop/TP не покрывал grow остаток** | follower snapshot терял `origQty`, а planner сравнивал только trigger level | `ProtectiveOrder.orig_qty`; market protection через `closePosition=true`; fixed STOP/TP quantity-aware replacement; authoritative post-fill safety и fail-safe flatten |
| 18 | **Повторный emergency проглатывался историческим CONFIRMED** | одинаковые payload/revision давали прежний command key, а pause не был частью аварийного flow | persisted UUID `operation_id`, pause до snapshots, bounded passes и два flat verification snapshots |
| 19 | **Boot мог дать READY при неизвестном финансовом исходе** | query-error оставлял in-flight, но startup не проверял остаток | структурированный boot report, bounded retry, unresolved блокирует READY и новые mutations |
| 20 | **READY переживал kill как ложный живой статус** | SQLite-флаг сбрасывался только graceful stop | runtime UUID + heartbeat; CLI считает старый heartbeat stale; kill/recovery доказан реальным SIGKILL |
| 21 | **Fake/Shadow скрывали exchange semantics защиты** | doubles допускали flip/oversize и не моделировали closePosition/fixed remainder | строгие ONE_WAY/HEDGE reduce rules, conditional expiration, closePosition и partial-fill hooks |
| 22 | **Direct onboarding скрывал причину preflight failure** | application service схлопывал `OnboardingError` в generic `ValidationError`, Telegram терял replacement context и показывал только restart copy | typed `OnboardingApplicationError.code`, enum-based Telegram remediation/error screen, revision-safe connect/replace retry и сохранённые secret/FSM invariants |
| 23 | **Onboarding-карточки наслаивались после повторного ввода** | старый prompt сохранялся рядом с новым, expiry отправлял отдельный ответ, а удалённый anchor оставался в FSM до replacement send | все три input-сообщения удаляются до проверки; valid step transitions используют fresh cards с edit-fallback без дублей; expiry заменяет текущую карточку recovery-экраном; retired anchor инвалидируется и удаляется из FSM до send |
| 24 | **Сырые системные логи в Telegram-уведомлениях** | движок слал внутренние имена RPC-команд (`MARKET_OPEN`, `PLACE_STOP`, `CANCEL_ORDER`, `NO_STOP`) напрямую в пользовательский чат | выделен модуль `notifications/formatter.py` (`format_confirmed_action`, `format_skip_reason`); уведомления форматируются в чистые трейдерские карточки (🟢 Открыта позиция, 🛡 Защитный стоп, 🎯 Тейк-профит, 🔴 Закрыта позиция, 🗑 Снят ордер, ⏳ Ожидание стопа), технический шум убран |
| 25 | **FAILED `SET_LEVERAGE` не блокировал `MARKET_OPEN`** | priority в плане — только порядок, а не зависимость: исполнитель продолжал план после FAILED prerequisite и отправлял вход с неподтверждённым плечом (fail-closed для плеча фактически отсутствовал) | generic dependency-модель плана: `ExecutionAction.action_id` / `depends_on_action_id`; planner связывает SET_LEVERAGE → вход/защиту, executor блокирует (`BLOCKED`) зависимые действия при FAILED/UNKNOWN prerequisite; закрытия/отмены зависимости не получают |
| 26 | **Движок терял известное плечо flat-symbol** | `_snapshot()` выбрасывал строки positionRisk с `positionAmt==0`, а planner трактовал «нет позиции» как «плечо неизвестно» — каждая новая сделка платила холодный `SET_LEVERAGE` (+TTL bracket GET) даже при годами стоящих на бирже 200x | `AccountState.symbol_leverages` из всех строк positionRisk (включая flat; неоднозначные per-side значения → unknown/fail-closed); planner сравнивает known vs target и эмитит SET только при расхождении; hot path сделки — ноль leverage REST-запросов |
| 27 | **Liquidation guard и HEDGE geometry** | Котировка 25.5M вызывала churn; последующий guard относительно entry пропускал допустимую liquidation между entry и mark | Corrective: adverse direction относительно текущего mark (равенство считается опасным), регрессии LONG/SHORT и signed LC4; новая live-квалификация ожидается; квалифицировано 2026-09-05: Package G 7/7 CLOSED — wrong-side guard в `Coordinator._liquidation_violated` (см. раздел Package G ниже) |

## Известные ограничения (осознанные решения этапа 1)

1. **ACCOUNT_UPDATE информационный**: позицию мутирует только TRADE-дельта;
   пропущенный ивент = дрейф до reconnect (логируется, лечится rebuild_full).
2. **Вход копиров всегда MARKET**: лимитка мастера копируется в момент
   исполнения (fill-copy). Зеркалирование висящих лимиток — этап 2.
3. `availableBalance` тестнета колеблется сильнее wallet balance — сайзинг
   считается от него (policy risk_base=available_balance).
4. **Один runtime process.** Heartbeat обнаруживает kill/stale и смену runtime,
   но не является распределённым mutex для двух одновременно запущенных App.
   Для одного трейдера это операционное правило запуска, а не повод добавлять
   Redis/оркестратор в Stage 1.
5. Перед запуском оператор подтверждает одинаковый position mode аккаунтов.
   Signed preflight и финальный audit это делают; автоматическая смена mode на
   аккаунте с открытой позицией может быть отклонена самой биржей.

## Кампания полной проверки (12 пунктов) — 2026-08-22

| Пункт | Статус | Результат |
|-------|--------|-----------|
| 1. Покупка long | ✅ | копиры BUY + стопы, ~7 c, WS-путь |
| 2. Продажа short | ✅ | SELL + BUY-стопы, ~7 c |
| 3. Тейк-триггер | ✅ | TP @77942/77847 сработал мастеру и всем копирам в одну секунду |
| 4. Стоп-триггер | ✅ | стоп @78073 → TRADE l=0.002, все копиры вышли своими стопами |
| 5. Перенос стопа | ✅ | place-new→cancel-old, мелкие движения 10-15$ |
| 6. Перенос тейка | ✅ | move-tp 78200→77942, PLACE_TP+CANCEL ×3, ~5 c |
| 7. Несколько позиций | ✅ | BTC long + SOL long одновременно, независимые эпизоды |
| 8. Казусы | ✅ | kill -9 с позой → recovery без дублей; быстрый цикл эпизодов; ручная отмена защиты мастером → корректный NO_STOP |
| 9. Вход без стопа→set-stop→set-tp | ✅ | SKIP(NO_STOP) → вход после set-stop → PLACE_TP после set-tp |
| 10. Вход лимиткой | ✅ | висел 2.5 ч без действий копиров; филл → реакция за секунды |
| 11. Лимитные защитные (STOP) | ✅ | copyc1–3 держат type=STOP sp=78201 px=78161.90 (биржа) |
| 12. Сверка ордеров с биржей по clientOrderId | ✅ | каждый прогон |

## Инфраструктура прогонов

- Скрипт `scripts/manual_master_order.py`: open/open-nostop (long/short),
  set-stop/move-stop, set-tp/move-tp, partial-close, close.
- Движок — hub-процесс `engine`; журнал команд — SQLite `execution_commands`,
  события мастера — `master_events`; сверка — query_order/allOrders по coid.

## Рефакторинг по review_stage1 (2026-08-22)
Проведён рефакторинг по итогам внешнего код-ревью: persisted sizing basis
(restart после пропущенного partial close), идемпотентность по ревизии перехода,
boot-recovery SENDING/SUBMITTED/UNKNOWN с order_links, async persist-before-send
nonce, реальный pause, deviation/post-fill риск, перенос стопа в прибыли,
tick-grid равенство, TP removal/orphan cleanup, HEDGE-rebuild защита, TRADE вне
порядка + entry от L, fail-closed routing, decoupled WS ingest.
Полная инвентаризация и верификация — [REFACTOR_STAGE1_REPORT.md](reports/REFACTOR_STAGE1_REPORT.md).
На момент закрытия review-specific доработок герметичный сьют дал **240 passed /
10 skipped** без
`CT_TESTNET`, `ruff check` и `ruff format --check` — clean. Signed testnet:
**9 passed / 1 skipped**; отдельно после финальной правки dependency gate
повторены one-tick stop replace и ONE_WAY cycle — **2 passed**. Read-only
сверка после cleanup: `main` снова HEDGE, `main` и copyc1–copyc3 flat,
открытых ордеров нет. Единственный skip — Builder e2e, который не блокирует
торговое ядро Stage 1 и перенесён в контур Stage 3.

## Hybrid load campaign — 2026-08-23

Проведён отдельный testnet-only нагрузочный гейт перед ревью:

- `scripts/hybrid_load_test.py`, 30 follower slots, 100 циклов;
- 3 REAL подписанных follower-а (`copyc1`–`copyc3`) + 27 явно помеченных
  SHADOW rows; fake credentials не создавались;
- **12 302/12 302 `CONFIRMED`**, `FAILED/UNKNOWN/non-terminal=0`,
  `ingest_dropped=0`, cleanup errors=0;
- run-БД до/после cleanup: 31 account flat/no-orders;
- независимый signed REST после прогона: `main` и copyc1–copyc3 flat,
  `open_orders=0`;
- `CT_LOAD=1` production-path integration: **1 passed** (30×100 shadow
  cycles), full suite после изменений: **247 passed / 11 skipped** (счётчик
  той кампании; актуальный — в «Текущем состоянии» и разделе 4).

Финальный JSON: `data/hybrid-load-20260822T233258Z.json`. Полная матрица
evidence и ограничения shadow-контура: [HYBRID_LOAD_TEST_REPORT_2026-08.md](reports/HYBRID_LOAD_TEST_REPORT_2026-08.md).

## Финальный gate по внешнему код-ревью — 2026-08-23

Текст ревью: [review_stage1_final.md](reviews/review_stage1_final.md). Два пункта:

### 1. P1 — защитная конвергенция (последний code blocker)

Сценарий ревьюера воспроизведён и закрыт на трёх уровнях:

- юнит (`test_planner_transitions.py::TestDuplicateProtectionConvergence`):
  extras отменяются при совпадающем target, рядом с обычной заменой (без
  двойного cancel), для TP и в flat-ветке;
- e2e на FakeAster (`test_end_to_end_fake.py::TestDuplicateProtectionConvergence`):
  инжект FAILED dependent cancel после CONFIRMED replacement → оба уровня
  висят → следующий reconcile удаляет stale, журнал содержит CONFIRMED CANCEL
  по старому coid, повторный проход выдаёт 0 команд; то же для TP и flat;
- **побочный наход e2e:** ретрай CANCEL/SET_LEVERAGE после FAILED падал на
  `UNIQUE(command_key)` — зависимый cancel не мог повториться вообще. Исправлено
  (FAILED → PENDING под исходным ключом), покрыто тем же e2e.

### 2. ONE_WAY signed E2E через ядро

Новый `tests/testnet/test_one_way_engine_e2e.py`: master open → stop →
stop move (замена защиты) → partial close → TP → master close, всё через
Projector→Coordinator→Risk→Planner→Executor→journal на REAL follower `copyc1`.
Живой прогон 2026-08-23: **PASSED за 36 с**. Аудит журнала: 0 FAILED / UNKNOWN /
BLOCKED; все закрывающие команды с `reduceOnly=true` и без `positionSide`;
финал — оба аккаунта flat, открытых ордеров нет. Первый запуск вскрыл ошибки
самого сценария (снятие baseline после события, неверное ожидание при
минимальном размере копира) — предикаты приведены к семантике движка.

### 3. Dust zero-target edge case (второй проход ревью, 2026-08-23)

Ревьюер принял оба пункта gate и нашёл последний edge case: при
`scaled_reduce → 0` (мастер режет позицию так, что пропорция follower'а
округляется ниже step) `_persist_refs` стирал basis-строку; следующий
post-fill recompute видел «мастер открыт + follower flat» и **немедленно
переоткрывал** намеренно закрытую ногу.

Фикс: персистентный маркер `sync_basis.zero_target` (новая миграция #12):
- `_persist_refs`: `t.qty == 0` при живом эпизоде мастера → `mark_zero_target`
  вместо `clear`; реальное закрытие эпизода → `clear` как раньше;
- входная ветка координатора: skip `INTENTIONAL_FLAT`, пока маркер активен
  (после OWN_PROTECTIVE_FILLED, до PAUSED);
- маркер снимают: новый эпизод мастера (`CopyTradeRepo.open_trade`) и сам
  `mark_zero_target` (сбрасывает и `issued_target` — иначе следующий ненулевой
  таргет совпал бы с нулевым в ревизии и новый MARKET_OPEN проглотила бы
  идемпотентность по command_key).

Контрольный тест защищает починенный drift-recovery: ненулевой таргет +
ручное закрытие follower'а → движок по-прежнему восстанавливает позицию той же
величины. Live dust-фаза добавлена в signed ONE_WAY E2E (phase 4b).

### 4. Третий проход ревью: flip-регрессия и фальшивое dust-evidence (2026-08-23)

Ревьюер вскрыл два дефекта предыдущего фикса, оба подтверждены кодом и живыми
прогонами:

1. **Flip-регрессия**: фикс №14 трактовал ЛЮБОЙ `qty=0` при живом мастере как
   intentional-flat. Транзитный ноль ветки flip получал маркер → SHORT после
   LONG→SHORT не открывался никогда (до следующего «настоящего» эпизода).
2. **Dust-фаза signed E2E не выполнялась**: guard
   `dust_left = snap(master_qty*0.005) >= step` при `0.003*0.005=0.000015`
   давал `0 < step` → вся фаза молча пропускалась, «passed за 38 c» ничего не
   доказывал. Плюс в синтетическое событие передавался ОСТАТОК позиции вместо
   объёма исполненного SELL.

Исправление (глобальное, по требованию ревьюера — не точечные заплатки):

- **Явная причина нулевого таргета** — `TargetPosition.hold_flat_until_episode_end`:
  ставится только для same-direction редукции в ноль; flip-ноль транзитный.
  `_persist_refs` клеит маркер только при флаге; флаг входит в
  `_canonical_target` → разные command keys для sticky-dust и flip-zero (`79be6b1`).
- **Episode-семантика флипа**: `_apply_fill` при flip закрывает старый episode и
  открывает новый с `direction=±1`; миграция #13 `copy_trades.direction`;
  snapshot-синк детектит смену знака после офлайна; новый эпизод снимает
  sticky-маркеры автоматически — система самосогласована (`8ea3dbd`).
- **Тесты**: офлайн `TestMasterFlipEpisodeSemantics` (оба сценария ревьюера,
  включая «dust-flat → flip»); в signed E2E dust-фаза стала ОБЯЗАТЕЛЬНОЙ
  (assert вместо тихого skip: первый же прогон честно упал на новом assert'е —
  математическое доказательство того, что раньше она не выполнялась), остаток
  мастера = ровно один step (цель follower'а гарантированно флорится в ноль),
  состояние проектора — из авторитетного `rebuild_full`, а харнесс явно гонит
  recompute_follower (у голого харнесса нет своего reconcile-цикла);
  новая фаза 4c — реальный signed flip LONG→SHORT через полное ядро с
  направленно-осознанной защитой/TP/закрытием (`7122a28`).

### Итог прогона

- Полный офлайн-suite: **258 passed / 1 skipped**, coverage 87%; ruff check +
  format clean.
- Signed testnet (CT_TESTNET=1): **10 passed / 1 skipped** (Builder — нет main
  private key, Stage 3), включая ONE_WAY E2E с обязательной dust-фазой и
  live-flip: PASSED за 155 с (run 7); полный сьют — 203 с.
- Hybrid load gate (после всех фиксов): **100/100 циклов**, 30 слотов
  (3 REAL + 27 SHADOW), **12207/12207 CONFIRMED**, 0 FAILED / UNKNOWN /
  SUBMITTED / SENDING / BLOCKED, ingest_dropped=0, cleanup errors=0,
  shadow_signed_requests=0, 31/31 аккаунтов flat/no-orders до и после cleanup;
  1 авторитетная рекавери мастера. Артефакты:
  `data/hybrid-load-20260823T075504Z.{json,sqlite3}`.
- Оба пункта первого gate + dust edge case второго прохода + оба дефекта
  третьего прохода закрыты с офлайн-регрессами и живыми signed-доказательствами.
  Решение о статусе этапа — за владельцем проекта/ревьюером.

## Corrective acceptance — 2026-08-23

Повторное открытие Stage 1 по CR-1..CR-5 завершено. Детальная хронология,
включая все непринятые попытки, находится в
[STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md](reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md).

Финальные gates:

- offline: **284 passed / 2 skipped / 16 deselected**, coverage **88.41%**;
- static/package: ruff check, ruff format check, git diff check — clean;
  `uv build` создал sdist и wheel без прежнего README-warning;
- signed testnet: **15 passed / 1 skipped за 510.48 s**; REAL-01..08 включают
  ONE_WAY/HEDGE protection sizing, closePosition contract, emergency дважды,
  boot found/not-found и настоящий App/WS reconnect/critical recovery;
- hermetic scale: 30 SHADOW × 100 и 100 SHADOW × 20 расширенных циклов;
- hybrid: 3 REAL + 27 SHADOW, 20/20 циклов, **3642 CONFIRMED**, остальные
  statuses 0, p50 35.118 s / p95 39.253 s / max 39.918 s, 31/31 account
  flat/no-orders;
- SOAK-01: **7203.589 s**, 6/6 signed cycles, forced reconnect, graceful
  restart, реальный child SIGKILL (`-9`) + stale heartbeat 33.709 s + recovery,
  24 periodic flat audits, **218 CONFIRMED**, in-flight/ingest drops/cleanup
  errors 0;
- независимый финальный signed REST: `main`, `copyc1`, `copyc2`, `copyc3`
  HEDGE и flat/no-orders; run и рабочая SQLite: in-flight 0, sticky zero 0,
  stopped runtime `ready=false`.

Финальные локальные артефакты:

- hybrid JSON SHA-256
  `6c0f8fe461d159377215d454500266256d5a410197b1fc27f8a6b42234cebd9e`;
- hybrid SQLite SHA-256
  `fc011bb8f30a380e9ca895e852cdc19d448c4269db1ac6862c6cc939db2ac62a`;
- soak JSON SHA-256
  `a6fbb57a1f06abb51843d5af30a35a8565c82b2e66da86be9cd08a4f5c59793d`;
- soak SQLite SHA-256
  `42dfdd4b23bc5234cf503ba38d0c6f67b623f2ad9f6faca17d5aeac6eb5ad1aa`.

Verdict: **Stage 1 CLOSED, push approved for the testnet trading core.**
Builder остаётся Stage 3; mainnet не разрешён этим verdict.

## Stage 2 automated acceptance — 2026-08-24

Реализован минимальный Stage 2 для одного master и 10–30, с проверенным
контуром до 100 Telegram users/followers:

- aiogram 3.30.0 long polling в том же управляемом процессе, что и `App`;
- современный inline-first dashboard: один редактируемый anchor message,
  typed callbacks и slash-команды только для входа/recovery;
- общий transport-neutral application service для Telegram и будущей Mini App;
- direct V3 onboarding/replacement/removal: проверка address/key pair,
  подписанный `GET /fapi/v3/agent`, permissions/expiry/IP preflight,
  зашифрованное atomic storage и activation только в paused state;
- owner-scoped risk/status/pause/resume/emergency с короткоживущими
  confirmations, account-version guard и сериализованными barriers;
- durable recipient-specific SQLite outbox с lease/retry/backoff и изоляцией
  Telegram 403;
- hot-add/recovery runtime через публичный registry, без переписывания Stage 1
  projector/coordinator/planner/executor/reconciler.

`Scenes Wizard` отдельно проверен по stable aiogram documentation. В Stage 2
он намеренно не используется: Scenes остаются experimental и предназначены
для ветвящихся wizard flows, а не для постоянного меню. Dashboard stateless;
короткие линейные формы используют обычный `StatesGroup`. В source tree нет
production-import `Scene`, `SceneRegistry` или `SceneWizard`; это закреплено
регрессионным тестом.

Автоматические доказательства:

- offline: **541 passed / 2 skipped / 17 deselected за 15.37 s**;
- coverage: **87.88%** (`8399` statements, `1018` missing), обязательный порог
  **87%** пройден;
- отдельный Stage 2 scale gate: **3 passed** — 30 одновременных users и
  100-user burst/restart/backpressure;
- signed testnet: **16 passed / 1 skipped за 395.51 s**; skip — Builder Stage 3;
- реальный Bot API: `getMe`, private command scope и menu button настроены;
- реальный composition smoke: `App` прошёл recovery/reconciliation/WS/READY,
  затем aiogram polling запустился и завершился по `SIGINT` с exit code 0;
- package gate: `uv build` успешно создал sdist и wheel;
- static gate: `ruff check`, `ruff format --check`, `git diff --check` — clean;
- независимый signed REST после suite: `main`, `copyc1`, `copyc2`, `copyc3`
  в HEDGE, nonzero positions 0, open orders 0.

Fail-closed Agent check с production-default выявил внешний prerequisite:
`ipWhitelist` у всех трёх текущих follower Agent пуст. Поэтому автоматический
signed-run использовал только явный `CT_TESTNET_IP_WHITELIST_OVERRIDE=true`;
этот override конфигурационно запрещён вне testnet. Для ручной приёмки владелец
должен либо настроить статический egress IP в Aster и `CT_ASTER_EGRESS_IPS`,
либо временно оставить testnet-only override.

Verdict: **автоматическая часть Stage 2 принята; code/static/signed blockers
не найдены.** Следующая полезная проверка требует действий владельца в private
Telegram chat и отдельного flat testnet follower account. До её результата
WP9b и общий Stage 2 остаются открытыми.

### Owner correction: admin UX — 2026-08-24

После замечания владельца устранено избыточное разделение `ADMIN`/`USER`:

- immutable Telegram ID владельца настроен локально как единственный admin и
  записан в рабочую SQLite (`role=ADMIN`, `access=on`); сам ID остаётся только
  в git-ignored `.env`/рабочей БД;
- admin — capability из env, а не отдельный человек: тот же ID при
  необходимости может владеть одним follower account;
- inline-админка показывает master summary, пользователей и счета подписчиков;
- доступ новому пользователю выдаётся и отзывается кнопками; неизвестный private
  user проходит inline captcha, после чего durable заявка сама уходит configured
  admin; ручной numeric Telegram ID оставлен только recovery;
- revoke закрывает только вход в UI, не бросает торговый account и не смешан с
  отдельной настройкой доставки уведомлений;
- users/followers листаются страницами по 10; 30-account pagination покрыта;
- targeted reconcile привязан к stable opaque SHA-256 account key, поэтому
  изменение списка между render/click не перенаправляет действие соседу;
- `authorize()` проверяет совпадение Telegram user/chat ID; legacy case-only
  duplicate Aster addresses дают диагностируемый `MigrationConflictError` до
  unique-index migration;
- owner+admin получает ровно одну outbox row на critical event;
- user-facing статусы и onboarding copy переведены на обычный русский:
  «счёт-источник», «права подключения», «закрытый ключ подключения».

Независимые Luna Max-аудиты: первоначально найдены три P1 (exclusive role,
index-based targeted reconcile, legacy migration crash) и P2 UX/auth; после
доработки P0/P1 не осталось. Финальный offline/static gate указан выше.

Реальный post-correction запуск в 10:41 MSK дошёл до App READY и Telegram
polling, затем штатно остановлен (снимок запуска на 10:41 MSK). Два более поздних
повтора в 10:57 MSK
fail-closed завершились до READY на внешнем Aster testnet `-1007 transport
timeout`; Telegram mutations не принимались. После отдельной проверки public
Aster health заключительный запуск в 11:05 MSK снова прошёл startup и
post-stream reconciliation, достиг READY и запустил polling, затем также был
штатно остановлен (снимок запуска на 11:05 MSK). Для ручной owner acceptance
процесс запускается отдельно.

### Access approval hardening — 2026-08-24

По дополнительному owner flow «второй аккаунт → подтверждение админом» закрыт
первичный доступ без ручной пересылки ID:

- неизвестному разрешены только private `/start` и typed guest captcha callbacks;
  ошибка authorization backend остаётся fail-closed, forged menu callback не
  достигает dashboard/application facts;
- captcha single-use с TTL/attempt limit передаёт проверенного пользователя в
  durable `telegram_access_requests`; один `PENDING` на user защищён SQLite
  transaction;
- карточка approve/reject отправляется только immutable configured admin IDs из
  env; резервный inline экран `Админка → Заявки` остаётся доступным, manual ID —
  recovery-only;
- approval/reject повторяемы: только первый click меняет `PENDING`, approval
  атомарно выдаёт `access_enabled` и сохраняет account/role/notifications;
  Bot API вызывается после commit;
- ошибка direct delivery не откатывает durable state; после approval бот пытается
  отправить первое меню с V3 connect CTA, а `/start` остаётся recovery;
- `AccessRequestDTO.created` и `create_pending_result()` подавляют duplicate
  admin cards при гонке captcha callback;
- Scenes не добавлялись: menu stateless, credential/risk input остаются обычными
  `StatesGroup`.

Regression evidence (без реального бота и без рабочей DB): access/storage/
application/telegram subset — **162 passed за 2.88 s**; ранее зависавший exact
набор engine/service/adapters/storage/notifications — **105 passed за 2.51 s**;
полный offline — **554 passed / 2 skipped / 17 deselected**, coverage **87.07%**.
Полный `ruff check .`, `ruff format --check .` и `git diff --check` — clean.
Детальный журнал: `docs/reports/STAGE2_ACCESS_APPROVAL_2026-08.md`.

Источник прошлого зависания не воспроизведён: адресный старый pytest-процесс
остановлен SIGTERM, после чего тот же точный набор завершался штатно под shell
timeout 120 s. В наборе есть bounded notification-worker sleeps/stop timeouts,
но текущий прогон доказал их завершение; отдельный детерминированный blocker не
найден.

### Onboarding UX и typed-error corrective gate — 2026-08-24

По live owner feedback исправлен direct onboarding path без доступа к live
процессу и рабочей SQLite:

- application boundary сохраняет только `OnboardingErrorCode` через
  `OnboardingApplicationError`; raw exchange errors, addresses и key material
  не попадают в Telegram/Mini App contract;
- introductory/prompt copy использует фактические названия Aster UI и коротко
  объясняет prerequisites: окончательная authorization API wallet подписью
  обычного wallet, read/perpetual permissions, withdrawal off, пустой account и
  Hedge;
- все предусмотренные preflight/permission/account/identity/commit/activation
  classifications имеют безопасный renderer с remediation, а unknown получает
  временный retry;
- private-key message удаляется до application call, FSM очищается в `finally`,
  replacement retry сохраняет branch через typed callback key, preflight failure
  не достигает commit;
- parameterized application/renderer/Telegram tests покрывают enum mapping,
  отсутствие raw key/address/error, recovery buttons, deletion и replacement
  retry.

Acceptance evidence:

- focused application + Telegram onboarding/rendering: **151 passed**;
- offline gate: **597 passed / 2 skipped / 17 deselected**, coverage **87.13%**
  при пороге 87%;
- `ruff check .`, `ruff format --check .`, `git diff --check`: clean;
- live bot не запускался и не перезапускался.

Вероятные причины исходного live отказа остаются классификацией до повторного
ввода после этой правки: незавершённая подпись авторизации API wallet (Agent не
найден), несоответствие API wallet address/private key, либо permission/read
или signed-read prerequisite. Утверждать одну уже известную причину нельзя,
поскольку старый generic boundary её намеренно потерял.

### Persistent safe-logging corrective gate — 2026-08-24

Проверен и доведён logging boundary после замечания, что важное onboarding
действие нельзя диагностировать только по UI:

- default `data/logs/copytradeaster.log` создаётся при обычном `setup_logging`
  без изменения env; console остаётся включённым;
- `RotatingFileHandler` ограничен настройками `CT_LOG_MAX_BYTES` /
  `CT_LOG_BACKUP_COUNT`, повторный setup не дублирует managed handlers и не
  конфликтует с pytest/caplog handlers;
- `connect`/`replace` пишут `attempt`, `success`, `recovered`, `rejected` с
  bounded/sanitized user/account identifiers и typed `code`/safe unexpected
  category; rejected
  имеет WARNING, остальные lifecycle events INFO;
- logging calls не получают wallet/API-wallet addresses, private key, token,
  signature, signed URL/query или raw exchange/exception payload; handler
  filter оставлен defense-in-depth и корректно обрабатывает `%`-formatted
  records;
- `httpx` и `httpcore` request logging подавлены до WARNING;
- Settings валидирует log level/rotation limits и `redacted_dump()` не
  раскрывает encryption key/Telegram token.

Evidence: focused logging/application suite и full offline
`pytest -q -m 'not testnet' --cov=src/copytradeaster` — **604 passed / 2
skipped / 17 deselected**, coverage **87.25%**; static gate и diff-check
запущены после этого журнала. Живой процесс, `.env`, `.botkey`, рабочая DB и
торговое состояние не менялись. Причина предыдущего live onboarding failure
этим изменением не восстанавливается и не переименовывается: аудит фиксирует
только безопасную текущую классификацию.

### Onboarding chat UX corrective gate — 2026-08-24

После owner feedback доведены card/message lifetime и expiry recovery без
доступа к живому процессу, `.env`, `.botkey`, рабочей SQLite или торговому состоянию:

- intro фиксирует exact Aster path `Aster testnet → Settings → Position mode →
  Hedge Mode`, prerequisites API wallet (`Perps` + read, withdrawal off),
  ordinary-wallet authorization и отсутствие позиций/ордеров;
- каждое из трёх credential-adjacent input-сообщений удаляется best-effort сразу
  после чтения, до validation/Aster call; outgoing text/FSM/logs не содержат
  values;
- valid transitions retire старую карточку и отправляют свежую Step 1/2/3;
  delete failure редуцируется к in-place edit без duplicate; после успешного
  delete `MenuSessionStore` и FSM больше не ссылаются на retired anchor до
  replacement send;
- Step 3 показывает clean loading, затем typed error/dashboard; replacement и
  retry сохраняют branch/revision semantics;
- expired input уже удалён, текущая карточка заменяется clean recovery view без
  соседнего `message.answer`, после чего FSM очищается.

Acceptance evidence: focused Telegram/onboarding/renderers — **136 passed**;
offline gate — **610 passed / 2 skipped / 17 deselected**, coverage **87.20%**;
`ruff check`, `ruff format --check` и `git diff --check` — clean. Живой процесс,
`.env`, `.botkey`, рабочая SQLite и торговое состояние не изменялись.

### Control audit boundary corrective implementation — 2026-08-24

Для доказуемости пользовательских state-changing действий добавлен bounded
`control_audit` слой: `attempt`/`success`/`rejected` с INFO/WARNING levels,
safe identifiers, whitelist state/category и changed field names. На public
application boundary покрыты notifications, risk, pause/resume, подтверждения
RESUME/EMERGENCY/DISCONNECT, admin access allow/set/decide, reconcile и
подтверждённые global pause/resume/emergency operations. Admin decision пишет
target только из безопасного returned `AccessRequestDTO`; APPROVED сообщает
`changed_fields=status,access_enabled`, REJECTED — только `status`, а duplicate
idempotent decision сохраняет state без `changed_fields`. Reconcile сообщает
`state=account`/`state=all` по наличию входного account ID и не пишет target ID
или address; request ID, confirmation tokens, risk values и raw exception
payloads не логируются. Logging failure defensive и не меняет бизнес-результат.

`result` в audit означает terminal outcome application call, а не доставку
Telegram-презентации. Если post-commit presentation failure произошёл после
изменения durable state, состояние отдельно читается из DB; rollback этим
контрактом не обещается. WP9b и общий Stage 2 по-прежнему открыты до ручной
owner acceptance.

Full offline gate: **615 passed / 2 skipped / 17 deselected**, coverage
**87.46%**; focused `tests/unit/application/test_service.py`: **75 passed**;
`ruff check`, `ruff format --check` и `git diff --check` — clean.

### Product-boundary and automatic-leverage correction — 2026-08-24

После сверки product boundary и отдельной signed testnet проверки Stage 2
зафиксированы следующие текущие факты:

- follower application boundary принимает для изменения только `risk_percent`;
  forged `max_leverage` и `max_entry_deviation_pct` получают `ValidationError`;
- coordinator production path читает максимум плеча для каждого account/symbol
  через signed `leverageBracket`, кэширует небольшой TTL и fail-closed при
  пустом/некорректном ответе; testnet с реальным follower вернул list и
  `initialLeverage=200`, поэтому исторический cap 125x не применяется;
- UI не предлагает редактировать leverage/deviation и не предлагает follower
  emergency; глобальный emergency остаётся admin-only. Dashboard не показывает
  ложный cap `20x`;
- owner вручную подтвердил notifications и изменение risk percent. Полный
  trading cycle и resume/copy flow остаются непроверенными;
- Stage 2 намеренно не меняет `marginType`. Isolated enforcement отложен до
  отдельной реальной проверки Aster testnet; evidence mock/fake не заменяет её.

Signed adapter evidence for connected testnet follower: `max_leverage(force=True)`
вернул BTCUSDT=200, ETHUSDT=200, SOLUSDT=100; `positionRisk` подтвердил
leverage `[200]`/`[200]`/`[100]`. Во всех трёх проверках margin остался cross,
nonzero positions=0 и open orders=0.

Focused regression evidence for this correction: **210 passed** across the
updated Aster endpoint, coordinator, application and Telegram boundary tests.

### Final Stage 2 state/readiness snapshot — 2026-08-24

Текущий кодовый срез: `b238407` в `codex/stage-2-telegram`. Root повторно
проверил product boundary, architecture diff, real signed leverage behavior и
запущенный runtime. Фактический итог подробно записан в
`reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md`.

Runtime после restart:

- `ready=true`, `degraded_active=false`;
- `allow_new_entries=false`, `emergency_active=true`;
- Telegram-follower enabled, `risk_percent=0.01`, per-account entries paused;
- in-flight execution commands = 0;
- последние `engine.startup`/`engine.ready` outbox events имеют `SENT`.

Четыре ранние `FAILED` startup/ready rows с `telegram_bad_request` остаются
историей первых запусков и не повторяются в актуальной доставке.

**Final Stage 2 Verdict: CLOSED (принят 24.08.2026).**

### Stage 2 live acceptance and notification formatting refactor — 2026-08-24

Проведён успешный живой приёмочный цикл WP9b на Aster Testnet с участием владельца:

1. **Рефакторинг форматирования уведомлений (`formatter.py`):**
   - Добавлен модуль `src/copytradeaster/notifications/formatter.py` с функциями `format_confirmed_action` и `format_skip_reason`.
   - Сырые системные логи заменены на чистые трейдерские карточки с форматированием чисел и разделителями тысяч:
     - 🟢 `Открыта позиция BTCUSDT (LONG)` / `Объём: 0.038`
     - 🛡 `Установлен защитный стоп BTCUSDT` / `Уровень стопа: 78 686.24 USDT`
     - 🎯 `Установлен тейк-профит BTCUSDT` / `Уровень тейка: 79 500 USDT`
     - 🔴 `Закрыта позиция BTCUSDT (LONG)` / `Объём: 0.038`
     - 🗑 `Снят ордер BTCUSDT`
     - ⏳ `Ожидание защитного стопа BTCUSDT (LONG).`
     - ⚠️ `Пропуск сделки BTCUSDT` с понятным описанием причины (`INSUFFICIENT_MARGIN`, `ENTRY_DEVIATION`, `STALE_MASTER` и др.).
   - Добавлены модульные тесты в `tests/unit/notifications/test_formatter.py`.

2. **Живой приёмочный прогон WP9b на Aster Testnet:**
   - Владелец подтвердил снятие паузы (`resume`) через Telegram-интерфейс бота.
   - Мастер открыл позицию `BTCUSDT LONG 0.002` со стопом `@78686.24`.
   - Движок скопировал сделку на подписчика `tg-8264892202`: рассчитал риск, выставил плечо 200x, открыл `0.038 BTC` и выставил биржевой `PLACE_STOP`.
   - Уведомления `🟢 Открыта позиция` и `🛡 Установлен защитный стоп` успешно доставлены в Telegram (статус `SENT`).
   - Мастер закрыл позицию; движок снял стоп (`CANCEL_ORDER`) и закрыл позицию подписчика (`MARKET_CLOSE 0.038 BTC`).
   - Уведомления `🗑 Снят ордер` и `🔴 Закрыта позиция` успешно доставлены в Telegram (статус `SENT`).
   - Прямой REST-аудит биржи подтвердил: `main`, `tg-8264892202`, `copyc1`–`copyc3` находятся в полном **FLAT** (0 позиций, 0 открытых ордеров).

3. **Финальные метрики:**
   - Офлайн-сьют: **632 passed / 2 skipped / 17 deselected** за 10.51 s;
   - Покрытие кода: **87.15%** (выше обязательного порога 87.0%);
   - Статический анализ: `ruff check` и `ruff format --check` — clean.

**[Снимок 2026-08-24]** Stage 2 официально CLOSED. Код готов к слиянию в main и переходу к Stage 3 (Builder / Aster Code).

## Pre-Mainnet focused live B/C/D/E — 2026-09-05 (REAL Aster Testnet)

Ветка `codex/live-copy-tests-hardening`. Все live-тесты сериализованы
оркестратором, каждый заканчивается flat/zero-open-orders audit, второй App
Testnet files 15→23: `test_pre_mainnet_unknown_live.py` (A),
`test_pre_mainnet_master_live.py` (B), `test_pre_mainnet_network_live.py` (C),
`test_pre_mainnet_risk_live.py` (D), `test_pre_mainnet_limit_live.py` (E2),
`test_pre_mainnet_isolation_live.py` (E2), `tests/testnet/test_pre_mainnet_load_live.py` (F),
`tests/testnet/test_pre_mainnet_hedge_live.py` (G).

- Package B (`test_pre_mainnet_master_live.py`, финальное дерево): S1
  rebuild-holds-queue 100s, S2 stale-cutoff 44s, W1 reconnect-gap 83s, W2
  failed-resnapshot 68s, P1 startup-transient 45s, REAL-06 App/WS/reconnect
  134s (run DB `data/pm-b-live-20260905T002000Z.sqlite3`) — все green.
  Harness: settle gate, `_wait_stable`, HEDGE nonzero-key counting,
  deterministic-COID revisions.
- Package C (`test_pre_mainnet_network_live.py`): 3/3 green за 12s
  (composition, mode-failure block, in-flight block). Harness: явный
  `testnet_ip_whitelist_override=False` kwarg (prod guard untouched).
- Package D (`test_pre_mainnet_risk_live.py`): 6/6 green за 386s (Dust-1
  master 0.008, Dust-2, Dust-3 SHORT-leg fix, Protection-cancel/naked,
  liquidation-hold). S1 livelock ~200x fixed entry gate; liquidation flatten
  unshapeable → hold contract + live evaluation evidence; TEMP probe
  добавлен+удалён. Offline hygiene: routing assertions 200→44, priceProtect
  удалён + 2 pins, 75 passed.
- Package E2 (`test_pre_mainnet_limit_live.py`): 2 passed (L1, L3) + L2 honest
  skip (exchange filled full дважды); (`test_pre_mainnet_isolation_live.py`):
  первый прогон 0/3 (harness: naked master, engine fail-closed) → fixed,
  rerun IN FLIGHT — PENDING RERUN, НЕ green.

## Pre-Mainnet E2 isolation close — 2026-09-05 (REAL Aster Testnet)

- **Package E2 isolation live (`tests/testnet/test_pre_mainnet_isolation_live.py`):**
  **3/3 LIVE-COMPLETE за 633s** на финальном дереве (F1 429-transport error injection,
  F2 permission error −2015 + degraded-доказательство, F3 control-plane-denial).
  Harness-фикс: master STOPs добавлены, engine корректно fail-closed.
  Аудит в `finally`: flat/zero-open-orders на всех аккаунтах.
  Package E официально CLOSED. Замеренный launch cap v1 = 10 followers.

## Pre-Mainnet Package F hybrid load close — 2026-09-05 (REAL Aster Testnet)

- **Package F hybrid load (`tests/testnet/test_pre_mainnet_load_live.py`):**
  **1 passed за 3:39:15** (runner `loadgate8`, `CT_LOAD_SCALE=1.0`, 100 циклов).
  Конфигурация: 3 REAL followers + 10 SHADOW slots (rescope со 100: замер load12
  показал свипы 17–37s при бюджете 40/s, потолок ≈28 → cap 10 с запасом).
  Сценарий: restart, SIGTERM, SIGKILL, фазы a/b/c/d.
  Phantom-fill 65000 устранён синком REST-marks (`App._sync_shadow_marks`).
  Метрики: p50/p95/p99 latency собраны. Journal без незакрытых записей,
  ноль дублирующихся мутаций. Финальный аудит: flat/order-free везде.
  Package F официально CLOSED.

## Pre-Mainnet Package G HEDGE live close — 2026-09-05 (REAL Aster Testnet)

- **Package G HEDGE dual-leg (`tests/testnet/test_pre_mainnet_hedge_live.py`):**
  **7/7 green**:
  - G0: pre-check (dual-side mode, нулевые остатки);
  - G1: LONG + SHORT одновременное открытие;
  - G2: асимметрия размеров ног;
  - G3: независимая перестановка стопов (stop replace);
  - G4: изменение плеча мастера не дёргает противоположные ноги;
  - G5: частичное закрытие (partial close);
  - G6: полное закрытие одной ноги без влияния на вторую.
- **Баг #27:** вскрыт на живом тестнете — биржа вернула мусорный `liquidationPrice=25.5M`
  на LONG-ноге (выше цены входа), что приводило к ложному срабатыванию инварианта ликвидации.
  Исправлено: wrong-side guard в `Coordinator._liquidation_violated` (игнорирует нефизичные
  значения цен ликвидации на неверной стороне).
- **Снятие ограничения:** ONE_WAY-only boot refusal СНЯТ из `src/copytradeaster/app.py`.
  Финальный аудит: FLAT-ALL. Package G официально CLOSED.

## Финальная офлайн-валидация и вердикт v1 — 2026-09-05

- **Offline test suite:** **1068 passed** (1 известный flaky: whitelist regex, тест-онли);
  frontend: 168/17; линтеры `ruff check` и `ruff format --check` — clean.
- **ВЕРДИКТ v1 (ONE_WAY + HEDGE): GO.** Launch cap v1 = 10 followers (по замеру).
  Все blocker findings закрыты кодом, тестами и записанным evidence на REAL Aster Testnet.
  Финальное решение о выходе в mainnet остаётся за owner'ом проекта.

---
> Это дно append-only журнала (начало — 2026-08-22). Текущее состояние — только TL;DR сверху файла; всё ниже него — снимки момента.

## 2026-09-06 — функциональная кампания, S1.1 read adapter (IN PROGRESS)

Добавлен `tests/unit/aster/test_history_api.py`: 19 checks на точные значения,
большие IDs, weights/параметры signed boundary, отказ от некорректных окон,
целостность страниц и безопасные ошибки. `PYTHONPATH=src .venv/bin/pytest -q
 tests/unit/aster` — **89 passed**. Это targeted gate, прежние full-suite
счётчики не пересчитаны. S1.1 live evidence и G1 ещё открыты.
Первый запуск без PYTHONPATH импортировал другой editable checkout и не является
проверкой этой ветки; приведённый результат получен с явным `PYTHONPATH=src`.


## 2026-09-06 — history reader и signed read probe

`PYTHONPATH=src .venv/bin/pytest -q -m 'not testnet'`: **1115 passed,
2 skipped, 44 deselected**, 14.98 s. Targeted aster/history: **95 passed**.
Новый `tests/unit/history/test_reader.py`: 6 сценариев split/budget/gap/bounds/error.
`PYTHONPATH=src CT_TESTNET=1 .venv/bin/pytest -q -s tests/testnet/test_history_reads_live.py`:
**1 passed**, 3.07 s; read-only copyc1, tmp nonce DB, ни одного trading write.
Окно: 9 fills, 12 income, 8 matched commissions; commission sign противоположен
income, 9 положительных commissions. fromId inclusive подтверждён; BTC/USDT/USDT
из metadata подтверждены. Funding/Builder/full-history completeness не приняты.
Полный testnet suite не перезапускался. Ruff changed files и doc links clean.


## 2026-09-06 — S1.2 history storage и import step

Append-only миграции history facts/observations/coverage; repository и bounded
sync step. Новый `tests/unit/history/test_storage.py`: 8 сценариев idempotency,
exact Decimal, scope isolation, conflict rollback, reconnect/pending resume,
income identity, invalid coverage, observations и read failure.
Full offline: **1123 passed / 2 skipped / 44 deselected**, 15.21 s.
После замены per-row SQL на batch lookup/insert: targeted history **14 passed**.
Ruff для изменённых файлов clean. S1.2 ещё открыт: worker lifecycle/hooks,
retention/capacity и episodes/events не приняты.


## 2026-09-06 — S1.2 executor observations

Production App/Executor подключены к записи order observations, включая
SUBMITTED/UNKNOWN query-before-retry и boot recovery. Пять новых checks в
`test_executor_journal.py`: identity/data callback, observer failure/no resend,
boot, SUBMITTED и UNKNOWN. Full offline: **1128 passed / 2 skipped /
44 deselected**, 16.61 s (`PYTHONPATH=src:.`). Ruff changed files/doc links clean.
Worker recovery пропущенных observations и runtime history sync ещё впереди;
этот gate не доказывает полноту истории после сбоя записи.


## 2026-09-06 — history background lifecycle

Full offline после worker wiring: **1131 passed / 2 skipped / 44 deselected**,
15.40 s. Затем добавлена lifecycle-проверка history.stop до закрытия БД;
targeted application/history **31 passed**. Full total после этого добавления
не пересчитывался. Worker tests: rotation/request bound, failure isolation/backoff,
in-flight cancellation/idempotent start/stop. Ruff changed paths clean.
G1 ещё открыт: latency/retention/repair и финансовая проекция не приняты.


## 2026-09-06 — observation repair и coverage compaction

Full offline **1135 passed / 2 skipped / 44 deselected**, 15.68 s.
Targeted history **20 passed**. Новые проверки: восстановление observation из
CONFIRMED journal, отсутствие ордера не меняет command status, retry cooldown,
слияние overlapping coverage сохраняет настоящий gap. Миграция 40 хранит repair
checkpoints; worker ремонтирует observation отдельным bounded job.
Synthetic benchmark: 100 accounts, 100k fills + 100k cashflows + 50k observations:
DB/backup 65,880,064 bytes, WAL до checkpoint 4,358,992 bytes; 1000-fill commit
p50 5.49 ms, max 7.67 ms. Это текущая схема без будущих episodes/events и без
параллельного trading workload; не окончательный G1 capacity/latency gate.


## 2026-09-06 — S1.3 чистая follower episode projection

Новый `tests/unit/history/test_projector.py`: 11 cases (increase/partial/close,
flip attribution, HEDGE, unknown/known seed, dedup, contradictory facts, source).
Targeted history **31 passed**; full offline **1146 passed / 2 skipped /
44 deselected**, 15.97 s. Ruff changed files/doc links clean. Это математическое
ядро; storage/snapshot proof/aliases/net/events ещё не подключены и G1 не закрыт.


## 2026-09-06 — episode cash accounting core

Новый `test_accounting.py`: 7 checks на signed commissions/rebates, dedup,
missing costs, funding attribution, foreign asset, shared flip fee и separate
Builder mechanism (синтетический тип, не exchange evidence). History targeted:
**38 passed**; full offline: **1153 passed / 2 skipped / 44 deselected**, 16.11 s.
Ruff changed files/doc links clean. Exact net включается только explicit proof;
реальный Builder policy/flip allocation/persistence ещё не приняты.


## 2026-09-06 — episode persistence / refs

Миграции 41–44: refs/episodes/owned index/aliases. `test_episodes.py` — 5 checks
restart/idempotency/revision CAS/ownership/alias merge. Shared history fixture
вынесена в conftest. Full offline **1158 passed / 2 skipped / 44 deselected**,
16.38 s; после canonical reserve-ref correction targeted **5 passed**.
History total перед последней correction **43 passed**. API/worker projection
wiring не включены; G1 открыт. Ruff changed files и doc links clean.


## 2026-09-06 — projection publication / reconciliation

Новый test_reconcile (4 cases), atomic batch rollback и durable-window integration
в test_episodes (+2). Targeted history **49 passed**; full offline **1164 passed /
2 skipped / 44 deselected**, 15.76 s. Ruff changed files/doc links clean.
Runtime boundary snapshots/worker projection activation ещё не реализованы;
эта приёмка доказывает storage→projector→reconciliation→atomic publish seam.


## 2026-09-06 — snapshot boundary checks

PositionDTO.updateTime сохранён как optional StrictInt; signed history probe:
**1 passed**, 3.65 s, updateTime есть у обеих HEDGE-сторон и ненулевой.
Новый test_snapshot: 6 cases на стабильность qty/entry/updateTime, bracketed
cutoff, missing time, seed inference и dedup. Targeted history **55 passed**;
full offline **1170 passed / 2 skipped / 44 deselected**, 15.94 s.
Ruff/doc links clean. Полный signed suite не повторялся; runtime capture machine
и long-lived incremental checkpoint ещё не реализованы.


## 2026-09-06 — serial boundary capture seam

Новый `history/capture.py`: `read_boundary` (один авторитетный `positionRisk`
snapshot с clock-интервалом), `HistoryCapture` с per-key `asyncio.Lock`,
`prove_unchanged` (margin 2000 ms), seeds обратным суммированием из доказанной
ending-границы, стабильные placeholder refs (`seed:{window.start}:{leg}`),
публикация через `HistoryProjection`; `capture_and_publish` выполняет сначала
bounded import, затем доказывает live edge. Отложенная публикация —
`HistoryReconciliationError` без потери durable fills, не повод для backoff.
`App.episodes` и `build_history_capture` подключены. Новый
`tests/unit/history/test_capture.py`: 12 checks. Targeted history
**67 passed**; full offline **1182 passed / 2 skipped / 44 deselected**,
17.56 s. Ruff changed files clean. Периодическая worker-активация, durable
capture checkpoints, user events и shared-fill allocation остаются открытыми;
G1 не закрыт.


## 2026-09-06 — хранилище пользовательских событий S1.3

Append-only миграции 45–47: `user_activity_events` (seq, UNIQUE event_ref,
account/environment ownership, nullable trade_ref, 13 kinds из §7.1,
occurred/recorded_at, canonical JSON facts, per-account dedup key, notify
eligibility) + индексы feed/trade. Новый `storage/events.py`:
idempotent `record` (повтор по dedup key возвращает первую запись, события
неизменяемы), owned `get`/`list_owned`/`list_by_trade`, стабильный seq-курсор
(поздний backfill не сдвигает страницы), facts только str/int/bool/null,
bounded limit. Новый `tests/unit/history/test_events.py`: 10 checks. Targeted
history **77 passed**; full offline **1192 passed / 2 skipped / 44 deselected**,
17.98 s. Ruff changed files clean. Вывод событий из фактов, worker-активация
capture и shared-fill allocation остаются открытыми; G1 не закрыт.


## 2026-09-06 — вывод событий из переходов эпизодов S1.3

Новый pure `history/event_builder.py`: `episode_events` сравнивает ранее
опубликованные эпизоды со свежей проекцией — OPENED/INCREASED/REDUCED/CLOSED
только за новые подтверждённые факты (цены строго из fills, недостающий fill
абрагирует пачку; связь `unknown` не приписывается; placeholder молчит до
первой entry-аллокации). Повторная проекция даёт ноль черновиков; dedup key
стабилен под first-wins семантику репозитория; `notify_eligible` — политика
caller. Новый `tests/unit/history/test_event_builder.py`: 11 checks. Targeted
history **88 passed**; full offline **1203 passed / 2 skipped / 44 deselected**,
19.10 s. Ruff changed files clean. Проводка в транзакцию публикации,
protection/system-события, worker-активация и shared-fill allocation остаются
открытыми; G1 не закрыт.


## 2026-09-06 — атомарные события публикации S1.3

Требование §10.6 «проекция и события — одной транзакцией»: `EventDraft`
переехал в `storage/events.py` + `insert_many` на чужом соединении;
`episodes.publish(..., events=)` пишет эпизоды и события атомарно (конфликт
revision и невалидный черновик откатывают всё). `HistoryProjection`
выводит черновики из diff со старой публикацией и требует `recorded_at`;
`HistoryCapture`/`App.user_events`/`build_history_capture` проводят политику
сквозно, `capture_and_publish` штампует временем after-снапшота. Без репо
поведение прежнее. Новые `test_episode_events.py` (6) + 2 capture-теста +
`recorded_at` в builder-тестах (12). Targeted history **97 passed**; full
offline **1212 passed / 2 skipped / 44 deselected**, 17.45 s. Ruff clean.
Остаются worker-активация, protection/system-события, shared-fill allocation;
G1 не закрыт.

## 2026-09-06 — worker-активация capture S1.3

Периодическая публикация истории: миграция 48 `history_capture_state`
(cap_start/cap_end per account/environment/symbol, secret-safe last_error);
`HistoryRepository.capture_state/record_capture` — dumb put, политика выше.
`prepare_window` строит стабильное окно из durable состояния без биржевых
чтений (пропуски: нет покрытия / фронтир опубликован / фронтир свежий;
старт пинится к checkpoint, спан > 7 дней клампится). `require_settled=True`
отказывает окна поверх открытой позиции — steady-state только settled.
`run_scheduled`: prepare → capture_and_publish → checkpoint; пропуск — False
без чтений, deferral пишет ошибку-категорию и пробрасывается в worker-backoff;
края моложе 5 мин eligible, backfill silent. `HistoryWorker`: capture-jobs в
ротации отдельно от fills, `mark_dirty` приоритизирует symbol (best-effort),
`App._observe_history_order` дергает hint через `history_mark_dirty`,
`build_history_worker` отдаёт fills+capture jobs. Тесты: storage +1, capture
+10 (prepare/settled/run_scheduled), worker +5, App hook +2. Targeted history
**113 passed**; full offline **1230 passed / 2 skipped / 44 deselected**,
17.17 s. Ruff changed files clean. Остаются protection/system-события,
shared-fill allocation; G1 не закрыт.

## 2026-09-06 — order-link источники и protection-события S1.3

`HistoryRepository.proven_order_sources`: order id — `copy` только при согласии
observation + CONFIRMED entry/exit-команды + link с master trade id; проекция
резолвит сама, частичная связь остаётся `unknown`. Новый `history/protection.py`:
baseline и факты окна только из CONFIRMED-команд, цены из journal payload,
чистая свёртка SET/CHANGED/UNCONFIRMED в транзакции публикации; замена — одно
событие, непривязываемый cancel молчит, trade_ref пуст. `CommandRepo.
by_client_order_id` для связи cancel→заменённый ордер. `account_episodes`:
batch pro-rata split shared-fill комиссий с точным сохранением суммы;
single-view без сиблингов keeps `shared_fill_commission`. System-события
(риск/пауза/связность/пропуск) из журнала не выводятся — источник
application-слой, S1.5. Тесты: accounting +3, storage +4, protection +9,
capture +3. Targeted history **132 passed**; full offline **1249 passed /
2 skipped / 44 deselected**, 19.52 s. Ruff всего репо clean. Остаются S1.4–S1.6;
G1 не закрыт.

## 2026-09-06 — S1.4 контракты истории (list/detail/feed)

`EpisodeRepository.list_closed/list_open` (курсор `closed_at:ref`), `max_seq`,
`in_flight_for`, `coverage_from`. DTO (`TradeListItem/Page`, `TradeDetailDTO`
с `gross_only`, `ActivityFeedPage`, `AccountOverview` без суммарного
risk_to_stop, `TradeUnavailableError`) и методы в `ApplicationProtocol`.
Новый `application/trade_views.py`: чистые презентеры поверх доказанных
фактов. Service: репозитории из `runtime.db`, `trades`/`trade`/`trade_events`/
`activity_feed` с ownership, pending-блок без payload, аддитивный `overview`
в DashboardDTO, refresh-snapshot singleflight. Адаптер `symbol_assets`
(baseAsset, soft-null). HTTP: `trades`/`trade`/`trade/events`/`activity/feed`
(стало 25 POST). Тесты: trade_views +9, service +12, HTTP +9. Full offline
**1279 passed / 2 skipped / 44 deselected**, 18.33 s. Ruff всего репо clean.
Остаются остаток S1.4 (risk preview, resume-контекст), S1.5–S1.6; G1 не закрыт.

Примечание (2026-09-10): «стало 25 POST» выше — исторический снимок на момент записи; актуально 27 POST (`grep -c 'add_post("/miniapp/api/' src/copytradeaster/miniapp/server.py` → 27). Историю не переписываем.

## 2026-09-06 — S1.4 остаток: risk preview и resume-контекст

`RiskPreviewStep/DTO` + чистый `risk_budget_steps` (10 шагов 0,5–5%, точный
`available × percent`); `service.risk_preview` поверх cached snapshot без
side effects, пустые steps вместо нулевого примера при недоступном балансе;
route `risk/preview` (стало 26 POST). `ResumeContextDTO` + аддитивное
`Confirmation.resume_context` только для RESUME, факты cached-only, unknown
вместо отказа begin. Замена hardcoded копии resume в UI — S3; TS-зеркала —
с экранами. Тесты: service +8, презентер +5, HTTP +1. Full offline
**1293 passed / 2 skipped / 44 deselected**, 19.96 s. Ruff + format clean.
S1.4 закрыт по backend-объёму; остаются S1.5–S1.6; G1 не закрыт.

## 2026-09-06 — S1.5 presenter, structured outbox, recovery из событий

`notifications/trade_cards.py`: общий pure-презентер всех 13 видов событий
по примерам §7 (нейтральное закрытие, итог «до комиссий», unknown опускается,
unknown kind — ValueError). Миграции 49–50: outbox `trade_ref`/`action` без
backfill; `NotificationEvent` closed-set action; router passthrough; worker
`trade_keyboard` dormant без base URL + `webapp_base_url` из настроек.
`recover_user_events` (eligible после floor, backfill молчит) поверх legacy
на старте. Service пишет RISK_CHANGED/COPY_PAUSED/COPY_RESUMED поверх ленты
best-effort. Push-активация и menu — S3. Тесты: презентер +7, recovery +3,
keyboard +2, writers +3. Full offline **1308 passed / 2 skipped /
44 deselected**, 19.18 s. Ruff + format clean. Остаются S1.6 и G1.

## 2026-09-06 — S1.6 offline-приёмка, ремонт миграций, retention

Cross-surface acceptance (§10.9.5/6) offline: один trade_ref и одинаковые
факты на списке/деталях/ленте/recovery; рестарт-идемпотентность; изоляция
§10.9.7 покрыта. Найден и отремонтирован блокер рестарта: mid-list вставка
схемы (36c55d3) ломала апгрейд pre-S1 БД (ghost-v33 пропускал CREATE TABLE
fills) — entry 34 стал table-ensure, индекс переехал в 51, S1-DDL закалена
IF NOT EXISTS; копия prod-БД мигрируется, fresh/upgraded конвергируют;
регрессия `test_database_upgrade.py`. Retention: facts без TTL, outbox
`prune_terminal` (SENT 7d / FAILED 30d, bounded, на старте worker).
Тесты: acceptance +2, upgrade +2, prune +2. Full offline **1316 passed /
2 skipped / 45 deselected**, ~19 s. Ruff + format clean. Live §10.9-цикл
написан (`test_history_acceptance_live.py`), не запущен — ждёт ревью; G1
не закрыт.

## 2026-09-06 — Шаг 22: пред-ревью аудит S1, 10 must-fix влиты

Шесть ревьюеров: шаги 12–15 CONFORM; P0 — live-edge capture (stale cutoff
делал bracket невыполнимым всегда; фикс: импорт до живого края,
cutoff=min(frontier, after−margin), одно ожидание margin, checkpoint и
eligibility по эффективному концу; инвариант плана не тронут); S1.4 —
курсор round-trip, preview отключённым, whitelist в advisory,
executed_qty None≠"0"; S1.5 — recovery листает всю ленту (гард курсора,
регрессия 204+1; найден висяк на стабе без after_seq); protection —
cancel-гейт требует CONFIRMED; acceptance — полный цикл + batching-правило;
FAILED-retention 10d/40d; live-файл hardened offline (order_id-фильтр,
single-count комиссий, foreign spot-check, notify_eligible=True).
Отклонено с обоснованием: seq-сортировка ленты (§11.2 — правится план),
fold верит CONFIRMED-команде, а не статусу наблюдения. Full offline
**1322 passed / 2 skipped / 45 deselected**. Ruff + format clean. Live
§10.9 (журнал, рестарт, SL/UNKNOWN, прогон) — только с ревью; G1 не закрыт.

## 2026-09-06 — Шаг 23: независимое ревью S1, REQUEST CHANGES

Scope `4f1a86a..f77a3fd`. Повторная проверка именно текущего checkout:
`PYTHONPATH=src:. .venv/bin/pytest -q -m 'not testnet'` — **1322 passed /
2 skipped / 45 deselected**, 18.76 s. `.venv/bin/ruff check .` и
`.venv/bin/ruff format --check .` — clean. Coverage не перемерялось;
frontend не менялся и повторно не запускался. Новых постоянных тестов нет.

Дополнительные hermetic probes (реальные repositories/service/capture,
временная SQLite; только notification emitter заменён recorder) подтвердили:
лишний fill чужого эпизода в trade_events; повторный closed episode с тем же
gross после сдвига start; потерю increase/partial и неверные промежуточные
числа при batch events; смешение HEDGE protection; потерю 1001-го eligible
event после продвижения recovery floor. Отдельно подтверждены отсутствующие
costs/issues в DTO, активный markup при обычном URL и tuple-контракт list_closed,
который нарушает live harness.

**S1/G1 — REQUEST CHANGES.** Открыты R1–R9 и остаток capacity/retention /
publication latency. Отчёт с файлами, воспроизведениями и критериями исправления:
[MINIAPP_S1_REVIEW_2026-09.md](reports/MINIAPP_S1_REVIEW_2026-09.md).
Ранее записанные CONFORM и «дальше только live» — результаты прежнего прохода,
не текущий verdict. Live не запускался; harness нужно исправить до signed
прогона. Рабочая БД и биржа этим review не затрагивались. G2/G3 также открыты.

## 2026-09-07 — C1–C9 исполнены; live нашёл делимый аккаунт + баг харнеса

C1–C4 (границы/аллокации/transitions/protection+хук), C5–C7 (деньги/delivery/
feed), C8 (worker budget, maintenance tick, бенчмарк 30 акков: publish p95
552ms, прогноз 365d 1.56GB), C9 (харнес на Executor/journal, HTTP, рестарт).
Offline **1428/2/45**, ruff/format clean, миграции 0..57.
Live `CT_TESTNET=1` FAIL за 96s: (1) copyc2 торгует чужой бот (fills
`ct+32hex` в нашем окне) — эксклюзивность из §23.10 не проверена, повтор
только в эксклюзивном окне; (2) одинаковые payloads open-a/open-b делили
journal key — open-b был тихим no-op (ядро право, харнес починен:
per-step operation_id + journal-coid↔биржа после каждого submit).
Аккаунт оставлен flat. G1 открыт.

## 2026-09-07 — повторное ревью C1–C9: G1 OPEN

На `9a0dd98` повторён полный offline gate:
`PYTHONPATH=src:. .venv/bin/pytest -q -m 'not testnet'` — **1429 passed /
2 skipped / 45 deselected**, 22.03 s. `ruff check .` и `ruff format --check .`
чистые. Новых suite-тестов в рамках review не добавлено; миграции 0..57.

Отдельные probes на временных SQLite подтвердили N1–N5 (scheduled bootstrap,
continuation/identity, потеря durable dirty range, events >200, default recovery
>1000). N6 установлен по коду harness/Executor: STOP cancel направлен на label,
а не фактический journal-generated clientOrderId; CONFIRMED допускает `-2011`.
Подробности и точный порядок исправлений:
[MINIAPP_S1_REVIEW_2026-09.md](reports/MINIAPP_S1_REVIEW_2026-09.md).

Записанный live PASS 26.56 s сохранён как evidence конкретного запуска, без
нового signed прогона в этом review. Он не проверяет scheduled continuation,
реальный SL fill и успешность отмены нужного STOP. Прямой capture benchmark
также не доказывает publication latency рабочего scheduler.
**S1/G1 OPEN / REQUEST CHANGES**; R4/R5/R7/R9 приняты в исходном scope,
остальные findings требуют указанной доработки. Рабочая БД/биржа не затронуты.

## S3 toggle delivery boundary — 2026-09-08

После implementation commit `903d4f4` исправлен найденный чтением кода race
mute/re-enable между sweep. Toggle/outbox cursor boundary транзакционный,
recipient/ownership повторно проверяются при commit, cursor монотонный.
Тестовые прогоны отложены владельцем; ruff check/format изменённых файлов
проходят. Дополнительные сценарии внесены в S3 testing handoff; G3 OPEN.

## Mini App Live Mode checkpoint — 2026-09-10 (снимок момента)

> Финальный статус: **CLOSED** — ручная приёмка владельца (iPhone, настоящий
> Telegram WebView) выполнена, живая кампания открытия/закрытия позиций прошла,
> финальный аудит `FLAT-ALL`. Детали — в хвосте
> `docs/reports/MINIAPP_LIVE_MODE_PLAN_2026-09.md`; счётчики ниже — снимок момента.

В ветке `codex/miniapp-live-updates` реализованы L1–L6 live presentation mode:
process-local follower snapshots, authenticated single-use ticket/WebSocket,
private follower-stream hints с coalesced authoritative REST refresh, shared
public mark-price stream, frontend reconnect/stale/auth lifecycle и shell-level
LiveStatus. Постоянные generic refresh-кнопки убраны из healthy пользовательских
surface; context recovery и security permission recheck сохранены. Execution
correctness по-прежнему остаётся за REST confirmation и reconciler.

Lifecycle/backpressure hardening этого среза: server application heartbeat 10s,
visible-client timeout 30s, hidden WebView без false-stale timeout, duplicate/
out-of-order revision fence, manual-retry generation fence, ordered `hello`
baseline и full live-resource reconciliation при revision gap/reconnect gap.
Dedicated live tests покрывают ticket TTL/single-use, bounded account fan-out,
Decimal mark-price/REST-age, private-stream burst coalescing и frontend
heartbeat/auth/reconnect/revision-gap paths.

Финальный corrective перед acceptance: per-process `epoch` в live hub + клиентский
re-baseline (иначе рестарт backend навсегда залипал в `stale` на более низких
revision), frame-gated liveness для shared mark-price стрима с bounded unsigned
REST `premiumIndex` fallback (на testnet-кластере mark-стримы принимают
подписку и не отдают кадров, тогда как `aggTrade` стримит), и классификация
неразмеченного серверного сбоя как retryable `service_unavailable` вместо утечки
имени класса исключения в public code.

Frontend full gate на этом срезе: **30 files / 367 passed**, Oxlint **0 warnings /
0 errors**, `tsc -b` и Vite production build PASS. Backend full offline gate
после финальных protocol-hardening изменений: **1540 passed / 2 skipped /
87 deselected**. `ruff check`/format для изменённого live Python surface и
`git diff --check` проходят. Полный `ruff check .` в этой ветке отдельно упирается
в уже существующие lint-ошибки `scripts/check_doc_facts.py` и не считается
live-regression. REAL Aster Testnet и ручной Telegram Mini App acceptance
выполнены (LIVE rail, auto-recovery после periodic boundary, stale-retry цикл,
position без manual refresh, живой PnL, `FLAT-ALL` после закрытия, 53 tab
transitions без роста sockets, 0 orphan tasks): live mode объявлен
**ACCEPTED** в объёме §6 live-плана.
