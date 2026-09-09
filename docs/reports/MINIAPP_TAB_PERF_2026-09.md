# Mini App: тёплое переключение вкладок — отчёт приёмки (2026-09)

## Цель

Убрать cold-mount при каждом возврате на root-вкладку (`home`, `positions`,
`activity`, `settings`): раньше `AppShell` монтировал ровно один экран, возврат
означал новый инстанс + skeleton + 1–2 HTTP-запроса. Цель: тёплый переход
<100 мс воспринимаемых, 0 блокирующих запросов, сохранение state/scroll.

## Что изменено

- `miniapp/src/components/AppShell.tsx` — lazy first mount (`visited`-множество),
  все посещённые табы живут одновременно в `hidden`-обёртках; nested-экран
  скрывает, но не размонтирует root; per-tab `scrollTop` (save в `handleTab`,
  restore в `useLayoutEffect`).
- 4 root-экрана — seq-guard строго по образцу `SystemStatusScreen` (`seq`/`my`,
  ранний `return` перед каждым `setLoad`, включая append-ветку Activity) +
  `AbortController` (предыдущий абортится, сигнал третьим аргументом,
  `ApiError(code='aborted')` молча выходит).
- `HomeScreen` — partial-ready (`positionsPending`: Dashboard рисуется до
  второго запроса, порядок dashboard→positions строго последовательный),
  проп `active` + `HOME_REVALIDATE_MS = 30_000`: фоновая ревалидация только
  при переходе в `active` и протухшем снапшоте, провал кладёт текст в
  error-слот без затирания UI.
- `miniapp/src/appShellKeepAlive.test.tsx` — 7 поведенческих кейсов (jsdom).

Backend не тронут. `navigation.ts`, контракты API, trust-семантика — без изменений.

## Замеры (Chromium, engine-стенд `preview_miniapp_engine.py`, аккаунт `copyc1`)

До: каждый переход платил цену cold-mount (skeleton + 1–2 запроса, ~100–2000 мс
в зависимости от свежести снапшота). После:

| Переход | Cold (1-й визит): tap→content / запросы | Warm (повтор): tap→content / запросы / skeleton |
|---|---|---|
| home | 2056 мс / `dashboard(true)` + `positions(false)` | 9 мс / 0 / нет |
| positions | 104 мс / `positions(false)` | 2 мс / 0 / нет |
| activity | 105 мс / `activity(all,0)` | 3 мс / 0 / нет |
| settings | 104 мс / `dashboard(false)` | 2 мс / 0 / нет |

Дополнительно: 1,5 с простоя после cold-круга — 0 фоновых запросов
(всего 5 POST за сессию: 2 home + по 1 на таб; `session` ушёл до установки
счётчика). Mount — 1 на таб за жизнь приложения (проверено jsdom-счётчиками
запросов на обратных переходах). Scroll/state/filter/page сохраняются
(кейсы б–г).

Скриншоты: `data/miniapp-perf/{cold,warm}-{home,positions,activity,settings}.png`
(8 шт., webp). Warm-кадры попиксельно повторяют cold (контент тот же, скелетонов нет).

## Тестовые доказательства

Гейт: `cd miniapp && pnpm lint && pnpm test && pnpm build` — зелёный.
`vitest`: 18 файлов / 175 тестов (168 существовавших без правок + 7 новых).
Новые кейсы: round-trip без рефетча; Activity filter/page через детур;
scrollTop 142 → restore; nested risk поверх Settings + BackButton-назад без
повторного `dashboard(false)`; seq-guard (deferred-промисы, побеждает новый);
partial-ready (Dashboard видим, превью pending); warm-ревалидация только
по протуханию (ровно один фоновый `dashboard(auth, true)`).

## Найденный и исправленный дефект

Первая версия вешала warm-эффект на `[active, load, refreshBackground]`:
промежуточный partial-ready `setLoad` переворачивал `load` при ещё старом
`loadedAt` и порождал новый фоновый refresh — самоподдерживающийся цикл
(dashboard→positions→dashboard…, 5 POST за 2,4 с простоя в браузере,
негативный контроль в jsdom: 3 вызова `dashboard` вместо 1). Исправление —
эффект строго на `[active]` (+ стабильный `refreshBackground`), `load`
читается через `loadRef`; частичные состояния цикл не перезапускают.
Регрессия покрыта счётчиками в partial-кейсе.

