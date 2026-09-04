/** Follower app shell (Stage 3 part 2, P2.0): exactly four root tabs with a
 * persistent bottom navigation.  Gate/Wizard/Recovery/Success stay outside
 * the shell; nested screens use the Telegram BackButton. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { AuthContext } from '../api'
import {
  ROOT_TABS,
  ROOT_TAB_LABELS,
  currentNested,
  initialRoute,
  popNested,
  pushNested,
  selectTab,
  type NestedScreen,
  type RootTab,
} from '../navigation'
import { hapticImpact, setBackButton } from '../telegram'
import { AccountBuilderScreen } from '../screens/AccountBuilderScreen'
import { ActivityScreen } from '../screens/ActivityScreen'
import { HelpScreen, SecurityScreen } from '../screens/InfoScreens'
import { HomeScreen } from '../screens/HomeScreen'
import { NotificationsScreen } from '../screens/NotificationsScreen'
import { PositionsScreen } from '../screens/PositionsScreen'
import { RiskScreen } from '../screens/RiskScreen'
import { SettingsScreen } from '../screens/SettingsScreen'
import { SystemStatusScreen } from '../screens/SystemStatusScreen'

export function AppShell({
  auth,
  banner,
  onNotConnected,
  accountMasked,
}: {
  auth: AuthContext
  banner?: ReactNode
  onNotConnected: () => void
  accountMasked?: string | null
}) {
  const [route, setRoute] = useState(initialRoute)
  const nested = currentNested(route)
  const [visited, setVisited] = useState<ReadonlySet<RootTab>>(
    () => new Set<RootTab>([initialRoute().tab]),
  )
  // Raised once Home reaches ready: its dashboard(true) refresh populates the
  // cached snapshot, so the other tabs' cheap reads stop returning empty or
  // stale frames. Until then they fetch on first tap (previous cold path).
  const [warmReady, setWarmReady] = useState(false)
  const handleHomeReady = useCallback(() => setWarmReady(true), [])
  const contentRef = useRef<HTMLDivElement | null>(null)
  const scrollTops = useRef<Record<RootTab, number>>({
    home: 0,
    positions: 0,
    activity: 0,
    settings: 0,
  })

  useEffect(() => {
    if (nested) {
      setBackButton(true, () => setRoute((current) => popNested(current)))
    } else {
      setBackButton(false)
    }
    return () => setBackButton(false)
  }, [nested])

  const handleTab = useCallback(
    (tab: RootTab) => {
      // The tab slot is written only when the tab itself is the visible
      // surface. From under a nested screen the container shows the nested
      // content — writing its scrollTop into the tab slot would corrupt it.
      if (route.nested.length === 0) {
        const el = contentRef.current
        if (el) scrollTops.current[route.tab] = el.scrollTop
      }
      setRoute((current) => selectTab(current, tab))
      setVisited((prev) => (prev.has(tab) ? prev : new Set([...prev, tab])))
      hapticImpact('light')
    },
    [route.tab, route.nested.length],
  )

  const openNested = useCallback(
    (screen: NestedScreen) => {
      // Opening nested hides the root tab: persist its scroll now, otherwise
      // the restore effect below would put back a stale value on return.
      const el = contentRef.current
      if (el) scrollTops.current[route.tab] = el.scrollTop
      setRoute((current) => pushNested(current, screen))
    },
    [route.tab],
  )

  useLayoutEffect(() => {
    const el = contentRef.current
    if (el) el.scrollTop = scrollTops.current[route.tab] ?? 0
  }, [route.tab, nested])

  const homeActive = route.tab === 'home' && !nested
  const positionsActive = route.tab === 'positions' && !nested
  const activityActive = route.tab === 'activity' && !nested
  const settingsActive = route.tab === 'settings' && !nested

  return (
    <div className="shell">
      {banner}
      <div className="shell-content" ref={contentRef}>
        <div hidden={!homeActive}>
          <HomeScreen
            auth={auth}
            onNotConnected={onNotConnected}
            active={homeActive}
            onReady={handleHomeReady}
          />
        </div>
        <div hidden={!!nested || route.tab !== 'positions'}>
          <PositionsScreen
            auth={auth}
            accountMasked={accountMasked}
            active={positionsActive}
            warm={warmReady || visited.has('positions')}
          />
        </div>
        <div hidden={!!nested || route.tab !== 'activity'}>
          <ActivityScreen
            auth={auth}
            accountMasked={accountMasked}
            active={activityActive}
            warm={warmReady || visited.has('activity')}
          />
        </div>
        <div hidden={!!nested || route.tab !== 'settings'}>
          <SettingsScreen
            auth={auth}
            accountMasked={accountMasked}
            onOpen={openNested}
            onNotConnected={onNotConnected}
            active={settingsActive}
            warm={warmReady || visited.has('settings')}
          />
        </div>
        {nested && (
          <NestedRoute
            nested={nested}
            auth={auth}
            accountMasked={accountMasked}
            onNotConnected={onNotConnected}
          />
        )}
      </div>
      <nav className="bottom-nav" aria-label="Основная навигация">
        {ROOT_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            className={`nav-item${route.tab === tab ? ' active' : ''}`}
            aria-current={route.tab === tab ? 'page' : undefined}
            onClick={() => handleTab(tab)}
          >
            <NavIcon tab={tab} />
            <span>{ROOT_TAB_LABELS[tab]}</span>
          </button>
        ))}
      </nav>
    </div>
  )
}

/** Nested screens live on the typed stack; unimplemented kinds render nothing
 * and are only reachable through their own future part. */
export function NestedRoute({
  nested,
  auth,
  accountMasked,
  onNotConnected,
}: {
  nested: NestedScreen
  auth: AuthContext
  accountMasked?: string | null
  onNotConnected?: () => void
}) {
  const handleNotConnected = onNotConnected ?? (() => {})
  switch (nested.kind) {
    case 'risk':
      return <RiskScreen auth={auth} accountMasked={accountMasked} />
    case 'notifications':
      return <NotificationsScreen auth={auth} accountMasked={accountMasked} />
    case 'system-status':
      return <SystemStatusScreen auth={auth} accountMasked={accountMasked} />
    case 'account-builder':
      return (
        <AccountBuilderScreen
          auth={auth}
          accountMasked={accountMasked}
          onNotConnected={handleNotConnected}
        />
      )
    case 'security':
      return <SecurityScreen accountMasked={accountMasked} />
    case 'help':
      return <HelpScreen accountMasked={accountMasked} />
    default:
      return null
  }
}

function NavIcon({ tab }: { tab: RootTab }) {
  const common = {
    width: 22,
    height: 22,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  }
  switch (tab) {
    case 'home':
      return (
        <svg {...common}>
          <path d="M4 11.5 12 4l8 7.5" />
          <path d="M6.5 10v9h11v-9" />
        </svg>
      )
    case 'positions':
      return (
        <svg {...common}>
          <rect x="3.5" y="7.5" width="17" height="12" rx="2.5" />
          <path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5M3.5 12.5h17" />
        </svg>
      )
    case 'activity':
      return (
        <svg {...common}>
          <path d="M3 12h4l2.5-6 4 12L16 12h5" />
        </svg>
      )
    case 'settings':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="3.2" />
          <path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8" />
        </svg>
      )
  }
}
