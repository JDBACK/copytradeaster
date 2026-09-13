# Карта документации

Что где лежит, что из этого живое, а что история, и что читать в каком порядке.
Обновляется при перемещении/добавлении документов. Последняя перестройка: 2026-09-13 (аудит документации и гайд ревьюеру по изменениям движка).

## Быстрый старт (что читать в новом сеансе)

Начать с корневого и применимого вложенного `AGENTS.md`. Остальное читать
по задаче: для UI/product — 2 и 7; для состояния/приёмки движка — 3 (сначала
TL;DR); для этапов — 5; для кода — 10 и затронутые отчёты. Полное чтение всех
документов для небольшой правки не требуется.

Handoff и сохранённые промпты — контекст своей кампании, а не автоматическая
задача нового сеанса. Перед продолжением сверить их с текущим запросом,
рабочим деревом и последними записями журнала; не повторять закрытые пакеты
только из-за старого указания «начать заново».

1. `AGENTS.md` (корень) — правила работы с репо и структура папок.
2. `PRODUCT.md` (корень) — durable product truth: пользователи, задачи,
   Telegram/Mini App boundaries и принципы интерфейса.
3. `docs/ENGINE_TEST_STATUS_2026-08.md` — текущее состояние движка: счётчики suite,
   список исправленных багов #1–27, кампании и актуальный acceptance verdict.
4. `docs/reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` — исторический
   фактический snapshot Stage 2 (2026-08-24); текущие engine scope, workflow и
   acceptance брать из пунктов 3, 5 и 10.
5. `docs/DEV_PLAN_SKELETON.md` — roadmap этапов и чеклисты; что уже сделано, что дальше.
6. `docs/reports/STAGE3_PART2_PROGRESS_2026-09.md` — журнал исполнения
   Stage 3 Part 2: P2.0–P2.7 + post-closure corrective, все CLOSED (2026-09-04).
   Текущая правда по Part 2 (исходный план — только в таблице reports ниже).
7. `docs/reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md` — центральная карта
   фактических UI-проверок Mini App в Telegram WebView, Chromium/CDP и frontend;
   живая презентация (streams, rail, pull-to-refresh) — `MINIAPP_LIVE_MODE_PLAN_2026-09.md`.
8. `docs/reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md` — design-only источник:
   IA, восемь Aster-style screen-эталонов (один Operate-концепт) и исходные screen states.
9. `docs/reports/STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` —
   закрытый corrective gate Part 1 и фактическое acceptance evidence.
10. `docs/ARCHITECTURE.md` — архитектура ядра и Stage 2 process; §20 —
   live presentation Mini App (streams, hub, rail, ручное обновление).
11. `docs/reports/STAGE2_IMPLEMENTATION_PLAN_2026-08.md` — исходный полный
  план Telegram/direct-V3 этапа и append-only журнал исполнения. Его старые
  sections/checklists — история планирования; текущую правду брать из пункта 4.
12. `docs/reports/STAGE2_ACCESS_APPROVAL_2026-08.md` — фактический журнал
   guest captcha, durable access request, env-admin approval и regression gate.
13. `docs/reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md` — выполненный
    corrective gate после независимого аудита: план, полный журнал и evidence.
14. `docs/reports/plan_live_test.md` — исторический owner-provided pre-mainnet
    hardening plan с критериями и статусами Package A–G; текущий verdict — в
    `ENGINE_TEST_STATUS_2026-08.md`.
15. `docs/reports/PRE_MAINNET_HARDENING_HANDOFF_2026-09.md` — исторический
    handoff кампании от 2026-09-04; его «начать заново» инструкции закрыты, а
    факт исполнения сохранён в `PRE_MAINNET_HARDENING_PROGRESS_2026-09.md`.

16. `docs/reports/ENGINE_CORRECTIVE_TEST_PLAN_2026-09.md` — исторический
    append-only corrective handoff hardening-кампании. Для текущего дерева и
    migration #62 фактический verdict брать из верхнего integrated TL;DR в
    `ENGINE_TEST_STATUS_2026-08.md`; старый GO A–G не переносится автоматически.

Всё остальное — справочники и история: грузить по необходимости, не по умолчанию.

## Живые документы

