# Stage 3 Part 2 — журнал прогресса

Дата: 2026-09-01

Ветка: `codex/stage-3-mini-app-part2` (baseline `main@660c1f5`).

## P2.0 — app shell и завершение Home: CLOSED (2026-09-01)

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

- frontend: vitest 53/53 (navigation и corrective trust/accessibility cases), oxlint 0/0,
  `pnpm build` зелёный;
- offline backend suite (regression): 740 passed, без изменений;
- визуальная проверка: `scripts/preview_miniapp_engine.py` (полный engine
  boot на копии prod БД `data/miniapp-preview.sqlite3`, dev-user 900000001 →
  account `tg-8264892202`, порт 8085). После fix: shell = высоте viewport,
  nav прибит к низу; все четыре tabs переключаются без перезагрузки и потери
  session; Home показывает реальные данные (`Всё работает`, система готова,
  риск 1%, позиций 0, свежий snapshot). Скриншоты: `/tmp/p2-shots/01..04`.

Отложено в общий финальный gate Part 2 и не блокирует переход к P2.1:

- реальные Pause/Resume из shell Home;
- safe-area/bottom nav на фактическом viewport;
- финальный screenshot current-HEAD.

## P2.1 — read-only Positions: CLOSED (2026-09-02, corrective gate passed)

Сделано:

- Backend boundary: `PositionSummaryDTO`, `PositionPageDTO`, `PositionSide`, `PositionProtectionState` в `src/copytradeaster/application/models.py`;
- `PositionState` в `domain/models.py` расширен безопасным полем `unrealized_profit: Decimal = Decimal(0)` с сохранением строгих decimal инвариантов;
- `Coordinator._snapshot` и `MasterProjector` заполняют `unrealized_profit` из биржевого `PositionDTO.un_realized_profit`;
- `AppStage2RuntimeAdapter` сохраняет точные `(AccountState, captured_at: datetime)` timestamps для кэша снепшотов; при сбое `refresh=True` происходит fallback на кэш с обязательным `is_stale=True` и `risk_to_stop_usdt=None`;
- `UserApplicationService.positions` и `_parse_position_summary`:
  - исключает нулевые позиции (`qty == 0`);
  - нормализует направления `LONG`/`SHORT` для `ONE_WAY` и `HEDGE`;
  - строгая валидация Stop-ордера: противоположная сторона позиции, флаги `reduce_only`/`close_position`, достаточность объема покрытия; при несоблюдении — статус `UNCONFIRMED` или `PARTIAL_STOP` и `risk_to_stop_usdt=None`;
  - Decimal-логика расчета `risk_to_stop_usdt` выполняется строго по свежему snapshot (`is_fresh=True`) и полностью подтвержденному Stop (с учетом `fee_buffer_pct`);
  - исключены сырые секреты, clientOrderId, account_id и orderId;
  - позиции сортируются по алфавиту `symbol`;
- Transport boundary: `POST /miniapp/api/positions` в `src/copytradeaster/miniapp/server.py` с валидацией Telegram `initData` / dev-testnet заголовков;
- Frontend:
  - `miniapp/src/types.ts` и `api.ts`: добавлены типы и `api.positions(...)`;
  - `miniapp/src/format.ts`: хелперы `formatPrice` и `formatPnl`;
  - `miniapp/src/index.css`: добавлены недостающие токены `--radius: 12px;` и `--surface-2: #1c1c1c;`;
  - `miniapp/src/screens/PositionsScreen.tsx`: полноценный экран по эталону `03-positions.png` (заголовок «Открытые позиции · N», карточки с PnL, входом, текущей ценой, Stop, TP, риском до Stop, плечом «N× авто», предупреждениями защиты, состояниями loading/error/empty/stale);
  - `miniapp/src/components/AppShell.tsx`: вкладка «Позиции» подключена к `PositionsScreen` с пробросом `accountMasked` (wallet chip);
  - `miniapp/src/screens/HomeScreen.tsx`: последовательный fetch (dashboard refresh -> positions cache read) без двойного signed snapshot, preview первой позиции из того же DTO, удален текст-заглушка «появится в следующей версии»;
- Gates:
  - Offline pytest suite: 742 passed, coverage 87%, ruff clean;
  - Frontend: vitest 58 passed (включая `positions.test.ts`), oxlint clean, pnpm build clean;
  - In-browser walkthrough: в mobile viewport проверены пустой экран и реальная карточка открытой позиции с предупреждением;
  - Real Aster Testnet live acceptance (`tests/testnet/test_live_positions_acceptance.py`): 8/8 фаз passed через **реальный production copy engine** (`App`, `MasterProjector`, `Coordinator`, `Executor`, `AppStage2RuntimeAdapter`, `create_miniapp_app`) (open + stop, engine copy, positions query, сравнение с positionRisk/openOrders, перестановка Stop/TP, partial close, warning при снятии Stop, cleanup flat).

## Следующие подчасти

- P2.2 — Activity (`04-activity`).
- P2.3–P2.7 по адаптированному плану.

Открытые решения владельца (нужны перед соответствующими подчастями):

1. точные `risk min/max/step/presets` (перед P2.4);
2. disconnect semantics: только локальный или + отдельный remote revoke
   (перед P2.6);
3. auth-модель `/builder/userTrades` — bounded live probe (перед P2.7).
