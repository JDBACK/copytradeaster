# Miniapp Guidelines

Map of `miniapp/`: Vite 8 + React 19 + TypeScript follower SPA served by the bot
process at `/miniapp/`. Talks to the backend only via 27 POST `/miniapp/api/*`
(routes in `src/copytradeaster/miniapp/server.py`); all business logic lives in
`UserApplicationService`, never here.

## Project Overview

Follower Mini App (Stage 3 Part 2, CLOSED): 4-tab shell (`Главная`, `Позиции`,
`Активность`, `Настройки`) with nested Risk/Notifications/SystemStatus/
AccountBuilder/Security/Help plus TradeList/TradeDetail/PositionDetail screens,
plus an out-of-shell onboarding flow (session gate → builder wizard |
attempt recovery → success receipt → home shell).

```bash
cd miniapp && pnpm install && pnpm dev        # HMR dev, /miniapp/api proxied to 127.0.0.1:8080
cd miniapp && pnpm lint && pnpm test && pnpm build   # oxlint + vitest + tsc -b && vite build
```

## Architecture & Data Flow

```text
main.tsx -> App.tsx Phase machine (gate|wizard|attempt_recovery|success|home)
  -> sessionDestination(session.ts) maps MiniAppSessionDTO -> destination
  -> onboarding onConnected or session->home opens one MiniAppResources owner per session generation; success reuses it, home mounts AppShell with it
  -> AppShell (navigation.ts RouteState tab + nested[] stack) shares that owner
  -> screens/resources -> api.ts -> 27 POST /miniapp/api/* -> UserApplicationService
```

- Gate/Wizard/Recovery/Success render standalone (no bottom nav); only `home`
  mounts `AppShell`. Telegram BackButton is visible iff a nested screen is on
  top; visible Sheets are closed before the nested route is popped.
- Server DTO is the single source of truth. Mutating screens flip UI only on
  the server-confirmed response. Session-scoped mutation locks survive
  Back/remount; ambiguous network/timeout outcomes are reconciled by read and
  never blindly replayed. Reconciliation after an ambiguous write fences any
  resource read admitted before that write boundary; only post-boundary
  authoritative evidence may resolve the owner lock.
- `MiniAppResources` is a bounded per-connected-session owner, not a global
  store. Direct keys: Dashboard/Positions/Risk/RiskPreview/Notifications/
  Status/AccountBuilder; history (Trades/Feed/Detail/Events) lives in the
  child `resources.history` owner.
  Weak/strong reads, generations, invalidation and write barriers prevent stale
  responses overwriting confirmed mutations. `401`/`invalid_session` hard-disposes
  the owner and returns the application to the session gate.
- `telegram.ts` is the sole `window.Telegram` touchpoint (initData, setup,
  haptics, close, BackButton). Transport handlers know only application use
  cases + typed public codes — zero `aster`/`storage` imports server-side.

## Key Directories

### Shell, routing, flow — `src/` root + `screens/` flow + `components/`

| File | Purpose |
|---|---|
| `App.tsx` | Root composition: 5-kind `Phase` machine, `routeSession`/`loadSession`/`handleRecover`, dev banner. Backend: `session`, `builder/recover` |
| `main.tsx` | Bootstrap: StrictMode `createRoot` + `index.css`; no exports |
| `navigation.ts` | Pure typed nav: `ROOT_TABS` (home/positions/activity/settings, RU labels), `NestedScreen` (risk/notifications/system-status/account-builder/security/help + trade-list/trade-detail/position-detail), `initialRoute/selectTab/pushNested/popNested/currentNested` |
| `session.ts` | Server→UI router: `sessionDestination()` over `MiniAppSessionDTO` (approved: recovery_required → runtime_recovery beats account_connected/connected → home; onboarding + APPROVAL_SENT/APPROVED → attempt_recovery, else wizard incl. PREPARED; pending/rejected/invalid pass through) |
| `components/AppShell.tsx` | Shell: mount-all four tabs in `hidden` wrappers + `NestedRoute`; Back/Sheet ordering, one foreground signal, warm-gated tabs, root + nested scroll/focus slots; shared resource owner passed to resource-backed screens |
| `screens/SessionGate.tsx` | Read-only access gate (loading/pending/rejected/invalid/error/recovery_required) |
| `screens/SuccessScreen.tsx` | Post-onboarding receipt (`BuilderOnboardedDTO` rows; copying paused) |
| `screens/BuilderAttemptRecovery.tsx` | Durable-attempt resync without wallet re-prompt (`builder/resume-attempt` + `client-log`) |
| `screens/InfoScreens.tsx` | Static Help/Security explainers; never claims live state |

