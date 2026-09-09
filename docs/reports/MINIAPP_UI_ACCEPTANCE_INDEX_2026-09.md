# Mini App UI — карта тестов и acceptance evidence (S2/S3, 2026-09)

Этот файл — центральный указатель по проверкам пользовательского интерфейса
Mini App. В охват входят ручные проходы в Telegram WebView, ручные и CDP-
проходы в Chromium, frontend Vitest/behavior-тесты и связанные screenshots.
Backend, engine и signed Aster testnet suites сюда не включаются; их результаты
остаются в `ENGINE_TEST_STATUS_2026-08.md` и профильных журналах.

Текущий итог:

- S2/G2 по нынешним экранам — **ACCEPTED**.
- Telegram Desktop S2-only revalidation — **PASS** по UI/lifecycle сценариям,
  записан в S2 §Telegram WebView revalidation и S3 §22.
- S3 Telegram Desktop evidence — **PASS** по фактически проверенным экранам и
  переходам, записан в S3 §§21–22.
- S3/G3 — **OPEN**: остаются отдельные performance/device-проверки и несколько
  специальных history-сценариев; viewport-блок закрыт текущим browser pass.
- Последний frontend gate: `28` test files, `349` passed, lint clean,
  `tsc -b && vite build` PASS.

## С чего начинать

1. [MINIAPP_S2_PROGRESS_2026-09.md](MINIAPP_S2_PROGRESS_2026-09.md) — S2/G2:
   data ownership, cache/lifecycle, browser measurements и Telegram-only
   revalidation в конце файла.
2. [MINIAPP_TAB_PERF_2026-09.md](MINIAPP_TAB_PERF_2026-09.md) — отдельный
   ранний browser walkthrough keep-alive и warm tab switching.
3. [MINIAPP_RISK_RULER_2026-09.md](MINIAPP_RISK_RULER_2026-09.md) — полный
   browser/CDP-чеклист шкалы риска и результат `15/15 PASS`.
4. [MINIAPP_S3_PROGRESS_2026-09.md](MINIAPP_S3_PROGRESS_2026-09.md) — S3:
   browser §§19–20, Telegram Desktop closed/detail §§21 и S2 revalidation §22.
5. [MINIAPP_S3_TESTING_HANDOFF_2026-09.md](MINIAPP_S3_TESTING_HANDOFF_2026-09.md)
   — исходная матрица S3 и границы того, что ещё требуется для G3.
6. [STAGE3_PART2_PROGRESS_2026-09.md](STAGE3_PART2_PROGRESS_2026-09.md) —
   исторический owner Telegram acceptance P2.0–P2.7, включая `9/9 PASS`.
7. [ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md) — единая
   точка актуальных автоматических счётчиков и общий verdict.

## Матрица ручных UI-проверок

