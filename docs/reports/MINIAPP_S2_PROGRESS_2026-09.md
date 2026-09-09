# Mini App S2 — скорость текущих экранов (2026-09)

Статус: **S2 IMPLEMENTED / G2 ACCEPTED (2026-09-07)**. Ветка
`feature/frontend`, исходный HEAD S2 — `b10f01a`. G1 закрыт на corrective
commit `84cabfb`; S2 не переоткрывает backend-историю и не начинает
S3-редизайн.

Источник требований и архитектуры:
[MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md](MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md),
§14 S2 и §19. Текущий визуальный состав четырёх root-вкладок сохраняется.

## Зафиксированные границы

- Оставить mount-all/hidden keep-alive root-вкладок и их локальный UI state.
- Убрать владение серверными DTO из времени жизни отдельных экранов: один
  session-scoped resource owner выше `AppShell`, без localStorage, Service Worker,
  React Query или глобального singleton.
- Scope кеша не строится по `account_masked`: owner живёт только внутри
  подтверждённой Mini App session (`user_id + environment + session generation`)
  и полностью уничтожается при disconnect/reconnect/session exit.
- `refresh=true` и дешёвое cached-read (`refresh=false`) — разные силы чтения:
  strong не присоединяется к уже идущему weak; strong-result имеет приоритет.
- Mutation response — подтверждённая серверная истина. Любой read, начатый до
  mutation barrier, не имеет права перезаписать её после завершения.
- Invalidation во время in-flight не теряется: максимум один follow-up read при
  наличии активного потребителя; ошибка не создаёт polling-loop.
- Nested Settings получают server DTO из bounded session cache. Prefetch только
  после фактического входа в Settings, не более двух запросов одновременно;
  скрытый prefetch никогда не запускает signed `refresh=true` проверки.
- Risk draft, confirmation tokens, wallet/signature state и Sheet state не кешируются.
- Уже принятая browser-часть RiskRuler S2.5/S3.4 сохраняется; device-зависимые
  haptics/WebKit/Telegram-жесты остаются в G3, как зафиксировано ранее.
- N4/N5/N6 из corrective G1 не затягиваются в S2: их deadlines остаются прежними.

## TODO выполнения

- [x] **S2.1 / F0 — baseline.** Зафиксировать текущие request budgets, cold/warm
  root и nested Settings, production build environment и воспроизводимые команды.
- [x] **S2.2 / F1 — transport + resources.** Держать timeout до завершения body,
  удалять внешний abort-listener, fail-safe `invalid_response`; добавить typed
  `resources.ts`/`useResource.ts`: TTL, weak/strong singleflight, generations,
  pending invalidation, scope disposal и owner-level mutation locks.
- [x] **S2.3 / F2 — root data ownership.** Перевести Dashboard и Positions на
  общий owner; Home остаётся последовательным dashboard(true) → positions(false),
  но Positions/Settings присоединяются к тем же DTO вместо дублирующих reads.
  Исправить StrictMode abort/setup и сделать foreground/TTL одним механизмом.
- [x] **S2.4 / F3 — быстрые nested Settings.** Кешировать Risk/Notifications/
  Status/AccountBuilder DTO, сделать bounded prefetch (max 2), click-priority/join,
  strong Status/AccountBuilder только при явной проверке; mutation result обновляет
  кеш и инвалидирует матрицу зависимостей.
- [x] **S2.5 / F4–F5 — lifecycle navigation.** Сохранить root scroll, добавить
  корректный nested scroll/focus lifecycle и cleanup listeners/timers; прогнать
  Risk pointer/wheel/cancel/save regressions. Оптимизировать rendering только при
  измеренном hotspot.
- [x] **S2.6 / F6 — regression + measurement.** Покрыть StrictMode, early tap,
  TTL, invalidation-during-flight, old-read-after-save, Back-during-save,
  strong-vs-weak, slow body, scope disposal и 50 navigation cycles. Повторить
  production build measurement и сверить §19.7.
- [x] **G2 evidence.** Обновить этот журнал, основной functional-progress,
  `ENGINE_TEST_STATUS_2026-08.md`, `DEV_PLAN_SKELETON.md` и `miniapp/AGENTS.md`;
  провести независимый blocker-only review и устранить найденные блокеры.

