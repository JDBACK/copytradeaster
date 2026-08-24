# Карта документации

Что где лежит, что из этого живое, а что история, и что читать в каком порядке.
Обновляется при перемещении/добавлении документов. Последняя перестройка: 2026-08-24.

## Быстрый старт (что читать в новом сеансе)

1. `AGENTS.md` (корень) — правила работы с репо и структура папок.
2. `PRODUCT.md` (корень) — durable product truth: пользователи, задачи,
   Telegram/Mini App boundaries и принципы интерфейса.
3. `docs/ENGINE_TEST_STATUS_2026-08.md` — текущее состояние движка: счётчики suite,
   список исправленных багов #1–23, кампании и актуальный acceptance verdict.
4. `docs/reports/STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md` — актуальный
   фактический scope Stage 2, реализованная архитектура, owner/testnet evidence,
   ограничения и единственный оставшийся acceptance-сценарий.
5. `docs/DEV_PLAN_SKELETON.md` — roadmap этапов и чеклисты; что уже сделано, что дальше.
6. `docs/ARCHITECTURE.md` — архитектура ядра и Stage 2 process.
7. `docs/reports/STAGE2_IMPLEMENTATION_PLAN_2026-08.md` — исходный полный
  план Telegram/direct-V3 этапа и append-only журнал исполнения. Его старые
  sections/checklists — история планирования; текущую правду брать из пункта 4.
8. `docs/reports/STAGE2_ACCESS_APPROVAL_2026-08.md` — фактический журнал
   guest captcha, durable access request, env-admin approval и regression gate.
9. `docs/reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md` — выполненный
   corrective gate после независимого аудита: план, полный журнал и evidence.

Всё остальное — справочники и история: грузить по необходимости, не по умолчанию.

## Живые документы

| Файл | Что это | Когда читать/менять |
|---|---|---|
| `PRODUCT.md` (корень) | Пользователи, purpose, позиционирование, operating context и durable product principles | Читать перед проектированием пользовательских поверхностей; менять только при изменении product truth |
| `docs/ARCHITECTURE.md` | Архитектура системы: стили, модули, planner/reconciliation, journal, конкурентность, recovery, этапы | Читать перед работой с кодом. **Не перемещать**: докстринги `src/copytradeaster/` ссылаются на её секции по номерам (`ARCHITECTURE.md §6/§7/§12`). Менять при изменении дизайна |
| `docs/DEV_PLAN_SKELETON.md` | Видение трёх этапов + актуальные Stage 1/2 чеклисты | Читать для понимания «что дальше». Обновлять по факту выполнения пунктов |
| `docs/ENGINE_TEST_STATUS_2026-08.md` | **Единая точка истины** о текущем состоянии: актуальные счётчики suite, таблица багов #1–23, разделы тестовых кампаний | Обновлять после каждого значимого изменения кода/тестов. Не дублировать его счётчики в других файлах |
| `docs/ASTER_API_RESEARCH_2026-08.md` | Фактологический справочник по Aster V3 API: подписи, ордера, WS, лимиты, режимы позиций | Читать при работе с `src/copytradeaster/aster/` или новыми API-вызовами |

## docs/reports/ — отчёты кампаний и corrective-планы

Отчёты фиксируют состояние на момент своей кампании: задним числом их не
переписывать, дополнения — только новыми секциями в конце. Текущий
corrective-план завершён и содержит фактический журнал/evidence acceptance.

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

## docs/reviews/ — дословные тексты внешних ревью

**Никогда не редактировать и не перефразировать** — это доказательная база гейтов.
Ответы на ревью живут в `docs/reports/FINAL_REVIEW_GATE_JOURNAL_2026-08.md`.

| Файл | Проход | Итог |
|---|---|---|
| `review_stage1_old.md` | Первое большое ревью после базового рефакторинга | список P0/P1, часть снята |
| `review_stage1_new.md` | Второе ревью: 6 блокеров (protective convergence, nonce, leverage…) | все закрыты в follow-up |
| `review_stage1_final.md` | Третий проход: dust zero-target edge case | закрыт (миграция #12) |
| `review_stage1_acceptance.md` | Финальное принятие: flip-регрессия + честное dust-evidence | **Stage 1 принят**, 0 blockers, две P2-заметки |

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
- Перемещение документов согласуется с этой картой; ссылки проверяются скриптом
  на битые `.md`-таргеты до коммита.
