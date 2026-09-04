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