## Baseline до S2-кода

Исторический последний Chromium measurement текущего keep-alive поколения
зафиксирован в [MINIAPP_TAB_PERF_2026-09.md](MINIAPP_TAB_PERF_2026-09.md): после
warm-up первый переход между root экранами занимает 2–5 ms без блокирующего
запроса, но startup budget равен **6 POST** (`session` + Home 2 + warmup 3).
Nested Settings всё ещё монтируются заново и повторяют initial read.

Повторный локальный baseline gate на `b10f01a` сначала не стартовал из-за
pnpm safety prompt на переустановку `node_modules` в non-TTY. Это окружение,
не test failure; повтор выполняется с `CI=true`, без изменения исходников.

## Ход работ

### Анализ перед реализацией

Подтверждены дефекты из §19 на current HEAD:

- `api.post()` снимает timeout сразу после получения headers, до `response.json()`;
- внешний `AbortSignal` регистрируется anonymous listener и не отписывается;
- успешный HTTP с невалидным JSON возвращается как `T` через cast;
- `PositionsScreen`/`SettingsScreen` имеют `startedRef`: StrictMode cleanup может
  abort-нуть первый read, а второй setup уже не стартует;
- `useWarmRevalidation` не зависит от завершения busy/in-flight, поэтому
  invalidation, пришедшая во время запроса, может ждать следующей активации;
- Home/Settings читают Dashboard отдельно; Home-preview и Positions читают
  Positions отдельно; текущий startup budget подтверждён тестом как 6 POST;
- Risk/Notifications/SystemStatus/AccountBuilder держат initial DTO только в
  component lifetime, поэтому Back → reopen снова показывает skeleton и делает read;
- Home pause/resume и nested mutations защищены локальным component state/ref,
  а не session owner: после unmount/re-enter lock исчезает; старый background read
  также не имеет общего write-generation barrier.

## Реализованная архитектура S2

`App` теперь владеет одним `MiniAppResources` на подтверждённую connected-session.
Scope строится из `user_id + environment + локальная session generation`, а не из
маски адреса. Disconnect/reconnect, invalid session и явный выход уничтожают весь
owner; поздние ответы старого scope не публикуются.

`resources.ts` содержит фиксированный typed cache для Dashboard, Positions, Risk,
Notifications, Status и AccountBuilder. Weak (`refresh=false`) может присоединиться
к strong (`refresh=true`), strong никогда не присоединяется к weak. Reads несут
generation/write/invalidation fences; mutation response публикуется через
`publishConfirmed`, поэтому pre-write read не может затереть подтверждённую запись.
Invalidation во время запроса остаётся pending и даёт не более одного follow-up;
ошибка не создаёт автоматический retry-loop. Начало обязательного stale/strong read
сразу переводит сохранённый DTO в stale presentation, не стирая его.

`useResource.ts` использует `useSyncExternalStore`; StrictMode cleanup/setup не
оставляет экран в вечном loading и один consumer не отменяет общий flight другого.
Foreground/focus/visibility сигнал принадлежит shell, а не каждому экрану.

Home и Positions используют один `PositionPage`, Home и Settings — один Dashboard.
Штатный cold path: `session → dashboard(true) → positions(false)` плюс Activity.
Ранний переход в Positions до завершения strong Dashboard имеет отдельную
correctness-семантику: первый weak Positions может относиться к старому backend
snapshot, поэтому после strong Dashboard разрешён ровно один weak follow-up; третьего
read нет.

Settings запускает speculative pass только после реального входа пользователя:
Risk → Notifications → Status(false) → AccountBuilder(false), не более двух
одновременно. Скрытый Settings не запускает nested reads; клик приоритизирует/join'ит
цель. Status и AccountBuilder signed/network проверки (`true`) остаются explicit;
cached DTO даёт первый кадр, но weak/stale/expired evidence никогда не выглядит fresh.

