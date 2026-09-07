# Mini App S2/G2 — handoff для code/architecture review

Дата: 2026-09-07. Ветка: `feature/frontend`. Базовый commit до S2:
`b10f01a2b54fd3f7f17a1beafe10486922bdd88b`.

Этот документ — короткий маршрут для независимого ревью уже реализованного S2.
Источник требований и архитектурных решений:
`MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md`, прежде всего §14 S2/G2,
§19.1–§19.8 и §20. Фактический журнал и измерения:
`MINIAPP_S2_PROGRESS_2026-09.md`.

## Что входит в review

S2 ускоряет **существующие** четыре root-экрана и Settings nested screens. Он не
реализует новый Home/Closed/Trade detail/feed из S3 и не должен оцениваться как
S3/G3. Физические Telegram iOS/Android haptics, native WebView Back, WebKit inertia
и snap-stop A/B остаются device acceptance S3.5/G3.

Проверить нужно не только зелёные тесты, а соответствие принятым архитектурным
решениям:

- четыре root tabs остаются постоянными `hidden` keep-alive slots;
- один `MiniAppResources` создаётся на connected-session scope выше `AppShell`;
- cache фиксированный и typed, без global singleton/localStorage/Service Worker;
- weak/strong reads различаются; strong не удовлетворяется weak flight;
- generations/invalidation/write barriers не дают старому read затереть mutation;
- invalidation во время flight не теряется и даёт не больше одного нужного
  post-boundary follow-up;
- failed current refresh не порождает автоматический retry loop;
- ambiguous mutation не replay'ится; session owner lock переживает Back/remount;
- если mutation и reconciliation обе неизвестны, typed `unknown` остаётся
  fail-closed до более позднего authoritative evidence;
- `401`/`invalid_session` — hard scope boundary, а не локальная ошибка экрана;
- Settings prefetch начинается только после видимого входа, максимум два weak
  reads одновременно; hidden warmup не делает signed refresh;
- nested stack entry имеет собственные scroll/focus slots; root slot не портится;
- видимый `Sheet` владеет Back первым и не сохраняет hidden body/focus/keydown
  lifecycle;
- один foreground/TTL owner, без polling hidden roots и idle API;
- оптимизации memo/lazy/virtualization не добавлены без trace evidence.

## Часть A — transport, resources, mutation safety

Основные файлы:

- `miniapp/src/api.ts`, `api.test.ts`;
- `miniapp/src/resources.ts`, `resources.test.ts`;
- `miniapp/src/useResource.ts`, `useResource.test.tsx`;
- `miniapp/src/resourceScopeContext.ts`, `App.tsx`;
- `screens/HomeScreen.tsx`, `RiskScreen.tsx`, `NotificationsScreen.tsx`,
  `AccountBuilderScreen.tsx`, `SystemStatusScreen.tsx`;
- соответствующие behavior tests.

Критические сценарии review:

1. Timeout живёт до завершения body consumption; external abort listener снимается
   в `finally`; empty/malformed JSON даёт safe `invalid_response`.
2. StrictMode synthetic cleanup не убивает shared initial read.
3. Strong/weak ordering, mutation barrier и invalidation revisions не принимают
   pre-mutation response как post-write truth.
4. `refreshCurrent()` делает follow-up после fenced старого flight и при success,
   и если этот **obsolete** flight отвергнут; реальный current network failure
   публикует error и не retry'ится автоматически.
5. Risk/Notifications/pause-resume/AccountBuilder держат synchronous session lock.
   Timeout/network mutation никогда не повторяется автоматически.
6. Double-unknown сохраняется через unmount/remount; explicit/foreground strong
   authoritative recovery может снять его, старый release closure — нет.
7. Shared/direct invalid-session path уничтожает owner, abort'ит siblings и
   возвращает App в session gate ровно один раз.
8. Cached strong Status не остаётся false-green во время обязательной проверки;
   Agent expiry AccountBuilder становится stale без API polling.

## Часть B — shell, navigation и lifecycle

Основные файлы:

- `miniapp/src/components/AppShell.tsx`;
- `miniapp/src/navigation.ts`, `telegram.ts`, `revalidate.ts`;
- Home/Positions/Activity/Settings root screens;
- `appShellKeepAlive.test.tsx`.

Проверить:

- все четыре root wrappers остаются mounted;
- ранний Positions tap: допустим первый weak read, затем ровно один correctness
  follow-up после Home strong snapshot; третьего read нет;
