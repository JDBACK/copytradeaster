# Mini App S2/G2 — handoff для тестирования

Дата: 2026-09-07. Scope: S2 текущих экранов Mini App на ветке
`feature/frontend`. Источник требований — `MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md`
§14/§19; implementation/evidence — `MINIAPP_S2_PROGRESS_2026-09.md`.

Цель этого handoff — проверить скорость, lifecycle и safety нового data layer,
не новый S3 интерфейс. Home composition, Closed/Trade details/deep links и полный
device gate относятся к S3/G3.

## Автоматический gate

Перед ручной проверкой:

```bash
cd /copytradeaster-front/miniapp
CI=true pnpm test
CI=true pnpm lint
CI=true pnpm build
```

Актуальный suite counter хранится только в `docs/ENGINE_TEST_STATUS_2026-08.md`.
Нефатальные jsdom предупреждения React `act(...)` сейчас присутствуют в behavior
suite; они не являются browser runtime error, но новые test failures игнорировать
нельзя.

Current final automated gate на момент handoff: **27 test files / 316 PASS**,
oxlint **0/0**, TypeScript + Vite production build PASS; main JS **336.56 kB /
96.46 kB gzip**, CSS **19.42 / 4.41 kB**.

## Стенд G2 browser acceptance

Безопасный production-like путь:

```bash
cd /copytradeaster-front
.venv/bin/python scripts/preview_miniapp_engine.py \
  --account copyc1 \
  --confirm-engine-preview \
  --port 8086
```

Preview делает SQLite backup и использует Aster testnet. Не подменять его запуском
против production DB/mainnet. Для сравнимости performance использовать production
bundle, одинаковый dataset, фиксированный viewport и записать Chrome version.

## T1 — cold/warm root

1. Fresh page/session: посчитать штатные read POST.
   Ожидание S2: `session + dashboard + positions + activity` = **4**.
2. После warmup сделать не менее 30 переходов на каждый root.
3. На каждом переходе cached content должен появляться без full skeleton и без
   нового API read внутри TTL.
4. Повторить с искусственной network latency около 300 ms. Warm content не должен
   ждать RTT.

Reference evidence текущего G2: root p95 1.0 ms; throttled p95 6.0 ms; API delta 0.

## T2 — Settings prefetch и nested revisit

1. Не открывая Settings после cold start, убедиться: Risk/Notifications/Status/
   AccountBuilder speculative calls отсутствуют.
2. Первый видимый вход Settings: очередь может сделать до четырёх cheap reads,
   одновременно максимум **2**.
3. Hidden/speculative Status и AccountBuilder используют weak/false read; никакого
   signed refresh из warmup.
4. Сразу после входа нажать один nested target: target join/priority не должен
   создавать duplicate read.
   Отдельно воспроизвести Status, когда его weak prefetch уже идёт: user priority
   должна освободить другой unobserved speculative slot, чтобы weak Status +
   обязательный strong Status не образовали третий concurrent owner read.
5. Открыть Risk, Notifications, Status, AccountBuilder повторно внутри TTL:
   cached frame без full skeleton и без лишнего read.
   Отдельный race: открыть target и нажать Back до settle weak prefetch. Если
   last-consumer cancellation отменил старый target flight, уже снова видимый
   Settings-pass должен requeue/re-warm его, но реальный network failure не должен
   запускать auto-retry loop.
6. `Перепроверить разрешения` и explicit System Status verification должны делать
   сильную проверку отдельно; не считать её duplicate bug.

Reference nested p95: 1.9 ms, 0 reads на cached revisit.

## T3 — pause/resume mutation safety

Проверить оба направления:

1. Двойной быстрый tap допускает одну mutation.
2. Уйти Back/на другую вкладку после уже отправленной операции и вернуться —
   второй pause/resume не допускается, пока owner занят.
3. Смоделировать timeout/network ambiguous outcome: mutation не replay'ится;
   запускается authoritative reconciliation.
4. Pre-mutation strong Dashboard response, пришедший поздно, не должен разблокировать
   owner или считаться post-write truth; нужен post-boundary evidence.
5. Если reconciliation тоже не удалась, состояние остаётся `unknown` через remount.
   Явная `Обновить данные` делает strong recovery без повтора mutation.

## T4 — Risk

1. Dirty draft не переписывается внешним policy update; недопустимый старый draft
   становится unsavable.
2. Save замораживает ruler; отправляется ровно выбранная fraction.
3. Back во время in-flight save и повторный вход не допускают второй POST.
4. Timeout/network + failed verification оставляют typed `unknown` и disabled save
   через remount.
5. Явный `Обновить данные` снимает unknown только после authoritative resource read;
   mutation не повторяется.