Mutation locks Risk/Notifications/pause-resume/AccountBuilder живут в session owner и
переживают Back/remount. Ambiguous timeout/network после Risk/Notifications,
pause/resume, reauthorize-submit и disconnect-confirm сначала проверяется
authoritative read'ом; mutation автоматически не повторяется. Для всех четырёх
mutation owners reconciliation сначала инвалидирует соответствующий resource:
strong read, начатый до mutation outcome boundary, причинно слишком стар и не может
разблокировать owner; `refreshCurrent` при необходимости делает ровно один новый
post-boundary strong follow-up. Если неизвестны и mutation outcome, и первая
reconciliation, owner переходит в `unknown` и остаётся fail-closed через
Back/remount; снять `unknown` может только более позднее authoritative evidence
(явный refresh/reactivation/foreground) либо уничтожение session scope — blind
replay нет. Dirty Risk хранит draft отдельно от server truth; внешний policy update не
переписывает выбор молча и блокирует save, если шаг перестал быть допустимым.
AccountBuilder Agent expiry имеет one-shot semantic deadline без polling.
Resource reads и все direct connected paths Activity/Risk/Notifications/Home/
AccountBuilder направляют `401`/`invalid_session` в единый hard scope boundary:
owner dispose + возврат приложения в session gate ровно один раз.

Навигация сохраняет четыре root slots и теперь имеет отдельные scroll/focus slots
для каждого nested stack entry. Видимый Sheet является верхним уровнем Back: native
Back сначала закрывает discard/disconnect Sheet, затем nested screen. При скрытии
Home confirmation Sheet снимается, поэтому body/focus listener не остаётся невидимым.

## Acceptance / regression evidence

Постоянные frontend regressions покрывают обязательную матрицу §19.7:

- slow body после быстрых headers, external abort cleanup и `invalid_response`;
- StrictMode initial read, shared flight двух consumers и scope disposal;
- weak/strong ordering, late weak/strong, old read после mutation, invalidation
  во время flight и failure без self-loop;
- normal cold sharing и early Positions correctness follow-up;
- hidden Settings = 0 nested prefetch, visible max-2, weak-only signed separation,
  early-click join и cached revisit без skeleton;
- Back во время Risk/Notifications save → remount без второго POST;
- ambiguous Risk/Notifications/pause/resume/reauthorize/disconnect → authoritative
  reconciliation; pre-mutation resource flight fenced, double-unknown
  остаётся locked через remount до более позднего authoritative recovery без blind
  replay;
- direct connected mutation/read `401`/`invalid_session` → hard scope disposal
  для Activity/Risk/Notifications/Home/AccountBuilder;
- dirty Risk Back/tab guard и внешний policy conflict;
- Status weak/stale/aged strong остаётся amber до завершения strong verification;
- AccountBuilder expiry на уже открытом экране, reauth/disconnect admission lock
  через remount, Sheet-first native Back;
- 50 Settings → Risk → Back циклов: одинаковый DOM, те же API counts, ноль
  resource in-flight; отдельный 50-scope regression abort'ит каждый старый owner
  и отвергает позднюю публикацию.

Финальный frontend gate 2026-09-07:

```text
cd miniapp && CI=true pnpm test
  27 files / 316 passed
cd miniapp && CI=true pnpm lint
  0 warnings / 0 errors
cd miniapp && CI=true pnpm build
  PASS (tsc -b + Vite production build)
```

Основной JS production artifact на финальном build: **336.56 kB raw /
96.46 kB gzip**, CSS **19.42 / 4.41 kB**. Wallet/provider остаются отдельными
lazy chunks; S2 не добавлял router/query/motion framework.

## G2 measured evidence — production bundle

Дата: **2026-09-07**. Стенд: `scripts/preview_miniapp_engine.py --account copyc1
--confirm-engine-preview --port 8086`, SQLite backup prod DB, testnet engine,
production `miniapp/dist` с dev-session только для локального стенда. Browser:
Headless Chrome **152**, viewport **390×844**, CDP measurement. Это реальный frontend
bundle + HTTP/application path; не jsdom fixture.

Важно для provenance: эти CDP latency/DOM/heap числа записаны **до** последних
post-audit corrective transport validation, last-consumer cancellation/backoff и
Settings priority edges. Финальный automated gate и bundle size выше уже относятся
к current tree; corrective не меняют штатную cold/warm data-flow форму, но отдельный
новый CDP trace после них не выдаётся здесь за выполненный.