| Файл | Что это | Когда читать/менять |
|---|---|---|
| `PRODUCT.md` (корень) | Пользователи, purpose, позиционирование, operating context и durable product principles | Читать перед проектированием пользовательских поверхностей; менять только при изменении product truth |
| `docs/ARCHITECTURE.md` | Архитектура системы: стили, модули, planner/reconciliation, journal, конкурентность, recovery, этапы; §20 — live presentation Mini App | Читать затронутые разделы перед работой с кодом. **Не перемещать**: докстринги `src/copytradeaster/` ссылаются на её секции по номерам (`ARCHITECTURE.md §6/§7/§12`). Менять при изменении дизайна |
| `docs/DEV_PLAN_SKELETON.md` | Roadmap и факт трёх этапов: sections 1–8 — исторические чеклисты, §9 — текущий engine disposition, §10 — принятый Mini App Live Mode | Читать для понимания границ и следующего шага; обновлять только живые секции по факту |
| `docs/reports/plan_live_test.md` | Исторический owner-provided pre-mainnet hardening plan; критерии и append-only статусы Packages A–G | Читать как источник критериев и provenance; текущий статус брать из `ENGINE_TEST_STATUS_2026-08.md` |
| `docs/ENGINE_TEST_STATUS_2026-08.md` | **Единая точка истины** о текущем состоянии: актуальные счётчики suite, таблица багов #1–27, разделы тестовых кампаний | Обновлять после каждого значимого изменения кода/тестов. Не дублировать его счётчики в других файлах |
| `docs/ASTER_API_RESEARCH_2026-08.md` | Фактологический справочник по Aster V3 API: подписи, ордера, WS, лимиты, режимы позиций | Читать при работе с `src/copytradeaster/aster/` или новыми API-вызовами |
| `docs/EXTERNAL_FOLLOWER_CLOSE_POLICY.md` | Реализованная durable-политика ручного/внешнего закрытия follower при активном master + offline/Testnet evidence | Читать перед изменением Coordinator/reconciliation и обновлять при изменении этого правила |
| `docs/reports/MARGIN_MODE_MIRROR_2026-09.md` | План и Testnet-журнал зеркалирования `CROSSED`/`ISOLATED` с двойным isolated-залогом до Stop | Читать при изменении режима маржи, плеча или соответствующей live-приёмки |
| `docs/reports/ENGINE_MARGIN_AND_EXTERNAL_CLOSE_REVIEW_GUIDE_2026-09.md` | Гайд ревьюеру по двум изменениям движка: diff-map, инварианты, signed Testnet evidence и границы проверки | Перед code review этих изменений; не подменяет живые журналы и единый статус |

## docs/reports/ — отчёты кампаний и corrective-планы

Отчёты фиксируют состояние на момент своей кампании: задним числом их не
переписывать, дополнения — только новыми секциями в конце. Выполненные планы
содержат фактический журнал/evidence acceptance; текущий открытый gate помечен
своим статусом в таблице.