### Transport, wallet, onboarding — `src/` lib

| File | Purpose |
|---|---|
| `api.ts` | Typed POST client mirroring all 27 routes; initData headers (+ dev-only `X-Dev-User-Id`), 15s AbortController timeout covering headers + body, external-abort cleanup, safe `invalid_response`, `ApiError` |
| `resources.ts` | Fixed session-scoped DTO owner: TTL, weak/strong singleflight, read/write/invalidation fences, confirmed mutation publication, hard scope disposal, owner mutation locks |
| `historyResources.ts` / `historyResources.test.ts` | Session history cache and invalidation revision fence; regressions for late responses, coalesced follow-up and inactive reads |
| `useHistory.ts` | External-store subscription for history list/feed/detail/events + bounded self-heal retry of failed slices |
| `screens/TradeListScreen.tsx` / `screens/TradeDetailScreen.tsx` | S3 history lists, independent fills/events pagination, grouped executions, owned split choice and stale/partial detail states |
| `components/PositionSummaryCard.tsx` / `TradeSummaryCard.tsx` / `TradeEventRow.tsx` | S3 position, trade and event presentation |
| `useResource.ts` | `useSyncExternalStore` subscription/activation bridge over `MiniAppResources`; StrictMode-safe shared reads + foreground/TTL + bounded self-heal retry + `useTransportStale` (error or live-stale, never a routine re-read) |
| `live.ts` | Session-scoped live client: ticket → WebSocket, epoch-aware revision watermark, gap reconciliation, backoff reconnect with a silence grace before anything is shown, heartbeat/stale deadline, resource invalidation |
| `liveStatus.ts` | Pure rail copy: `LIVE`/`Подключаем`/`Восстанавливаем связь…`/`Нет связи · N сек назад` |
| `pullToRefresh.ts` | Pure damped pull tracker: slop, threshold, ownership rules, release/cancel semantics. The app's only manual refresh path |
| `resourceScopeContext.ts` | Connected-session owner context for direct screen-owned paths such as Activity so auth loss still hits the hard session boundary |
| `settingsPrefetch.ts` | Settings-only bounded speculative queue: max two concurrent weak reads; click priority/join; no hidden signed refresh |
| `wallet.ts` | Adapters: injected EIP-1193 first, lazy WalletConnect second; BSC-56 signing domain; typed `WalletError` (`VITE_WC_PROJECT_ID`) |
| `telegram.ts` | Sole Telegram wrapper: initData, setup, haptics, close, closing-confirm, BackButton registry, vertical-swipe handoff (Bot API 7.7+, optional) |
| `errors.ts` | Server-code → Russian `{title,hint}` map + FALLBACK; never leaks raw payloads |
| `types.ts` | DTO mirrors of backend `application/models.py` (session/dashboard/positions/activity+feed/risk+preview/notifications/status/confirmation/builder+reauthorize/account-builder/trade history+events/overview/resume) |
| `wizard.ts` | Pure onboarding machine: wallet→review→signing→sync + trust-rail index |
| `accountBuilder.ts` | Shared verdict (verified-only), state/reason copy, local-only disconnect copy |
| `revalidate.ts` | Unused warm-activation primitive (no callers; Activity reads via `useHistory` over HistoryResources): active-only TTL/invalidation/foreground edges, busy guard, retry/revalidate split; `HOME_REVALIDATE_MS` reused as `useResource` TTL, `ACTIVITY_REVALIDATE_MS` covered by keep-alive tests; resource-backed screens use `useResource` |
| `screens/BuilderWizard.tsx` | Onboarding screen: resolveAdapter → prepare → one combined `approveAgent` signature → submit |
| `screens/AccountBuilderScreen.tsx` | Read-only attestation: seq-guarded refresh, backend-gated reauthorize, local-only disconnect |

### Follower screens — `screens/` + pure view-models