| Этап и поверхность | Что реально проверялось | Где записано | Итог |
|---|---|---|---|
| P2.0–P2.7, Telegram WebView владельца, iPhone, 2026-09-02/03 | Home, 4 root tabs, Positions, Activity, Risk, Status, Account/Builder, Notifications on/off, Pause/Resume с confirm | `STAGE3_PART2_PROGRESS_2026-09.md`, owner section; `docs/reports/acceptance-p27/` | `9/9 PASS`, исторический owner gate закрыт |
| S2/G2, Chromium/CDP, 2026-09-07, production bundle, 390×844 | Cold root budget, warm roots, Settings prefetch, cached nested, 300 ms latency, 50 cycles, DOM/listeners/heap, long tasks and explicit Status | `MINIAPP_S2_PROGRESS_2026-09.md`, `G2 measured evidence`; `MINIAPP_TAB_PERF_2026-09.md` | G2 ACCEPTED; cold `4` reads, warm root `160` samples p95 `1.0 ms`, nested `120` p95 `1.9 ms`, throttled root p95 `6.0 ms`, warm API delta `0` |
| Risk ruler, Chromium/CDP, 2026-09-06, 390×844 | Touch short/long/reversal/vertical, tap settle, interrupted settle, mouse capture, save freeze, wheel gates, keyboard/ARIA, idle, frame/layout, reduced motion, edge fade and widths in the browser checklist | `MINIAPP_RISK_RULER_2026-09.md` | Browser slice `15/15 PASS`; device-only WebKit/haptics remains outside that gate |
| S3, Chromium/Computer, 2026-09-09, 390×844 | Home active/paused/resume sheet, live copy open→close, Positions, Activity, Settings and 6 nested screens, Risk save `2%→1%→2%`, error/stale/unknown states and notification gate probe | `MINIAPP_S3_PROGRESS_2026-09.md` §§19–20; `docs/reports/acceptance-s3/` | Partial S3 browser evidence; 17 PNGs, G3 remains open |
| S3 viewport recheck, Chromium/Computer, 2026-09-09, `320/360/430×844` | Home, Positions open/closed, Activity, Settings и все 6 nested screens; AX/DOM проверка page-level overflow; визуальная проверка Home на трёх ширинах и Risk на `320` | `MINIAPP_S3_PROGRESS_2026-09.md` §23 | PASS: на каждой ширине `documentWidth = bodyWidth = viewportWidth`, page-level horizontal overflow отсутствует |
| S3, Telegram Desktop WebView, 2026-09-09 | Closed list, non-empty trade detail, event blocks, two fills раскрываются, Activity filters and owned link, native Back, Settings/nested screens, Home flat and pause/resume | `MINIAPP_S3_PROGRESS_2026-09.md` §21; handoff addition | PASS for listed Telegram Desktop scenarios |
| S2-only revalidation, Telegram Desktop WebView, 2026-09-09 | Root transitions, Settings cache/revisit, stale Status/Aster, Notifications read-only, Risk dirty draft and discard Sheet, scroll/Back, 50 `Settings→Risk→Back` cycles, Resume Sheet Cancel and confirmed Resume | `MINIAPP_S2_PROGRESS_2026-09.md`, final section; `MINIAPP_S3_PROGRESS_2026-09.md` §22 | PASS for listed UI/lifecycle smoke |

Последний Telegram-проход завершился в безопасном подключённом состоянии:
Home показывал `Копирование включено`, открытых позиций `0`, риск `2%`;
уведомления оставлены выключенными. В аккаунте было `170` history episodes и
`487` fills, незавершённых execution commands не было. Точная сверка БД записана
в S3 §§21–22 и финальном Telegram-разделе S2.

## Что проверялось в Telegram

Исторический owner pass в `acceptance-p27` — первая полная mobile acceptance
для P2.0–P2.7. Он охватил четырёхвкладочный shell и исходные Settings/Risk/
Status/Builder flows в iPhone Telegram WebView. Текущий Telegram Desktop pass
расширяет evidence на S3 history UI:

- `Позиции → Закрытые` showed two real cards, including `BTCUSDT · LONG`,
  `+2,25 USDT`, entry `78 671,5`, exit `78 732,3`;
- detail showed entry/closed/remaining quantities, averages, duration and
  open/close events; entry and exit fills expanded independently;
- `Активность` showed `Все`/`Сделки`/`Система`, safe event text and
  `Открыть сделку`; the owned detail opened and Back returned to the list;
- Settings opened Risk, Notifications, Status, Aster connection, Security and
  Help. Notifications were read only and remained disabled;
- Home pause showed `Новые входы на паузе`; Resume Sheet required explicit
  confirmation. Cancel preserved paused state; confirmation restored active;
- Risk `2,5%` dirty draft raised the discard Sheet and was not persisted;
  reopening showed the server value `2,0%`;
- 50 nested navigation cycles completed without a stuck screen. This is a
  visual lifecycle check in Telegram; HTTP request counts and CDP memory
  counters were not instrumented in Telegram Desktop.

## Какие frontend-тесты дописывались и проверялись

Автоматические тесты лежат рядом с исходниками Mini App в
`miniapp/src/*.test.ts` и `*.test.tsx`. Они не заменяют тап в Telegram, но
закрепляют те же контракты до ручных проходов.