| Файл | Кампания |
|---|---|
| `MINIAPP_FUNCTIONAL_PROGRESS_2026-09.md` | Общий append-only журнал S1–S3; итоговые G1/G2/G3 закрыты к 2026-09-09, детали финального S3 — в отдельном журнале ниже |
| `MINIAPP_S2_PROGRESS_2026-09.md` | **S2/G2 ACCEPTED:** session resources, Settings prefetch, mutation fencing, lifecycle и performance evidence |
| `MINIAPP_S3_PROGRESS_2026-09.md` | **S3/G3 CLOSED в согласованном scope:** реализация, Chromium §19.7, Telegram Desktop, iPhone Mirroring, history/error matrix и signed N6 evidence |
| `MINIAPP_S3_TESTING_HANDOFF_2026-09.md` | Выполненная передача S3 на проверку; в конце зафиксирован CLOSED verdict и фактический scope |
| `REFACTOR_STAGE1_REPORT.md` | Базовый рефакторинг Stage 1 + инвентаризация первого ревью (2026-08-22) |
| `FINAL_STAGE1_FOLLOWUP_REPORT_2026-08.md` | Follow-up по второму ревью + первый hybrid gate 30×100 (2026-08-22/23) |
| `FINAL_STAGE1_FOLLOWUP_JOURNAL_2026-08.md` | Хронология разработки того follow-up (журнал к отчёту выше) |
| `HYBRID_LOAD_TEST_REPORT_2026-08.md` | Hybrid load gate: методология, матрица ревью, метрики прогона 20260822T233258Z |
| `HYBRID_LOAD_TEST_JOURNAL_2026-08.md` | Хронология неудачных промежуточных прогонов того gate |
| `FINAL_REVIEW_GATE_JOURNAL_2026-08.md` | Сага финальных гейтов: проходы 1–3, dust-fix, flip-фикс, post-fix rerun 20260823T075504Z |
| `REPORTS_FACTUAL_COMPARISON_2026-08.md` | Сверка фактологии параллельных исследований AsterDEX (19.08.2026) |
| `STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md` | Выполненный corrective-план Stage 1: protection quantity, emergency, boot recovery, signed/load/soak evidence и итоговый verdict (2026-08-23) |
| `STAGE2_AIOGRAM_RESEARCH_2026-08.md` | Справочник по закреплённому aiogram 3.30.0: Context7/официальные источники, API, lifecycle, security и test strategy (2026-08-23) |
| `STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` | **Исторический snapshot Stage 2**: возможности, owner/live testnet evidence и readiness на 2026-08-24; §27 явно описывает superseded engine scope |
| `STAGE2_IMPLEMENTATION_PLAN_2026-08.md` | Исторический implementation plan Stage 2: direct V3 onboarding, Telegram control/notifications, work packages, acceptance matrix и append-only журнал исполнения 24.08.2026 |
| `STAGE2_TELEGRAM_UX_BRIEF_2026-08.md` | Исторический inline-first UX brief: dashboard, screen map, copy, callbacks, Bot/Mini App boundary и обязательный Scenes verdict (2026-08-23) |
| `STAGE2_ACCESS_APPROVAL_2026-08.md` | Access onboarding hardening: inline captcha, durable request, env-admin decision, fallback admin screen и regression evidence (2026-08-24) |
| `STAGE2_ONBOARDING_UX_TYPED_ERRORS_2026-08.md` | Owner feedback corrective gate: Aster UI vocabulary, typed direct-onboarding errors, actionable Telegram retry и secret/FSM invariants (2026-08-24) |
| `STAGE2_ONBOARDING_CHAT_UX_2026-08.md` | Фактологический follow-up по чистым onboarding-карточкам, expiry recovery, Aster prerequisites и live-safe facts (2026-08-24) |
| `STAGE2_SAFE_LOGGING_AUDIT_2026-08.md` | Persistent rotating logging, onboarding audit classification, secret-safety и типы static/offline gate (2026-08-24); актуальные counters только в `ENGINE_TEST_STATUS_2026-08.md` |
| `STAGE3_MINI_APP_UX_PLAN_2026-08.md` | Design-only план Stage 3 follower Mini App: Builder authorization, IA, Aster-style screen concepts, анализ, missing application contracts и UI Definition of Done (2026-08-26) |
| `STAGE3_BUILDER_MINI_APP_PART1_2026-08.md` | Stage 3 часть 1 (Builder-auth Mini App): реестр решений владельца (23), план WP0–WP5, факты перепроверки доков, append-only журнал — реализация, signed e2e и ручная приёмка из Telegram (2026-08-31) |
| `STAGE3_PART1_REVIEW_GUIDE_2026-08.md` | Исторический гайд ревьюеру по части 1: scope, коммиты, security-инварианты, найденные проблемы и решения, команды тестов, отложенное (2026-08-31) |
| `STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md` | Исторический right-sized corrective plan после независимого code/UI review (`REQUEST CHANGES` на 2026-08-31); execution и signed-live evidence закрыты в `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` |
| `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` | Финальный независимый аудит, remediation ledger и закрывающие offline/frontend/full Testnet/manual evidence для Stage 3 Part 1 (2026-09-01) |
| `STAGE3_PART1_REMEDIATION_IMPLEMENTATION_PROMPT_2026-09.md` | Готовый промпт исполнителю Part 1 remediation (ветка `codex/stage-3-mini-app-part1`): обязательное чтение, diff-база, критерии приёмки. Исторический, исполнение закрыто файлом выше |
| `STAGE3_MINI_APP_PART2_ADAPTED_PLAN_2026-09.md` | Исторический исходный implementation plan follower Mini App: восемь подчастей P2.0–P2.7, файлы, Testnet gates и anti-overengineering boundaries (2026-09-01); frozen `PLAN READY` внутри не отражает закрытое исполнение, факт — в `STAGE3_PART2_PROGRESS_2026-09.md` |
| `STAGE3_PART2_PROGRESS_2026-09.md` | **Журнал исполнения Stage 3 Part 2: P2.0–P2.7 + post-closure Builder safety corrective, все CLOSED (2026-09-04).** Текущая правда по Part 2; сводные gates — `ENGINE_TEST_STATUS_2026-08.md` |
| `MARGIN_MODE_MIRROR_2026-09.md` | План, fail-closed границы и signed Aster Testnet evidence зеркалирования режима маржи |
| `ENGINE_MARGIN_AND_EXTERNAL_CLOSE_REVIEW_GUIDE_2026-09.md` | Гайд ревьюеру: новая семантика follower-close и mirror CROSSED/ISOLATED, карта diff, live-доказательства и ограничения |
| `PRE_MAINNET_HARDENING_PROGRESS_2026-09.md` | Append-only исторический журнал pre-mainnet hardening: Packages A–G, corrective qualification, signed REAL Aster Testnet и load evidence; текущий disposition — в финальной секции и `ENGINE_TEST_STATUS_2026-08.md` |
| `PRE_MAINNET_HARDENING_HANDOFF_2026-09.md` | Исторический handoff от 2026-09-04 с исходными требованиями REAL Aster Testnet; закрытые «следующие пакеты» не запускать по этому файлу |
| `acceptance-p27/` | Owner Telegram WebView acceptance P2.7: 9/9 PASS, 12 current-HEAD скриншотов всех табов и Pause/Resume (2026-09-03) |
| `acceptance-s3/` | S3 browser evidence: 20 скриншотов production bundle, включая Home, history alias/split/partial, nested screens и risk states; итог — S3 progress §§19–26 |
| `assets/stage3-mini-app/` | 18 файлов: `reference-templates/` (8 screen-эталонов `01–08` + 8 `.prompt.txt`) и `current-head-a9d67a6/` (2 current-HEAD скрина: recovery + wallet) |
| `MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md` | Центральная карта принятой UI-кампании: Chromium/CDP, Telegram Desktop, iPhone Mirroring, frontend tests и screenshots; G3 ACCEPTED в согласованном scope |
| `MINIAPP_TAB_PERF_2026-09.md` | Приёмка тёплого переключения вкладок Mini App: keep-alive + фоновая ревалидация, замеры cold/warm, бюджет запросов, цикл ревалидации (2026-09-04) |
| `MINIAPP_LIVE_MODE_PLAN_2026-09.md` | **ACCEPTED (REAL Aster Testnet), смержено в `main`:** live positions/PnL/history без штатной refresh-кнопки; follower/private + mark-price streams, authenticated Mini App WebSocket, stale/reconnect UX и acceptance matrix. §8 ведёт журнал проходов de-noising (коды движка → русская копия, один голос на факт, обратная связь по нажатию, честный возраст доказательств, churn сокетов) и merge-review corrective с live-перепроверкой |
| `MINIAPP_LIVE_MERGE_REVIEW_RESPONSE_2026-09.md` | Пофайловый ответ ревьюеру на merge-review ветки `codex/miniapp-live-updates`: все 7 × P1 и 8 × P2 — что изменено, чем доказано (unit + A/B на живых каналах), гейты итогового дерева `6d50fe7`, остаточные ограничения. Ревьюер вернул **GO**, ветка слита в `main` (`c4a8eba`). Дословный текст самого ревью — во внешнем handoff, здесь только ответ |
| `MINIAPP_S2_REVIEW_HANDOFF_2026-09.md` | Handoff независимому reviewer по закрытому S2/G2: три архитектурных слоя, инварианты §19/§20, критические race/lifecycle точки и команды gate (2026-09-07) |
| `MINIAPP_S2_TESTING_HANDOFF_2026-09.md` | Handoff тестировщику S2/G2: browser/manual матрица cold/warm, Settings prefetch, mutation unknown/recovery, Back/Sheet/scroll/focus, 50-cycle memory и граница G3 (2026-09-07) |
| `MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md` | Исторический исходный план последовательности S1/G1 → S2/G2 → S3/G3; все три gate закрыты к 2026-09-09. Backend B1–B10, §19 — скорость F0–F6, §20 — review. Фактические итоги: MINIAPP_FUNCTIONAL_PROGRESS, MINIAPP_S2_PROGRESS и MINIAPP_S3_PROGRESS |
| `MINIAPP_RISK_RULER_2026-09.md` | Шкала риска — vertical slice S2.5 lifecycle + S3.4 motion: browser 15/15 PASS и независимая перепроверка; итоговый touch/keyboard/WebKit проход выполнен на iPhone Mirroring в S3 §26 |
| `MINIAPP_S1_REVIEW_2026-09.md` | Исторический независимый verdict `REQUEST CHANGES` (`f77a3fd`, повторный проход `9a0dd98`): источник N1–N6 и baseline reproductions. Обязательные для G1 N1–N3 затем закрыты corrective-пакетом; текущий статус брать из ENGINE_TEST_STATUS / журнала шага 30 |
| `MINIAPP_S1_FIX_HANDOFF_2026-09.md` | Выполненный corrective handoff масштаба 10–30: N1–N3, T1–T6 и §6 закрыты на tested `84cabfb`; G1 CLOSED 2026-09-07. Отложенные там N4–N6 позднее закрыты в S3/G3 |
| `PNL_NET_FEES_PLAN_2026-09.md` | Кампания «PnL с учётом комиссий» (Mini App + Telegram): границы владельца, **измеренный факт** — комиссия Builder списывается внутри обычного `COMMISSION` (0,05% против 0,04%, разница ровно `feeRate`), тому же замеру подчинён bound чтения денег (комиссия филла приходит на 0,5–0,9 с раньше филла), durable-штамп политики, единое число результата сделки. **P0–P4 закрыты**, net-карточка доставлена живым Bot API; открыты Mini App HTTP live-проверка и продуктовое решение по не-USDT комиссии |