| Screen | Contract | Backend routes |
|---|---|---|
| `HomeScreen.tsx` (+`home.ts`) | MUTATES pause/resume (+ confirm Sheet); account overview, freshness, compact copy state and contextual navigation | `dashboard`, `pause`, `resume`, `confirmation/begin` |
| `PositionsScreen.tsx` | READ-ONLY list + exported `PositionCard` (entry/now/stop/TP/risk-to-stop/leverage); refresh-only | `positions` |
| `ActivityScreen.tsx` (+`activityView.ts`) | READ-ONLY user-event feed, pending operations, pinned cursor pages and trade links. Row copy arrives ready-made from the backend presenter (`notifications/trade_cards.format_user_event`); the frontend never renders engine codes | `activity/feed` |
| `RiskScreen.tsx` (+`riskView.ts`) | MUTATES risk-on-Stop: draft/confirmed/save integration, exact 1e-5 integer math; ruler control lives in `components/RiskRuler.tsx` | `risk`, `risk/preview`, `risk/percent` |
| `SettingsScreen.tsx` (+`settingsView.ts`) | READ-ONLY capability list; bounded Settings prefetch (weak, max-2); navigation only | — (routes to nested) |
| `NotificationsScreen.tsx` (+`notificationsView.ts`) | MUTATES single toggle (`role=switch`), server-confirmed flip + double-tap gate | `notifications`, `notifications/enabled` |
| `SystemStatusScreen.tsx` (+`systemStatus.ts`) | READ-ONLY status slice, seq-guarded refresh, transport-stale honesty | `status` |
| `components/ui.tsx` | Shared `Brand/TrustRail/Sheet/Row/ErrorBox` (+ inline SVG icons) | — |
| `components/RiskRuler.tsx` | Snap-strip control: native touch/trackpad inertia, mouse drag w/ pointer capture, gated wheel, interruptible settle animator, scrollend+fallback settle, rate-limited selection haptics, save freeze | — |
| `components/LiveStatus.tsx` | Transport rail; renders only for the `stale` verdict (channel silent past its deadline). Healthy, connecting and reconnecting states render nothing | — |
| `components/PullToRefresh.tsx` | Shell-bound pull gesture over `.shell-content`: arms only at scroll top, claims the vertical drag from Telegram while armed, triggers one authoritative `resources.pullRefresh()` | — |
| `riskRuler.ts` | Pure ruler decisions: wheel deltaMode normalization + `wheelIntent` gating, ease-out/duration, clamp, haptic rate limit | — |
| `protectionWarning.ts` | Protection-state → Russian warning copy map | — |
| `format.ts` | USDT/fee/date/pnl formatting | — |

## Development Commands

```bash
cd miniapp
pnpm install          # deps (@walletconnect lazy chunk needs VITE_WC_PROJECT_ID at build time)
pnpm dev              # HMR; needs backend on :8080 (VITE_API_TARGET override) — or real bot via dev-up.sh
pnpm lint             # oxlint (no ESLint)
pnpm test             # vitest run; current counters and UI acceptance map are in docs/reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md
pnpm build            # tsc -b && vite build -> dist/ (git-ignored, served by bot at /miniapp/)
pnpm preview          # vite preview
```

Pairing with backend: `./scripts/dev-up.sh` (bundle + tunnel + bot), or
`python scripts/preview_miniapp_engine.py --account <ID> --confirm-engine-preview`
(engine-backed preview on a DB copy, browser-only).

## Code Conventions & Common Patterns