Отклонение от плана: кейс (а) — `api.positions` итого 2 вызова за круг
(превью Home + cold-mount Positions), на обратном переходе 0 новых;
буквальное «positions 1 раз» несовместимо с требуемым превью-запросом,
намерение (0 блокирующих запросов на warm) соблюдено.

## Остаток для ручной приёмки (не блокирует мердж)

Telegram WebView iOS/Android: нативный трейс переключения, свайп Risk-линейки,
safe-area и BackButton-жесты; десктопный Chromium этого не покрывает.
Дистрибутив `miniapp/dist` собран с `VITE_DEV_USER_ID=900000001` только для
стенда — прод-сборка без этой переменной (гейт по умолчанию).

## Часть 2A — прогрев всех вкладок + ревалидация по активации (2026-09-04)

Смена цели (фиксируется здесь): прежняя цель «0 фоновых запросов» была целью
того гейта, не инвариантом. Новая цель: **0 блокирующих запросов на любой
переход, включая первый**; данные не протухают; сервер не нагружается.
Фоновый прогрев и ревалидация — легитимный трафик.

### Что изменено

- `miniapp/src/revalidate.ts` — общий хук `useWarmRevalidation` (active-only
  триггер, TTL-пол, busy-guard, split retry/revalidate) + `HOME_REVALIDATE_MS`
  (30 c, переиспользуется positions/settings) и `ACTIVITY_REVALIDATE_MS` (60 c).
  Клиентские полы не ниже серверного TTL (`max(2×reconcile, 30)` ≥ 30 c).
- `AppShell` — mount-all: все 4 экрана монтируются сразу в `hidden`-обёртках
  (lazy-`visited` для рендера убран); флаг `warmReady` поднимается, когда Home
  впервые доходит до ready (его `dashboard(true)` наполняет кэш-снапшот —
  дешёвые чтения остальных вкладок до этого дали бы пустой/is_stale кадр).
- Стартовый фетч Positions/Activity/Settings — при `warmReady` или тапе
  пользователя, что раньше; однократно; ранний тап не ждёт Home (прежний cold).
- Все 4 экрана ревалидируются только переходом active false→true при
  протухании; скелетон не показывается; провал фона не затирает UI (у Home —
  в error-слот, у остальных — тихо с авто-ретраем на следующей протухшей
  активации); без поллинга, без прогрева страниц дальше page 0.
- Ревью-фиксы той же серии: `_admin_ids` читает `.env`-fallback, fail-fast
  проверки `dev-up.sh` до `stop_instance`, скролл-слоты по семантике видимой
  поверхности, дедупликация `refresh/refreshBackground` в единый `fetchAll`,
  стабильный `onNotConnected` в `App`, честность error-слота задокументирована.

### Замеры first-visit (Chromium, тот же engine-стенд, аккаунт `copyc1`)

Стартовый бюджет сошёлся с проектным: `session` 1 + Home 2
(`dashboard(true)` + `positions(false)`) + прогрев 3 (`positions(false)`,
`activity(all,0)`, `dashboard(false)`) = **6 POST** за ~3,0 с (доминирует
cold-загрузка Home ~2 с подписанными REST к бирже — out of scope, не трогали).

| Первый визит | tap→content | Блокирующих запросов | Skeleton |
|---|---|---|---|
| positions | 5 мс | 0 | нет |
| activity | 5 мс | 0 | нет |
| settings | 4 мс | 0 | нет |
| home (возврат) | 2 мс | 0 | нет |

Повторные круги — те же 2–9 мс / 0 запросов (гейт Части 1 не регрессировал).
Кадры: `data/miniapp-perf/first-<tab>.png` (4 шт.) + прежние
`cold-/warm-<tab>.png`.

### Решения по скроллу и error-слоту