## docs/reviews/ — дословные тексты внешних ревью

**Никогда не редактировать и не перефразировать** — это доказательная база гейтов.
Дословные тексты — для 4 проходов Stage 1 (`review_stage1_old/new/final/acceptance`) + leverage + risk-ruler; проходы 2b/3 (dust zero-target, flip) verbatim не сохранялись, факты по ним — `FINAL_REVIEW_GATE_JOURNAL_2026-08.md` §§7–8, `ENGINE_TEST_STATUS_2026-08.md:1050-1096`, `DEV_PLAN_SKELETON.md:530-541`.
Ответы по финальному gate — в `docs/reports/FINAL_REVIEW_GATE_JOURNAL_2026-08.md` (§§1–6 — пункты final, §§7–8 — проходы 2b/3); по первому ревью — `docs/reports/REFACTOR_STAGE1_REPORT.md`, по второму — `docs/reports/FINAL_STAGE1_FOLLOWUP_REPORT_2026-08.md` (+ журнал и `HYBRID_LOAD_TEST_REPORT_2026-08.md`), по leverage — внутри `review_leverage_control_plane.md` (acceptance :159; stage-контекст — `STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` §7), по risk-ruler — `docs/reports/MINIAPP_RISK_RULER_2026-09.md` (финальная секция).