- **View-models are pure**: `home.ts`, `tradeView.ts`, `riskView.ts`, `activityView.ts`, `notificationsView.ts`, `systemStatus.ts`, `settingsView.ts`, `navigation.ts`, `session.ts`, `wizard.ts`, `trustSteps.ts` (step copy shared by Home/Status/wizard) — no fetch, no Telegram, unit-testable in node. Server DTO ownership lives in the session resource owner; component state is presentation/draft/interaction state, not an independent data store.
- **One voice per fact**: `railSpeaks()` (`live.ts`) keeps the shell rail as the only transport voice while last-good data is on screen; screens suppress their duplicate read-error box (never a failed action, never the empty-data case).
- **One voice per fact**: `railSpeaks()` (`live.ts`) keeps the shell rail as the only transport voice while last-good data is on screen; screens suppress their duplicate read-error box and their duplicate snapshot warning (never a failed action, never the empty-data case). `usePaging()` keeps "Загружаем…"/disabled feedback on the control the user pressed, so background revalidation never repaints a button. `useAgeSeconds()` (`useAge.ts`) keeps a server-sent age truthful while the screen stays open, and `needsPermissionReproof()` re-proves aged evidence on open instead of leaving a green verdict under an old stamp.
- **Read-only honesty**: read-only screens assert it in markup (no trading controls, no secrets/ids); mutating screens change confirmed values only.
- **No manual refresh in the product**: routine re-reads (live invalidation, TTL,
  reconcile, batch refetch) are invisible — `snapshot.stale` is a cache-revision
  flag and must never be painted as staleness. Transport verdicts come only from
  `useTransportStale` (failed request or the live channel past its stale
  deadline), the rail renders only a stale channel, and a failed read retries
  itself with bounded backoff. Evidence that merely *ages* (Agent/Builder
  verification) is the row's business, not an overall verdict: the Status screen
  re-verifies once on open instead of asking the user. The single manual path is
  pull-down at the top of the shell.
- **Secret-safety**: masked addresses only; `describeError` maps codes, raw payloads never render; wallet private key never enters the app.
- **Russian UI copy** (`ROOT_TAB_LABELS`, errors, info screens); inline SVG icons, no emoji; `:focus-visible`, semantic dialog roles; native BackButton/Haptics.
- **Naming**: `*.test.ts(x)` next to source; `*View.ts` = pure VM; `*Behavior.test.tsx` = jsdom interaction.

## Important Files

| File | Purpose |
|---|---|
| `src/App.tsx` | Phase machine, session routing, dev banner |
| `src/navigation.ts` | Tab + nested-stack state (no router dep) |
| `src/session.ts` | Session-DTO → destination router |
| `src/api.ts` | 27-method typed transport client |
| `src/wallet.ts` | Injected + WalletConnect adapters, chain 56 |
| `src/telegram.ts` | Sole Telegram boundary |
| `src/types.ts` | Backend DTO mirrors |
| `src/index.css` | Tokens: `--canvas #070707`, `--champagne #f2c98d`, safe-area/radius |
| `vite.config.ts` | `base: '/miniapp/'`, `/miniapp/api` proxy |
| `package.json` | `dev/build/lint/preview/test` scripts; React 19, Vite 8, TS ~6, Oxlint, Vitest |

## Runtime & Tooling Preferences

- Node (unpinned — use modern for Vite 8) + `pnpm` (lockfile 9.0).
- React 19 + TypeScript (`erasableSyntaxOnly`, `noUnused*`; strict not enabled) + Oxlint (react + typescript + oxc plugins; NO ESLint).
- Vitest `environment: 'node'`, `src/**/*.test.{ts,tsx}`; `dist/` git-ignored.
- Backend pairing on `127.0.0.1:8080` (`VITE_API_TARGET` override; `VITE_DEV_USER_ID` for unsigned dev sessions).

## Testing & QA

Frontend test structure and the manual Telegram WebView / Chromium acceptance
matrix are indexed in
`../docs/reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md`. The tests use three
styles:

- **Pure VM** (node, no rendering): `home/navigation/session/wallet/wizard/accountBuilder/systemStatus/format/riskRuler` tests — import helpers directly.
- **Static contract** (`renderToStaticMarkup`, effects never fire): `positions/risk/activity/notifications/settings` — loading frames + presentational views on canned DTOs, read-only/secret-free assertions.
- **Behavior** (jsdom + testing-library, mocked `api.*`): `resources`/`useResource` (singleflight, fencing, invalidation, scope disposal), `settingsNavigationPerformance` (weak prefetch/join/cached nested), `activityInvalidSession` + `notificationsBehavior` (direct hard-session boundary), `riskBehavior` (draft/save/unknown/auth), `riskRulerBehavior`, `systemStatusBehavior`, `pullToRefreshBehavior` (indicator, threshold, scrolled-away guard, swipe handoff, shell-wide strong read), `accountBuilderBehavior` (expiry/reauth/disconnect/unknown outcomes), `appShellKeepAlive` (startup four-read budget, StrictMode, early tap, TTL/foreground, scroll/focus/Sheet lifecycle, cross-remount locks, 50 cycles).