Cold startup дал ровно **4 read POST**:
`session`, `dashboard`, `positions`, `activity`. Первый реальный вход Settings —
tap→content **10.9 ms**; speculative nested peak **2**; после прохода появились
ровно Risk/Notifications/Status/AccountBuilder reads. Strong Status verification
добавил отдельный `/status`, как и требует split weak/strong.

Warm root: по 40 переходов на каждую из четырёх вкладок, **160 samples**:

| Root | p95 tap→content | max |
|---|---:|---:|
| Positions | 0.8 ms | 1.7 ms |
| Activity | 1.2 ms | 2.1 ms |
| Settings | 0.9 ms | 2.8 ms |
| Home | 0.9 ms | 2.8 ms |
| **Все** | **1.0 ms** | **2.8 ms** |

За все 160 warm root transitions — **0 API reads**.

Warm cached nested: по 30 повторов на Risk/Notifications/Status/AccountBuilder,
**120 samples**. Общий p95 **1.9 ms**, max **2.5 ms**, **0 API reads**. По экранам
p95: Risk 1.8 ms, Notifications 1.1 ms, Status 2.5 ms, AccountBuilder 1.2 ms.
Это существенно ниже бюджета ≤100 ms.

CDP network throttle с **300 ms latency**: ещё 160 warm root transitions дали
p95 **6.0 ms**, max 6.1 ms, **0 API reads**. Тем самым cached frame измеренно не
зависит от RTT.

50 browser Settings→Risk→Back cycles после warmup: visible DOM **319→319**,
API delta **0**, body overflow lock пуст в финале. Отдельный Chrome
`Memory.getDOMCounters` после 50 warmup + ещё 50 измеряемых циклов и принудительного
GC: documents **1→1**, nodes **443→443**, JS event listeners **160→160**,
visible DOM **319→319**, backing storage без роста; JS used heap изменился
2,649,560 → 2,735,748 bytes (**+3.25% / 84.2 KiB**), без структурного DOM/listener
роста. Global listener/timer probe: listeners **4→4**, timers/intervals/rAF **0**.

Activity-scroll + full-range Risk interaction trace: **0 long tasks >50 ms**, 0 page
errors. Ранее принятая отдельная Risk browser acceptance остаётся 15/15 PASS и
независимо перепроверена в `MINIAPP_RISK_RULER_2026-09.md`.

Отдельно измерена не fixture-скорость explicit strong System Status через тот же
testnet application path: 5 последовательных `status(refresh=true)` дали
**1.45–2.21 s**, median **2.05 s**. Ответы имели `age_seconds=0`, Aster `ok` и
`permissions_state=valid`; runtime verdict в отдельных samples честно менялся
между `ready` и `degraded` вместе с фактическим system/copy state. Это подтверждает
разделение двух метрик §19.7: cached tap→content не ждёт этот signed round-trip,
а strong truth приходит отдельно и до ответа не выдаётся за fresh green state.

Физические Telegram/WebKit/haptics/A-B `scroll-snap-stop`, которые прежний Risk
handoff уже явно перенёс в S3.5/G3, этим G2 не переобъявляются выполненными. В
текущем окружении подключённых iOS/Android устройств нет; G2 принимает именно
существующий UI/data-lifecycle по production bundle, а device-only финальная
приёмка остаётся в G3 согласно §14/§21.

После первого blocker-only pass владелец потребовал ещё один независимый аудит в
трёх новых областях. Он дополнительно нашёл и закрыл: runtime DTO validation для
непустого wrong-shape HTTP 200, timeout/external abort precedence, backoff и
last-consumer release с mutation protection, Settings priority/cancellation edge,
reopen-at-top/per-entry scroll-map cleanup, focus restore regression, Telegram
boundary и late-unknown dependency invalidation.

Финальный независимый audit ledger на current WIP перед commit/push:

- **Part A — transport/resources/mutation safety: PASS, P0/P1/P2 нет.** После
  corrective replacement-аудитор отдельно перепроверил endpoint-specific DTO
  validators, malformed nested rows/closed enums, first-wins abort classification
  и resource regressions: focused 7 files / 95 tests PASS; full 27/316 PASS;
  oxlint 0/0.