| Файл | Проход | Итог |
|---|---|---|
| `review_stage1_old.md` | Первое большое ревью после базового рефакторинга | список P0/P1, часть снята |
| `review_stage1_new.md` | Второе ревью: 6 блокеров (protective convergence, nonce, leverage…) | все закрыты в follow-up |
| `review_stage1_final.md` | Третий проход: P1 protective convergence (failed CANCEL после успешного PLACE) + недостающий ONE_WAY E2E live-proof | оба закрыты в follow-up |
| `review_stage1_acceptance.md` | Финальное принятие: flip-регрессия + честное dust-evidence | **Stage 1 принят**, 0 blockers, две мелочи (doc-расхождение hold_flat + P2 polish); dust/flip — позднейшие проходы 2b/3, факты — `FINAL_REVIEW_GATE_JOURNAL_2026-08.md` §§7–8 + `ENGINE_TEST_STATUS_2026-08.md:1050-1096` |
| `review_leverage_control_plane.md` | Три прохода ревью leverage control-plane | 4 находки → остаточный P1 (ambiguous ≠ FAILED) → финальный acceptance `69fa148` (2026-08-25) |
| `review_miniapp_risk_ruler_2026-09.md` | Независимая перепроверка браузерной приёмки шкалы риска: перемер подмножества живым CDP/DOM, все числа сошлись; одна формулировочная находка («в тот же тик» → «в ближайшие кадры») | verdict: приёмка реальная, slice принять; правка применена, разрешение — в `MINIAPP_RISK_RULER_2026-09.md` |
Примечание — только к четырём строкам Stage 1 выше: актуальный corrective acceptance после нового независимого аудита описан в `docs/reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md` (gate 2026-08-23). Leverage принят 2026-08-25 внутри `review_leverage_control_plane.md` (:159), risk-ruler 2026-09-06 разрешён в `MINIAPP_RISK_RULER_2026-09.md` — планом 2026-08-23 они не покрываются.

