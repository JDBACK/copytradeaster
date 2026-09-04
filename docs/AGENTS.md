# Карта документации

Что где лежит, что из этого живое, а что история, и что читать в каком порядке.
Обновляется при перемещении/добавлении документов. Последняя перестройка: 2026-09-04.

## Быстрый старт (что читать в новом сеансе)

1. `AGENTS.md` (корень) — правила работы с репо и структура папок.
2. `PRODUCT.md` (корень) — durable product truth: пользователи, задачи,
   Telegram/Mini App boundaries и принципы интерфейса.
3. `docs/ENGINE_TEST_STATUS_2026-08.md` — текущее состояние движка: счётчики suite,
   список исправленных багов #1–26, кампании и актуальный acceptance verdict.
4. `docs/reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` — актуальный
   фактический scope Stage 2, реализованная архитектура, owner/testnet evidence,
   leverage control-plane, ограничения и выполненный acceptance verdict.
5. `docs/DEV_PLAN_SKELETON.md` — roadmap этапов и чеклисты; что уже сделано, что дальше.
6. `docs/reports/STAGE3_MINI_APP_PART2_ADAPTED_PLAN_2026-09.md` — исходный
   план Stage 3 follower Mini App (P2.0–P2.7, статус внутри `PLAN READY /
   IMPLEMENTATION NOT STARTED` на 2026-09-01): история планирования, не факт.
   Факт исполнения — в пункте 7.
7. `docs/reports/STAGE3_PART2_PROGRESS_2026-09.md` — журнал исполнения
   Stage 3 Part 2: P2.0–P2.7 + post-closure corrective, все CLOSED (2026-09-04).
   Читать вместо плана из пункта 6 для текущего состояния.
8. `docs/reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md` — design-only источник:
   IA, восемь Aster-style concepts и исходные screen states.
9. `docs/reports/STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` —
   закрытый corrective gate Part 1 и фактическое acceptance evidence.
10. `docs/ARCHITECTURE.md` — архитектура ядра и Stage 2 process.
11. `docs/reports/STAGE2_IMPLEMENTATION_PLAN_2026-08.md` — исходный полный
  план Telegram/direct-V3 этапа и append-only журнал исполнения. Его старые
  sections/checklists — история планирования; текущую правду брать из пункта 4.
12. `docs/reports/STAGE2_ACCESS_APPROVAL_2026-08.md` — фактический журнал
   guest captcha, durable access request, env-admin approval и regression gate.
13. `docs/reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md` — выполненный
    corrective gate после независимого аудита: план, полный журнал и evidence.

Всё остальное — справочники и история: грузить по необходимости, не по умолчанию.

## Живые документы

| Файл | Что это | Когда читать/менять |
|---|---|---|
| `PRODUCT.md` (корень) | Пользователи, purpose, позиционирование, operating context и durable product principles | Читать перед проектированием пользовательских поверхностей; менять только при изменении product truth |
| `docs/ARCHITECTURE.md` | Архитектура системы: стили, модули, planner/reconciliation, journal, конкурентность, recovery, этапы | Читать перед работой с кодом. **Не перемещать**: докстринги `src/copytradeaster/` ссылаются на её секции по номерам (`ARCHITECTURE.md §6/§7/§12`). Менять при изменении дизайна |
| `docs/DEV_PLAN_SKELETON.md` | Видение трёх этапов + актуальные Stage 1/2 чеклисты | Читать для понимания «что дальше». Обновлять по факту выполнения пунктов |
| `docs/ENGINE_TEST_STATUS_2026-08.md` | **Единая точка истины** о текущем состоянии: актуальные счётчики suite, таблица багов #1–26, разделы тестовых кампаний | Обновлять после каждого значимого изменения кода/тестов. Не дублировать его счётчики в других файлах |
| `docs/ASTER_API_RESEARCH_2026-08.md` | Фактологический справочник по Aster V3 API: подписи, ордера, WS, лимиты, режимы позиций | Читать при работе с `src/copytradeaster/aster/` или новыми API-вызовами |

## docs/reports/ — отчёты кампаний и corrective-планы