- **Part B — shell/navigation/lifecycle: PASS, P0/P1/P2 нет.** Подтверждены
  keep-alive четырёх root, StrictMode/shared-read semantics, mutation Back/remount,
  scroll/focus cleanup, reopen-at-top, Sheet-first Back и Telegram boundary.
- **Part C — Settings/prefetch/performance/docs: PASS, P0/P1/P2 нет.** Подтверждены
  max-2, user-target priority/admission, last-consumer cancellation interaction и
  requeue только cancellation/fenced/no-error; реальная network failure не получает
  automatic retry.

Текущий automated gate после этих corrective — **27/316**. Browser/CDP числа выше
остаются честно помеченным reference evidence: после последних correctness-only
изменений новый CDP trace не запускался.

## Итог

S2.1–S2.6 выполнены, обязательные regressions и measured budgets §19.7 пройдены.
Штатный cold budget улучшен **6 → 4 POST**, warm root/nested укладываются в ≤100 ms
с большим запасом, повторные cached переходы не ждут сеть, bounded prefetch и
session lifecycle подтверждены тестами и браузерными измерениями. **G2 ACCEPTED.**
Следующий этап кампании — S3/G3; S2 не менял визуальный состав Home и не включал
новые history/detail screens раньше их S3-пакетов.

## Telegram WebView revalidation — S2 Mini App only (2026-09-09)

После S3-прохода выполнена отдельная ручная перепроверка именно Mini App внутри
открытого Telegram Desktop WebView. Этот раздел не заявляет новый backend gate и
не подменяет мобильные iOS/Android проверки.

Проверено в Telegram:

- root-переходы `Главная → Позиции → Настройки → Главная`; flat Home показал
  `0,00 USDT`, `Открытые позиции 0`, риск `2%`, `Копирование включено`;
- Settings prefetch/revisit: Risk, Notifications, Status и Aster открываются;
  повторный Risk показывал текущие `2,0%` без skeleton, Status/Aster честно
  отображали stale/aged состояние, без ложного fresh-green;
- Notifications прочитаны без клика по toggle: доставка выключена, очередь `0`,
  ранее не доставлено `0`;
- Risk dirty draft `2,0% → 2,5%`: Back показал Sheet «Отменить изменения?»;
  выход без сохранения вернул Settings, повторный Risk снова показал серверные
  `2,0%`, финансовая настройка не изменилась;
- native Back из nested Risk/Status/Aster и root navigation отрабатывают;
  scroll Risk до нижней части и возврат в Settings не оставили экран в сломанном
  состоянии;
- **50 циклов** `Settings → Risk → Back` завершились на Settings, без зависания
  и без изменения server value; это визуальный Telegram lifecycle smoke,
  сетевые POST/heap/listener counters в Telegram Desktop этим проходом не
  инструментировались;
- pause/resume mutation flow: pause показал `Новые входы на паузе`, Resume Sheet
  открылся после async-загрузки, `Отмена` сохранила paused; повторный Resume с
  отдельным подтверждением после ожидания вернул `Копирование включено`;
  быстрый повторный тап во время занятой mutation не создал дополнительного
  перехода состояния.

Финальная сверка после Telegram-прохода: `risk_percent=0.02`,
`allow_new_entries=1`, владелец `notifications_enabled=0`, история `170`
episodes и `487` fills, execution commands в состояниях PENDING/SENDING/
SUBMITTED/UNKNOWN — `0`. Последняя фактическая пара pause/resume записала ровно
один `COPY_PAUSED` и один `COPY_RESUMED`; всего в живом аккаунте после всех
сегодняшних ручных прогонов — по `3` таких события. Dirty Risk изменений в БД
нет.

S2 Mini App Telegram smoke — **PASS** по перечисленным UI/lifecycle сценариям.
G2 общий статус остаётся **ACCEPTED** по уже записанному automated/CDP evidence;
доступные mobile-only WebKit/haptics и transport/heap измерения не приписываются
этому Telegram Desktop проходу.

Сводная карта всех UI-проверок Mini App, включая этот Telegram-only проход,
Chromium evidence и список frontend-тестов, —
[MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).