## docs/history/ — выполненное и исходное

| Файл | Что это |
|---|---|
| `plan_stage_1_archive.md` | Исследовательский план Stage 1 (~80K), выполнен полностью. Грузить только при вопросе «почему так спроектировано» |
| `firstcontext.md` | Исходный брифинг проекта от владельца (требования к копитрейд-боту) |

## Конвенции

- Один дом на факт: счётчики suite — только `ENGINE_TEST_STATUS_2026-08.md`,
  карта — только этот файл, контракты подсистем — только вложенные
  `tests|scripts|miniapp/AGENTS.md`; остальное ссылается, не пересказывает.
- Отчёт о работе: сначала why, потом what (net change списком файлов), потом verification с меткой источника (где гонялось и чем локальный прогон слабее полного). «Проверено» без метки запрещено.
- Язык: корень и `tests|scripts|miniapp/AGENTS.md` — English, `docs/*.md` — Russian.
- Свежесть в том же коммите: изменил поведение — обнови доку сразу: новый файл/скрипт/экран — строка в соответствующий `AGENTS.md`; новая секция поведения — `docs/ARCHITECTURE.md` + ссылки из докстрингов; переезд символа — grep по `docs/`, `miniapp/`, всем `AGENTS.md`.
- Потолки объёма стоячих доков (`scripts/doc-budgets.json`, гейт `python scripts/check_doc_budgets.py`): при красном гейте сначала вынеси чужое в свой дом + однострочная ссылка, потом ужати своё, потолок поднимай только если словам тесно — с обоснованием в коммите. Слишком низкий потолок — баг бюджета.
- Новые кампании: `docs/reports/TOPIC_YYYY-MM.md`, даты прогонов внутри файла.
- Актуальные счётчики тестов — только в `docs/ENGINE_TEST_STATUS_2026-08.md`;
  в исторических файлах они остаются как снимки момента.
- Перемещение документов согласуется с этой картой; перед коммитом проверить,
  что все `.md`-ссылки указывают на существующие файлы (`python scripts/check_doc_links.py`), а объёмы карт — в потолках (`python scripts/check_doc_budgets.py`).
- Anti-rot считаемых фактов: стоячий док НЕ транскрибирует счётчики, статусы гейтов и количества файлов — только ссылку на дом-владелец; считаемый факт допустим лишь при покрытии пробой гейта (`python scripts/check_doc_facts.py`).

Текущий журнал реализации: [MINIAPP_FUNCTIONAL_PROGRESS_2026-09.md](reports/MINIAPP_FUNCTIONAL_PROGRESS_2026-09.md) — append-only шаги; G1/G2/G3 закрыты.
Отдельный журнал S2: [MINIAPP_S2_PROGRESS_2026-09.md](reports/MINIAPP_S2_PROGRESS_2026-09.md) — S2.1–S2.6 выполнены, G2 ACCEPTED, session resources, Settings prefetch, lifecycle, performance measurements и G2 evidence.