- Скролл: слот вкладки пишется, только когда она перестаёт быть видимой
  поверхностью (switch вне nested, открытие nested из неё); при видимом
  nested в слоты ничего не пишется; возврат восстанавливает. Покрыто двумя
  jsdom-кейсами с негативным контролем.
- Error-слот Home оставлен как есть: partial-ready не стирает видимую ошибку,
  провал фона только заполняет слот. Поведение не менялось.

### Тесты

Гейт `cd miniapp && pnpm lint && pnpm test && pnpm build` — зелёный, 18 файлов /
185 тестов (15 keep-alive кейсов: бюджет старта 6 через полный `App`, гейт
прогрева за Home-ready, ранний тап без дублей, TTL-полы 30/60 c, запрет
ревалидации при незавершённом фетче, ошибка прогрева → error UI + ретрай,
ревалидация не сбрасывает накопленную пагинацию Activity).
Backend не менялся — pytest не требовался; тронутый
`scripts/verify_menu_button.py` прошёл `ruff check` и `ruff format --check`.

### Решение: ревалидация Activity и пагинация

Тихая ревалидация страницы 0 заменяет накопленный таймлайн, поэтому она
применяется только пока пагинация не углублялась (`page === 0`). Таймлайн с
допрокрученными страницами ревалидацию пропускает (позиция читателя
сохраняется, счётчик запросов не растёт); следующая активация после TTL
повторяет пропуск. Журнал read-only, свежесть быстрее 60 c для него не
критична — решение зафиксировано тестом.

### Ручная приёмка перед мержем (обязательна)

Telegram WebView iOS/Android: трейс переключений, safe-area, BackButton-жесты,
свайп Risk-линейки. Десктопный Chromium этого не покрывает.

## S2/G2 final measurement — shared resources (2026-09-07)

После закрытия G1 старый Part 2A loader заменён S2 session-scoped resource owner.
Текущий источник фактов и полный regression ledger:
`MINIAPP_S2_PROGRESS_2026-09.md`. Ниже только performance delta к историческим
замерам этого файла.

Финальный frontend gate: **27 test files / 316 passed**, oxlint 0/0,
TypeScript + Vite production build PASS. Main JS **336.56 kB raw / 96.46 kB
gzip**, CSS **19.42 / 4.41 kB**.

Engine preview `copyc1`, production bundle с preview-only dev-user define,
Headless Chrome 152, 390×844, CDP:

Таблица ниже — recorded G2 browser run до последних post-audit transport/
cancellation corrective. Финальный current tree прошёл automated gate/build выше;
новый CDP latency/DOM rerun после corrective в этом документе не заявляется.

| Проверка | Финал S2 |
|---|---:|
| Cold startup | **4 POST**: session + dashboard + positions + activity |
| First Settings tap→content | **10.9 ms**, nested prefetch peak **2** |
| Warm roots | **160 samples, p95 1.0 ms**, 0 API reads |
| Warm cached nested | **120 samples, p95 1.9 ms**, 0 API reads |
| Warm roots при +300 ms latency | **160 samples, p95 6.0 ms**, 0 API reads |
| 50 Settings→Risk→Back | DOM **319→319**, API delta 0 |
| Chrome DOM counters после GC | docs/nodes/listeners **1/443/160 → 1/443/160** |
| JS heap после GC | **+3.25% / ~84.2 KiB**, backing storage без роста |
| Trace Activity + Risk | **0 long tasks >50 ms, 0 page errors** |

Explicit signed `status(refresh=true)` на том же testnet application path не
скрывается за tap metric: 5 reads **1.45–2.21 s**, median **2.05 s**,
`age_seconds=0`, Aster `ok`, permissions valid. Поэтому S2 показывает cached
content мгновенно, но stale/weak evidence не выдаёт за fresh green во время
этой отдельной strong verification.

Итог: историческая цель keep-alive сохранена, но startup budget улучшен с 6 до
**4 POST**, а повторные nested Settings теперь также cached/RTT-independent.
Browser G2 CLOSED; физический Telegram/WebKit/device gate остаётся S3.5/G3 по
актуальному §19.7 функционального плана.