6. Отдельно проверить race: старый fenced strong read может **отказаться**, а не
   только успешно завершиться. Тогда resource owner делает ровно один обязательный
   post-boundary follow-up; current network failure не запускает auto-loop.

Risk gesture/browser acceptance подробно остаётся в
`MINIAPP_RISK_RULER_2026-09.md`.

## T5 — Notifications

Повторить ту же destructive/ambiguous матрицу:

- один toggle POST на double tap;
- Back/remount не допускает дубликат;
- timeout/network не replay'ит toggle;
- failed reconciliation оставляет unknown lock;
- explicit authoritative refresh снимает lock без нового toggle POST;
- late/fenced pre-mutation read не считается доказательством результата.

## T6 — Account + Builder

1. Prepare/confirmation admission блокируется **до** late submit/confirm фазы и
   переживает remount.
2. Reauthorize timeout + failed verify остаётся unknown; explicit strong check
   восстанавливает возможность действия без replay submit.
3. Disconnect ambiguous outcome сверяется через session truth; уже совершённый
   disconnect приводит в not-connected path один раз.
4. Cached verified Agent, срок которого истёк пока экран открыт, становится amber
   без periodic API.
5. Visible disconnect/confirmation Sheet: native Back сначала закрывает Sheet,
   второй Back выходит из nested screen.

## T7 — invalid session

Проверить expired/invalid session во время уже connected shell:

- shared resource read;
- Activity direct read;
- Risk/Notifications/Home/AccountBuilder direct mutation paths.

Ожидание: hard dispose session resource scope, sibling requests aborted/fenced,
переход в session gate один раз; локальный экран не остаётся с stale connected UI.

## T8 — scroll/focus/Sheet lifecycle

1. На root прокрутиться, открыть nested, прокрутить nested, Back — root scroll
   восстановлен.
2. Nested открывается с top=0.
3. Nested→nested не меняет root slot.
4. Focus после Back возвращается к исходному row/button, когда target ещё существует.
5. Скрытие root с открытым Sheet не оставляет document keydown/focus trap/body
   overflow lock.
6. Native Back при visible Sheet закрывает Sheet первым.

## T9 — 50-cycle lifecycle/performance

После warmup выполнить минимум 50 циклов Settings→Risk→Back:

- API delta 0 внутри TTL;
- visible DOM возвращается к baseline;
- `body.style.overflow` пуст;
- нет накопления document/window listeners, timers, intervals, rAF;
- CDP `Memory.getDOMCounters` не показывает структурный рост nodes/listeners;
- age/freshness UI сам по себе не вызывает API.

Reference G2: visible 319→319, CDP nodes 443→443, listeners 160→160, global
listeners 4→4, timers/intervals/rAF 0, post-GC heap +3.25% без структурного роста.

## T10 — long tasks и Status truth

1. Записать long-task trace при реальном scroll Activity и полном взаимодействии
   Risk. Повторяющихся app-generated task >50 ms быть не должно.
2. Cached System Status может появиться сразу, но weak/stale/expired evidence не
   выглядит fresh green.
3. Explicit `status(refresh=true)` измеряется отдельно от tap→cached-frame.

Reference G2: Activity+Risk long tasks >50 ms = 0, page errors = 0; пять signed
status reads 1.45–2.21 s, median 2.05 s.

## Что **не** блокирует G2

Следующие проверки нужны в S3.5/G3 и не должны превращаться в дефект S2 без нового
функционального регресса:

- реальные Telegram haptics на телефоне;
- native Telegram WebView BackButton на iOS/Android;
- физическая WebKit inertia;
- device A/B `scroll-snap-stop: normal` vs `always`;
- финальный UI/Closed/Trade detail/deep-link flows S3.

При обнаружении дефекта записать: exact route/screen, cold/warm состояние,
viewport/browser, network mode, число API вызовов, ожидаемый/фактический lifecycle
и воспроизводимость. Для race желательно сохранить sequence запросов и момент Back/
mutation/foreground, а не только screenshot.

## Фактическая Telegram-only перепроверка

Исходная матрица и baseline этого handoff сохранены как история S2/G2. После
S3 выполнена отдельная ручная перепроверка именно Mini App внутри Telegram
Desktop WebView: root/cache/revisit, stale Status/Aster, Notifications,
dirty Risk + Sheet-first Back, scroll, 50 navigation cycles и pause/resume.
Факты записаны в [MINIAPP_S2_PROGRESS_2026-09.md](MINIAPP_S2_PROGRESS_2026-09.md)
в разделе `Telegram WebView revalidation`, а сводная карта всех UI-поверхностей
и frontend-тестов — в
[MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).
