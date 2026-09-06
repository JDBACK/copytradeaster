# Miniapp Guidelines

Map of `miniapp/`: Vite 8 + React 19 + TypeScript follower SPA served by the bot
process at `/miniapp/`. Talks to the backend only via 26 POST `/miniapp/api/*`
(routes in `src/copytradeaster/miniapp/server.py`); all business logic lives in
`UserApplicationService`, never here.

## Project Overview

Follower Mini App (Stage 3 Part 2, CLOSED): 4-tab shell (`Главная`, `Позиции`,
`Активность`, `Настройки`) with nested Risk/Notifications/SystemStatus/
AccountBuilder/Security/Help screens, plus an out-of-shell onboarding flow
(session gate → builder wizard → attempt recovery → success receipt).

```bash
cd miniapp && pnpm install && pnpm dev        # HMR dev, /miniapp/api proxied to 127.0.0.1:8080
cd miniapp && pnpm lint && pnpm test && pnpm build   # oxlint + vitest + tsc -b && vite build
```

## Architecture & Data Flow

```text
main.tsx -> App.tsx Phase machine (gate|wizard|attempt_recovery|success|home)
  -> sessionDestination(session.ts) maps MiniAppSessionDTO -> destination
  -> home mounts AppShell (navigation.ts RouteState tab + nested[] stack)
  -> screens fetch via api.ts -> 26 POST /miniapp/api/* -> UserApplicationService
```

- Gate/Wizard/Recovery/Success render standalone (no bottom nav); only `home`
  mounts `AppShell`. Telegram BackButton is visible iff a nested screen is on
  top (pop on press, hidden at tab roots).
- Server DTO is the single source of truth. Mutating screens flip UI only on
  the server-confirmed response and guard same-tick double taps with a
  synchronous `createToggleGate` (React `saving` state exists only after
  re-render).
- `telegram.ts` is the sole `window.Telegram` touchpoint (initData, setup,
  haptics, close, BackButton). Transport handlers know only application use
  cases + typed public codes — zero `aster`/`storage` imports server-side.

## Key Directories

### Shell, routing, flow — `src/` root + `screens/` flow + `components/`

| File | Purpose |
|---|---|
| `App.tsx` | Root composition: 5-kind `Phase` machine, `routeSession`/`loadSession`/`handleRecover`, dev banner. Backend: `session`, `builder/recover` |
| `main.tsx` | Bootstrap: StrictMode `createRoot` + `index.css`; no exports |
| `navigation.ts` | Pure typed nav: `ROOT_TABS` (home/positions/activity/settings, RU labels), `NestedScreen` (risk/notifications/system-status/account-builder/security/help), `initialRoute/selectTab/pushNested/popNested/currentNested` |
| `session.ts` | Server→UI router: `sessionDestination()` over `MiniAppSessionDTO` (recovery_required beats connected; APPROVAL_SENT/APPROVED → attempt_recovery; PREPARED → wizard) |
| `components/AppShell.tsx` | Shell: mount-all four tabs in `hidden` wrappers + `NestedRoute` switch + BackButton wiring; warmup gate (`warmReady` on Home ready) + per-tab scroll slots; child screens fetch own data |
| `screens/SessionGate.tsx` | Read-only access gate (loading/pending/rejected/invalid/error/recovery_required) |
| `screens/SuccessScreen.tsx` | Post-onboarding receipt (`BuilderOnboardedDTO` rows; copying paused) |
| `screens/BuilderAttemptRecovery.tsx` | Durable-attempt resync without wallet re-prompt (`builder/resume-attempt` + `client-log`) |
| `screens/InfoScreens.tsx` | Static Help/Security explainers; never claims live state |

### Transport, wallet, onboarding — `src/` lib

| File | Purpose |
|---|---|
| `api.ts` | Typed POST client mirroring all 21 routes; initData headers (+ dev-only `X-Dev-User-Id`), 15s AbortController timeout, `ApiError` |
| `wallet.ts` | Adapters: injected EIP-1193 first, lazy WalletConnect second; BSC-56 signing domain; typed `WalletError` (`VITE_WC_PROJECT_ID`) |
| `telegram.ts` | Sole Telegram wrapper: initData, setup, haptics, close, closing-confirm, BackButton registry |
| `errors.ts` | Server-code → Russian `{title,hint}` map + FALLBACK; never leaks raw payloads |
| `types.ts` | DTO mirrors of backend `application/models.py` (session/dashboard/builder/status/account-builder) |
| `wizard.ts` | Pure onboarding machine: wallet→review→signing→sync + trust-rail index |
| `accountBuilder.ts` | Shared verdict (verified-only), state/reason copy, local-only disconnect copy |
| `revalidate.ts` | Shared warm-activation primitive: `useWarmRevalidation` (active-only trigger, TTL floors, busy guard, retry/revalidate split) + `HOME_REVALIDATE_MS`/`ACTIVITY_REVALIDATE_MS` |
| `screens/BuilderWizard.tsx` | Onboarding screen: resolveAdapter → prepare → one combined `approveAgent` signature → submit |
| `screens/AccountBuilderScreen.tsx` | Read-only attestation: seq-guarded refresh, backend-gated reauthorize, local-only disconnect |

### Follower screens — `screens/` + pure view-models