Отчёты фиксируют состояние на момент своей кампании: задним числом их не
переписывать, дополнения — только новыми секциями в конце. Выполненные планы
содержат фактический журнал/evidence acceptance; текущий открытый gate помечен
своим статусом в таблице.

| Файл | Кампания |
|---|---|
| `REFACTOR_STAGE1_REPORT.md` | Базовый рефакторинг Stage 1 + инвентаризация первого ревью (2026-08-22) |
| `FINAL_STAGE1_FOLLOWUP_REPORT_2026-08.md` | Follow-up по второму ревью + первый hybrid gate 30×100 (2026-08-22/23) |
| `FINAL_STAGE1_FOLLOWUP_JOURNAL_2026-08.md` | Хронология разработки того follow-up (журнал к отчёту выше) |
| `HYBRID_LOAD_TEST_REPORT_2026-08.md` | Hybrid load gate: методология, матрица ревью, метрики прогона 20260822T233258Z |
| `HYBRID_LOAD_TEST_JOURNAL_2026-08.md` | Хронология неудачных промежуточных прогонов того gate |
| `FINAL_REVIEW_GATE_JOURNAL_2026-08.md` | Сага финальных гейтов: проходы 1–3, dust-fix, flip-фикс, post-fix rerun 20260823T075504Z |
| `REPORTS_FACTUAL_COMPARISON_2026-08.md` | Сверка фактологии параллельных исследований AsterDEX (19.08.2026) |
| `STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md` | Выполненный corrective-план Stage 1: protection quantity, emergency, boot recovery, signed/load/soak evidence и итоговый verdict (2026-08-23) |
| `STAGE2_AIOGRAM_RESEARCH_2026-08.md` | Актуальный stable aiogram 3.30.0: Context7/официальные источники, API, lifecycle, security и test strategy (2026-08-23) |
| `STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` | **Актуальный итог Stage 2:** фактические возможности, текущая архитектура, owner/live testnet evidence, runtime snapshot, ограничения и readiness verdict (2026-08-24) |
| `STAGE2_IMPLEMENTATION_PLAN_2026-08.md` | Implementation plan Stage 2: direct V3 onboarding, Telegram control/notifications, work packages, acceptance matrix и append-only журнал исполнения 24.08.2026 |
| `STAGE2_TELEGRAM_UX_BRIEF_2026-08.md` | Inline-first UX brief: dashboard, screen map, copy, callbacks, Bot/Mini App boundary и обязательный Scenes verdict (2026-08-23) |
| `STAGE2_ACCESS_APPROVAL_2026-08.md` | Access onboarding hardening: inline captcha, durable request, env-admin decision, fallback admin screen и regression evidence (2026-08-24) |
| `STAGE2_ONBOARDING_UX_TYPED_ERRORS_2026-08.md` | Owner feedback corrective gate: Aster UI vocabulary, typed direct-onboarding errors, actionable Telegram retry и secret/FSM invariants (2026-08-24) |
| `STAGE2_ONBOARDING_CHAT_UX_2026-08.md` | Фактологический follow-up по чистым onboarding-карточкам, expiry recovery, Aster prerequisites и live-safe facts (2026-08-24) |
| `STAGE2_SAFE_LOGGING_AUDIT_2026-08.md` | Persistent rotating logging, onboarding audit classification, secret-safety и типы static/offline gate (2026-08-24); актуальные counters только в `ENGINE_TEST_STATUS_2026-08.md` |
| `STAGE3_MINI_APP_UX_PLAN_2026-08.md` | Design-only план Stage 3 follower Mini App: Builder authorization, IA, Aster-style screen concepts, анализ, missing application contracts и UI Definition of Done (2026-08-26) |
| `STAGE3_BUILDER_MINI_APP_PART1_2026-08.md` | Stage 3 часть 1 (Builder-auth Mini App): реестр решений владельца (23), план WP0–WP5, факты перепроверки доков, append-only журнал — реализация, signed e2e и ручная приёмка из Telegram (2026-08-31) |
| `STAGE3_PART1_REVIEW_GUIDE_2026-08.md` | Гайд ревьюеру по части 1: scope, коммиты, security-инварианты, ключевые файлы, все найденные проблемы и решения, команды тестов, отложенное (2026-08-31) |
| `STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md` | Актуальный right-sized corrective plan после независимого code/UI review: REQUEST CHANGES, простая durable Builder recovery, Home/control-plane fixes и signed-live testnet кампания под 10–100 users; отдельный anti-overengineering audit (2026-08-31) |
| `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` | Финальный независимый аудит, remediation ledger и закрывающие offline/frontend/full Testnet/manual evidence для Stage 3 Part 1 (2026-09-01) |
| `STAGE3_PART1_REMEDIATION_IMPLEMENTATION_PROMPT_2026-09.md` | Готовый промпт исполнителю Part 1 remediation (ветка `codex/stage-3-mini-app-part1`): обязательное чтение, diff-база, критерии приёмки. Исторический, исполнение закрыто файлом выше |
| `STAGE3_MINI_APP_PART2_ADAPTED_PLAN_2026-09.md` | Исходный implementation plan follower Mini App: восемь подчастей P2.0–P2.7, конкретные файлы, Testnet acceptance gates и anti-overengineering boundaries (2026-09-01). Статус внутри — `PLAN READY / IMPLEMENTATION NOT STARTED`; факт исполнения — строкой ниже |
| `STAGE3_PART2_PROGRESS_2026-09.md` | **Журнал исполнения Stage 3 Part 2: P2.0–P2.7 + post-closure Builder safety corrective, все CLOSED (2026-09-04).** Текущая правда по Part 2; сводные gates — `ENGINE_TEST_STATUS_2026-08.md` |
| `docs/reports/acceptance-p27/` | Owner Telegram WebView acceptance P2.7: 9/9 PASS, 12 current-HEAD скриншотов всех табов и Pause/Resume (2026-09-03) |