- root→nested сохраняет root scroll, nested открывается сверху;
- nested→nested не пишет свой scroll в root slot;
- Back восстанавливает правильный scroll/focus;
- native Back закрывает видимый Sheet перед route pop;
- скрытый Home/Sheet не оставляет `body.style.overflow`, focus trap или document
  keydown listener;
- hidden roots не получают recurring API/listeners/rAF;
- 50 циклов root→nested→Back не растят DOM/resource ownership.

## Часть C — Settings, performance и G2 evidence

Основные файлы:

- `settingsPrefetch.ts`, `settingsPrefetch.test.ts`;
- `SettingsScreen.tsx`, `SystemStatusScreen.tsx`, `AccountBuilderScreen.tsx`;
- `settingsNavigationPerformance.test.tsx`;
- `MINIAPP_S2_PROGRESS_2026-09.md`, `MINIAPP_TAB_PERF_2026-09.md`,
  `ENGINE_TEST_STATUS_2026-08.md`, `DEV_PLAN_SKELETON.md`, `miniapp/AGENTS.md`.

Measured production-bundle evidence, которое нужно сопоставить с §19.7:

| Проверка | S2 evidence |
|---|---:|
| Cold startup | 4 read POST |
| Warm root | 160 samples, p95 1.0 ms, 0 reads |
| Warm cached nested | 120 samples, p95 1.9 ms, 0 reads |
| Warm root при +300 ms network latency | 160 samples, p95 6.0 ms, 0 reads |
| First Settings activation | tap→content 10.9 ms, speculative peak 2 |
| Browser 50-cycle DOM | visible 319→319 |
| CDP DOM counters | docs/nodes/listeners 1/443/160 → 1/443/160 |
| Post-GC JS heap | +3.25%, без структурного DOM/listener роста |
| Activity scroll + Risk interaction | 0 long tasks >50 ms, 0 page errors |
| Explicit `status(refresh=true)` | 5 samples, 1.45–2.21 s, median 2.05 s |

Browser acceptance — Chromium 152, 390×844, production bundle через безопасный
engine preview/testnet path. Это G2 browser evidence, не physical device evidence.

## Независимые аудиты перед merge

По запросу владельца финальный WIP разделён на три независимые области и отдан
**трём новым** review agents. Финальный ledger после corrective-проходов:

| Область | Финальный verdict | Что было перепроверено / исправлено до PASS |
|---|---|---|
| Part A — transport/resources/mutation safety | **PASS — P0/P1/P2 нет** | Endpoint-specific runtime DTO validation, nested row/closed-enum validation, timeout/body lifetime, first-wins external-vs-timeout abort classification, weak/strong singleflight/generations, invalidation follow-up, retry backoff, last-consumer cancellation и mutation protection |
| Part B — shell/navigation/lifecycle | **PASS — P0/P1/P2 нет** | Four-root keep-alive, StrictMode/shared reads, Back/remount mutation safety, per-entry scroll/focus cleanup, reopen-at-top, Sheet-first Back и единая `telegram.ts` boundary |
| Part C — Settings/prefetch/performance/docs | **PASS — P0/P1/P2 нет** | max-2 speculative reads, click priority/admission, cancellation, Back-before-settle rewarm без auto-retry реальной network error, G2 evidence/provenance и документация |

Part A первоначально нашёл реальные transport gaps: non-empty wrong-shape HTTP
200 мог пройти поверхностную проверку, а более поздний timeout мог переопределить
уже случившийся external abort. После endpoint-specific validator hardening и
first-wins abort fix replacement-аудитор повторил Part A на current WIP:
focused **7 files / 95 tests PASS**, full frontend **27 files / 316 tests PASS**,
oxlint **0/0**; итог — **PASS**. Подробный corrective-журнал находится в
`MINIAPP_S2_PROGRESS_2026-09.md`.

Reviewer должен считать S2 принятым только если после этих проходов нет P0/P1,
полный frontend gate зелёный, docs links/diff clean и текущие цифры совпадают с
`ENGINE_TEST_STATUS_2026-08.md`.

## Команды reviewer gate

```bash
cd /copytradeaster-front/miniapp
CI=true pnpm test
CI=true pnpm lint
CI=true pnpm build

cd /copytradeaster-front
python3 scripts/check_doc_links.py
git diff --check
```

Не запускать mainnet или production DB ради этого frontend review. Live browser
evidence S2 собирался через `scripts/preview_miniapp_engine.py` на копии SQLite и
Aster testnet.
