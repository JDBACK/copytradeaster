# Stage 3 Part 2 — журнал прогресса

Дата: 2026-09-01

Ветка: `codex/stage-3-mini-app-part2` (baseline `main@660c1f5`).

## P2.0 — app shell и завершение Home: РЕАЛИЗОВАНО (2026-09-01)

Сделано:

- typed route state без router dependency: `miniapp/src/navigation.ts`
  (ровно четыре root tabs `Главная/Позиции/Активность/Настройки`, nested
  stack для Risk/Notifications/System Status/Account+Builder/Help);
- `miniapp/src/components/AppShell.tsx`: постоянный bottom navigation,
  Telegram BackButton для nested screens, SVG-иконки, haptics;
- Gate/Wizard/Recovery/Success остались вне shell (`miniapp/src/App.tsx`);
- Home перенесён в shell без изменения логики Pause/Resume/freshness;
  compact verified trust summary (Wallet/Agent/Builder/Sync) сохранён;
  placeholder «появится в следующей версии» убирается в P2.1 вместе с
  подключением Positions DTO (по плану);
- tabs Positions/Activity/Settings — interim stub screens до P2.1–P2.3;
- CSS: `.shell`/`.bottom-nav`/`.nav-item`/trust-strip в `index.css`.

Найден и исправлен layout-баг при визуальной проверке:

- `.shell` имел `flex: 1` внутри `#root` (flex column, `min-height: 100dvh`);
  `flex-basis: 0%` перекрывал `height: 100dvh`, shell растягивался контентом
  (869px при viewport 577px) и bottom nav уходил ниже сгиба. Исправлено:
  `.shell` задаёт собственную definite height `100dvh` без `flex: 1`.

Gates:

- frontend: vitest 50/50 (включая `navigation.test.ts`), oxlint 0/0,
  `pnpm build` зелёный;
- offline backend suite (regression): 740 passed, без изменений;
- визуальная проверка: `scripts/preview_miniapp_engine.py` (полный engine
  boot на копии prod БД `data/miniapp-preview.sqlite3`, dev-user 900000001 →
  account `tg-8264892202`, порт 8085). После fix: shell = высоте viewport,
  nav прибит к низу; все четыре tabs переключаются без перезагрузки и потери
  session; Home показывает реальные данные (`Всё работает`, система готова,
  риск 1%, позиций 0, свежий snapshot). Скриншоты: `/tmp/p2-shots/01..04`.

Осталось по live gate P2.0 (за владельцем, в Telegram WebView на телефоне):

- реальные Pause/Resume из shell Home;
- safe-area/bottom nav на фактическом viewport;
- финальный screenshot current-HEAD.

## Следующие подчасти

- P2.1 read-only Positions (DTO → API → экран, Home preview из того же DTO).
- P2.2–P2.7 по адаптированному плану.

Открытые решения владельца (нужны перед соответствующими подчастями):

1. точные `risk min/max/step/presets` (перед P2.4);
2. disconnect semantics: только локальный или + отдельный remote revoke
   (перед P2.6);
3. auth-модель `/builder/userTrades` — bounded live probe (перед P2.7).