| Группа | Файлы | Что закреплено |
|---|---|---|
| Pure view/model и routing | `api.test.ts`, `errors.test.ts`, `format.test.ts`, `home.test.ts`, `navigation.test.ts`, `session.test.ts`, `wallet.test.ts`, `wizard.test.ts`, `accountBuilder.test.ts`, `systemStatus.test.ts`, `riskRuler.test.ts` | Typed transport errors, safe formatting, Home status, root/nested routing, session gate, wallet seam, onboarding reducer, status view model и ruler math |
| Static screen contracts | `positions.test.tsx`, `activity.test.tsx`, `notifications.test.tsx`, `risk.test.tsx`, `settings.test.tsx` | Read-only markup, safe DTO presentation, filters, stale/error copy, permissions, Settings rows, risk screen и secret-free output |
| Behavior и mutation paths | `activityInvalidSession.test.tsx`, `accountBuilderBehavior.test.tsx`, `notificationsBehavior.test.tsx`, `riskBehavior.test.tsx`, `systemStatusBehavior.test.tsx`, `riskRulerBehavior.test.tsx` | Activity auth boundary, double-tap toggle gate, Builder expiry/reauth/disconnect, dirty/save/unknown Risk states, status refresh и ruler touch/mouse/settle behavior |
| Resource, cache и lifecycle | `resources.test.ts`, `useResource.test.tsx`, `settingsPrefetch.test.ts`, `settingsNavigationPerformance.test.tsx`, `appShellKeepAlive.test.tsx`, `historyResources.test.ts` | Session owner, weak/strong singleflight, invalidation/write fences, bounded Settings prefetch, root keep-alive, scroll/focus/Sheet cleanup, 50-cycle lifecycle и S3 history cache/revision/retry |

Последний фактический запуск frontend suite:

```text
cd miniapp && CI=true pnpm test
  28 test files / 349 passed
cd miniapp && CI=true pnpm lint
  clean
cd miniapp && CI=true pnpm build
  tsc -b + Vite production build: PASS
```

Для хронологии добавления этих проверок полезны коммиты:

- `a6afc18`, `5e5b533`, `ec09d49` — P2.0 shell, Positions, Activity;
- `fe84d2c`, `7413abc` — Settings/Notifications и double-tap gate;
- `f8fb338`, `09a4d74`, `40b8323`, `be6b9a9` — Risk ruler и lifecycle corrective;
- `c35e2b9`, `9bcbc21`, `75f0780` — Status и Account/Builder flows;
- `41fba43` — S2 resource owner, keep-alive, prefetch, mutation/recovery tests;
- `6bfc160` — отдельный risk-ruler slice;
- `957bef5`, `06eba59`, `b83c708`, `903d4f4`, `09bd528` — S3 history,
  Activity, cache/revision, Home/Settings and timezone/resume regressions.

## Где лежат screenshots и прочие артефакты

- `docs/reports/acceptance-p27/` — 12 PNG owner Telegram WebView P2.0–P2.7,
  включая all tabs, Settings, Risk, Status, Notifications, Pause/Resume and
  Account/Builder.
- `docs/reports/acceptance-s3/` — 17 PNG S3 browser pass, production bundle,
  viewport 390×844: Home, Positions, Activity, Settings and nested screens.
- `data/miniapp-perf/` — git-ignored browser perf captures referenced by
  `MINIAPP_TAB_PERF_2026-09.md`.
- `scripts/dev-up.sh` — bot + tunnel + production bundle at port `8080`;
  `scripts/preview_miniapp_engine.py` — testnet engine on a SQLite backup copy
  for browser UI checks at a separate port.
- `scripts/manual_master_order.py` — live testnet master open/close used to
  create the real open/closed data shown by the Mini App. It is an engine
  operator script, not a frontend test.

## Что ещё не закрыто этим индексом

G3 остаётся открытым для отдельно обозначенного evidence:

1. Свежий S3 §19.7 CDP trace после финальных S3 transport/history changes:
   cold/warm request budget, delayed API, long history, long tasks and 50-cycle
   memory/listener counters. The S2/G2 measurements above are valid S2 evidence
   and must not be reused as a new S3 trace.
2. S3-specific mobile iOS/Android WebKit evidence for haptics, keyboard,
   gestures and short viewport. The earlier iPhone P2.0–P2.7 acceptance remains
   valid for that historical scope.
3. Visual split/alias/partial-history cases and a full blocked/throttled API
   matrix. Normal closed detail, stale/incomplete history and unknown-ref
   fail-closed states were already checked in Chromium/Telegram as recorded
   above.

Для итогового verdict читай этот index вместе с S3 §§19–23. Старую фразу
«не запускалось» из pre-acceptance-раздела нельзя принимать за текущий статус:
актуальные факты находятся в более поздних append-only sections.