## docs/reviews/ — дословные тексты внешних ревью

**Никогда не редактировать и не перефразировать** — это доказательная база гейтов.
Ответы на ревью живут в `docs/reports/FINAL_REVIEW_GATE_JOURNAL_2026-08.md`.

| Файл | Проход | Итог |
|---|---|---|
| `review_stage1_old.md` | Первое большое ревью после базового рефакторинга | список P0/P1, часть снята |
| `review_stage1_new.md` | Второе ревью: 6 блокеров (protective convergence, nonce, leverage…) | все закрыты в follow-up |
| `review_stage1_final.md` | Третий проход: dust zero-target edge case | закрыт (миграция #12) |
| `review_stage1_acceptance.md` | Финальное принятие: flip-регрессия + честное dust-evidence | **Stage 1 принят**, 0 blockers, две P2-заметки |
| `review_leverage_control_plane.md` | Три прохода ревью leverage control-plane: 4 находки → остаточный P1 (ambiguous ≠ FAILED) → финальный acceptance `69fa148` (2026-08-25) |
Это исторический verdict того прохода. Актуальный corrective acceptance после
нового независимого аудита описан в
`docs/reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md`.

## docs/history/ — выполненное и исходное

| Файл | Что это |
|---|---|
| `plan_stage_1_archive.md` | Исследовательский план Stage 1 (~80K), выполнен полностью. Грузить только при вопросе «почему так спроектировано» |
| `firstcontext.md` | Исходный брифинг проекта от владельца (требования к копитрейд-боту) |

## Конвенции

- Новые кампании: `docs/reports/TOPIC_YYYY-MM.md`, даты прогонов внутри файла.
- Актуальные счётчики тестов — только в `docs/ENGINE_TEST_STATUS_2026-08.md`;
  в исторических файлах они остаются как снимки момента.
- Перемещение документов согласуется с этой картой; перед коммитом проверить,
  что все `.md`-ссылки указывают на существующие файлы (`rg -o '\]\(([^)]+\.md[^)]*)\)' docs/ AGENTS.md PRODUCT.md README.md` + сверка с диском — отдельного скрипта проверки в репо нет).