| Screen | Contract | Backend routes |
|---|---|---|
| `HomeScreen.tsx` (+`home.ts`) | MUTATES pause/resume (+ confirm Sheet); trust strip, metrics, first position | `dashboard`, `pause`, `resume`, `confirmation/begin`, `confirmation/confirm` |
| `PositionsScreen.tsx` | READ-ONLY list + exported `PositionCard` (entry/now/stop/TP/risk-to-stop/leverage); refresh-only | `positions` |
| `ActivityScreen.tsx` (+`activityView.ts`) | READ-ONLY journal timeline, filter tabs, append-pagination | `activity` |
| `RiskScreen.tsx` (+`riskView.ts`) | MUTATES risk-on-Stop: draft/confirmed/save integration, exact 1e-5 integer math; ruler control lives in `components/RiskRuler.tsx` | `risk`, `risk/percent` |
| `SettingsScreen.tsx` (+`settingsView.ts`) | READ-ONLY capability list; navigation only | — (routes to nested) |
| `NotificationsScreen.tsx` (+`notificationsView.ts`) | MUTATES single toggle (`role=switch`), server-confirmed flip + double-tap gate | `notifications`, `notifications/enabled` |
| `SystemStatusScreen.tsx` (+`systemStatus.ts`) | READ-ONLY status slice, seq-guarded refresh, transport-stale honesty | `status` |
| `components/ui.tsx` | Shared `Brand/TrustRail/Sheet/Row/ErrorBox` (+ inline SVG icons) | — |
| `components/RiskRuler.tsx` | Snap-strip control: native touch/trackpad inertia, mouse drag w/ pointer capture, gated wheel, interruptible settle animator, scrollend+fallback settle, rate-limited selection haptics, save freeze | — |
| `riskRuler.ts` | Pure ruler decisions: wheel deltaMode normalization + `wheelIntent` gating, ease-out/duration, clamp, haptic rate limit | — |
| `protectionWarning.ts` | Protection-state → Russian warning copy map | — |
| `format.ts` | USDT/fee/date/pnl formatting | — |

## Development Commands

```bash
cd miniapp
pnpm install          # deps (@walletconnect lazy chunk needs VITE_WC_PROJECT_ID at runtime)
pnpm dev              # HMR; needs backend on :8080 (VITE_API_TARGET override) — or real bot via dev-up.sh
pnpm lint             # oxlint (no ESLint)
pnpm test             # vitest run, 21 files
pnpm build            # tsc -b && vite build -> dist/ (git-ignored, served by bot at /miniapp/)
pnpm preview          # vite preview
```

Pairing with backend: `./scripts/dev-up.sh` (bundle + tunnel + bot), or
`python scripts/preview_miniapp_engine.py --account <ID> --confirm-engine-preview`
(engine-backed preview on a DB copy, browser-only).

## Code Conventions & Common Patterns

- **View-models are pure**: `home.ts`, `riskView.ts`, `activityView.ts`, `notificationsView.ts`, `systemStatus.ts`, `settingsView.ts`, `navigation.ts`, `session.ts`, `wizard.ts` — no fetch, no Telegram, unit-testable in node. Components are thin containers with local `useState` load/error only; no store.
- **Read-only honesty**: read-only screens assert it in markup (no trading controls, no secrets/ids); mutating screens change confirmed values only.
- **Secret-safety**: masked addresses only; `describeError` maps codes, raw payloads never render; wallet private key never enters the app.
- **Russian UI copy** (`ROOT_TAB_LABELS`, errors, info screens); inline SVG icons, no emoji; `:focus-visible`, semantic dialog roles; native BackButton/Haptics.
- **Naming**: `*.test.ts(x)` next to source; `*View.ts` = pure VM; `*Behavior.test.tsx` = jsdom interaction.

## Important Files

| File | Purpose |
|---|---|
| `src/App.tsx` | Phase machine, session routing, dev banner |
| `src/navigation.ts` | Tab + nested-stack state (no router dep) |
| `src/session.ts` | Session-DTO → destination router |
| `src/api.ts` | 21-method typed transport client |
| `src/wallet.ts` | Injected + WalletConnect adapters, chain 56 |
| `src/telegram.ts` | Sole Telegram boundary |
| `src/types.ts` | Backend DTO mirrors |
| `src/index.css` | Tokens: `--canvas #070707`, `--champagne #f2c98d`, safe-area/radius |
| `vite.config.ts` | `base: '/miniapp/'`, `/miniapp/api` proxy |
| `package.json` | `dev/build/lint/preview/test` scripts; React 19, Vite 8, TS ~6, Oxlint, Vitest |

## Runtime & Tooling Preferences

- Node (unpinned — use modern for Vite 8) + `pnpm` (lockfile 9.0).
- React 19 + TypeScript strict (`erasableSyntaxOnly`, `noUnused*`) + Oxlint (react + typescript plugins; NO ESLint).
- Vitest `environment: 'node'`, `src/**/*.test.{ts,tsx}`; `dist/` git-ignored.
- Backend pairing on `127.0.0.1:8080` (`VITE_API_TARGET` override; `VITE_DEV_USER_ID` for unsigned dev sessions).

## Testing & QA

21 test files, three styles:

- **Pure VM** (node, no rendering): `home/navigation/session/wallet/wizard/accountBuilder/systemStatus/format/riskRuler` tests — import helpers directly.
- **Static contract** (`renderToStaticMarkup`, effects never fire): `positions/risk/activity/notifications/systemStatus/settings` — loading frames + presentational views on canned DTOs, read-only/secret-free assertions.
- **Behavior** (jsdom + testing-library, mocked `api.*`): `riskBehavior` (tap→dirty→save, one-POST gate, ruler freeze), `riskRulerBehavior` (wheel gating/normalization/boundary, drag capture/cancel/suppression scope, animator interruption, settle fallback), `systemStatusBehavior` (races, stale honesty, retry), `accountBuilderBehavior` (load/refresh/reauth/disconnect/seq-guard), `appShellKeepAlive` (mount-all warmup budget, TTL revalidation, scroll slots, seq-guards, partial-ready).
