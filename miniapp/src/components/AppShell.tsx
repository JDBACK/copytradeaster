/** Follower app shell (Stage 3 part 2, P2.0): exactly four root tabs with a
 * persistent bottom navigation.  Gate/Wizard/Recovery/Success stay outside
 * the shell; nested screens use the Telegram BackButton. */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { AuthContext } from '../api'
import { MiniAppResources } from '../resources'
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
import { TradeListScreen } from '../screens/TradeListScreen'
import { TradeDetailScreen, PositionDetailScreen } from '../screens/TradeDetailScreen'
import { Sheet } from './ui'
import { AccountBuilderScreen } from '../screens/AccountBuilderScreen'
import { ActivityScreen } from '../screens/ActivityScreen'
import { HelpScreen, SecurityScreen } from '../screens/InfoScreens'
import { HomeScreen } from '../screens/HomeScreen'
import { NotificationsScreen } from '../screens/NotificationsScreen'
import { PositionsScreen } from '../screens/PositionsScreen'
import { RiskScreen } from '../screens/RiskScreen'
import { SettingsScreen } from '../screens/SettingsScreen'
import { SystemStatusScreen } from '../screens/SystemStatusScreen'
import { LiveStatus } from './LiveStatus'
import { PullToRefresh } from './PullToRefresh'

export function AppShell({
  auth,
  resources: providedResources,
  banner,
  onNotConnected,
  accountMasked,
  launchRef = null,
  environment,
}: {
  auth: AuthContext
  launchRef?: string | null
  environment?: string | null
  resources?: MiniAppResources
  banner?: ReactNode
  onNotConnected: () => void
  accountMasked?: string | null
}) {
  const resources = useMemo(
    () => providedResources ?? new MiniAppResources(auth, `standalone-shell:${auth.devUserId ?? auth.initData}`),
    [providedResources, auth],
  )
  const [route, setRoute] = useState(() => launchRef ? { tab: 'positions' as const, nested: [{ kind: 'trade-detail' as const, tradeRef: launchRef }] } : initialRoute())
  const nested = currentNested(route)
  const [visited, setVisited] = useState<ReadonlySet<RootTab>>(
    () => new Set<RootTab>([initialRoute().tab]),
  )
  // Raised once Home reaches ready. Before live is healthy Home performs the
  // authoritative dashboard(true) boundary; once live is healthy it consumes
  // the already-authoritative process cache with dashboard(false). Either path
  // leaves the shared snapshot ready for the other tabs' cheap reads.
  const [warmReady, setWarmReady] = useState(false)
  const handleHomeReady = useCallback(() => setWarmReady(true), [])
  // Activity pagination is intentionally still screen-owned in S2/F2; keep a
  // single invalidation generation for it until its stable-id merge lands.
  const [activityInvalidation, setActivityInvalidation] = useState(0)
  const handleHomeMutated = useCallback(() => {
    resources.history.invalidate()
    setActivityInvalidation((value) => value + 1)
  }, [resources])
  const [foregroundSignal, setForegroundSignal] = useState(0)
  const lastForegroundAt = useRef(0)
  const contentRef = useRef<HTMLDivElement | null>(null)
  // Navigation lifetime is separate from component lifetime.  Root tabs stay
  // mounted, while each nested stack entry owns its own scroll position and
  // the element that opened it.  Today the product only links one level deep,
  // but keeping this keyed by the typed route stack prevents a future
  // nested→nested Back from inheriting the root slot or restoring the wrong
  // focus target.
  const nestedScrollTops = useRef(new Map<string, number>())
  const nestedFocusTargets = useRef(
    new Map<string, { element: HTMLElement | null; nestedFocusableIndex: number | null }>(),
  )
  const nestedEntryKeys = useRef<string[]>(launchRef ? ['launch-detail'] : [])
  // Render keys must be part of the committed React snapshot. The ref is
  // retained only for synchronous Back/scroll bookkeeping in event handlers.
  const [renderEntryKeys, setRenderEntryKeys] = useState<string[]>(() => launchRef ? ['launch-detail'] : [])
  const nextNestedEntryId = useRef(0)
  const [riskDirty, setRiskDirty] = useState(false)
  const [leaveIntent, setLeaveIntent] = useState<
    { kind: 'back' } | { kind: 'tab'; tab: RootTab } | null
  >(null)
  const nestedBackOverrideRef = useRef<(() => boolean) | null>(null)
  const setNestedBackOverride = useCallback((handler: (() => boolean) | null) => {
    nestedBackOverrideRef.current = handler
  }, [])
  const scrollTops = useRef<Record<RootTab, number>>({
    home: 0,
    positions: 0,
    activity: 0,
    settings: 0,
  })

  useEffect(() => {
    const foreground = () => {
      if (document.visibilityState !== 'visible') return
      const now = Date.now()
      // Telegram/WebKit commonly deliver focus + visibilitychange together.
      if (now - lastForegroundAt.current < 100) return
      lastForegroundAt.current = now
      setForegroundSignal((value) => value + 1)
    }
    window.addEventListener('focus', foreground)
    document.addEventListener('visibilitychange', foreground)
    return () => {
      window.removeEventListener('focus', foreground)
      document.removeEventListener('visibilitychange', foreground)
    }
  }, [])

  const commitBack = useCallback(() => {
    const currentKey = nestedEntryKeys.current.at(-1) ?? null
    const el = contentRef.current
    if (currentKey && el) nestedScrollTops.current.set(currentKey, el.scrollTop)
    const focusTarget = currentKey ? nestedFocusTargets.current.get(currentKey) ?? null : null
    const returnsToNested = nestedEntryKeys.current.length > 1
    if (currentKey) {
      nestedEntryKeys.current.pop()
      setRenderEntryKeys([...nestedEntryKeys.current])
      nestedScrollTops.current.delete(currentKey)
      nestedFocusTargets.current.delete(currentKey)
    }
    setRiskDirty(false)
    setRoute((current) => popNested(current))
    window.requestAnimationFrame(() => {
      if (focusTarget?.element?.isConnected) {
        focusTarget.element.focus()
        return
      }
      // A nested→nested push unmounts the previous NestedRoute. Its opener DOM
      // node is therefore detached on Back; restore focus by the per-entry
      // focusable slot inside the freshly remounted nested surface.
      const nestedFocusableIndex = focusTarget?.nestedFocusableIndex ?? null
      if (returnsToNested && nestedFocusableIndex !== null) {
        nestedFocusableElements()[nestedFocusableIndex]?.focus()
      }
    })
  }, [])

  const requestBack = useCallback(() => {
    if (leaveIntent) {
      // Native Back closes the visible discard Sheet first; a second Back can
      // then leave the Risk screen. This mirrors ordinary modal stack order.
      setLeaveIntent(null)
      return
    }
    if (nestedBackOverrideRef.current?.()) return
    if (nested?.kind === 'risk' && riskDirty && !resources.mutationLocked('risk')) {
      setLeaveIntent({ kind: 'back' })
      return
    }
    commitBack()
  }, [commitBack, leaveIntent, nested, resources, riskDirty])

  useEffect(() => {
    if (nested) setBackButton(true, requestBack)
    else setBackButton(false)
    return () => setBackButton(false)
  }, [nested, requestBack])

  const commitTab = useCallback(
    (tab: RootTab) => {
      // The tab slot is written only when the tab itself is the visible
      // surface. From under a nested screen the container shows the nested
      // content — writing its scrollTop into the tab slot would corrupt it.
      if (route.nested.length === 0) {
        const el = contentRef.current
        if (el) scrollTops.current[route.tab] = el.scrollTop
      }
      for (const key of nestedEntryKeys.current) {
        nestedScrollTops.current.delete(key)
        nestedFocusTargets.current.delete(key)
      }
      nestedEntryKeys.current = []
      setRenderEntryKeys([])
      setRoute((current) => selectTab(current, tab))
      setVisited((prev) => (prev.has(tab) ? prev : new Set([...prev, tab])))
      hapticImpact('light')
    },
    [route.tab, route.nested.length],
  )

  const handleTab = useCallback(
    (tab: RootTab) => {
      if (nested?.kind === 'risk' && riskDirty && !resources.mutationLocked('risk')) {
        setLeaveIntent({ kind: 'tab', tab })
        return
      }
      commitTab(tab)
    },
    [commitTab, nested, resources, riskDirty],
  )

  const openNested = useCallback(
    (screen: NestedScreen) => {
      // Persist the currently visible surface before pushing.  The first
      // nested entry saves the root slot; deeper entries save their own slot.
      const el = contentRef.current
      const currentKey = nestedEntryKeys.current.at(-1) ?? null
      if (el) {
        if (currentKey) nestedScrollTops.current.set(currentKey, el.scrollTop)
        else scrollTops.current[route.tab] = el.scrollTop
      }
      const nextRoute = pushNested(route, screen)
      const nextKey = `${route.tab}:nested-${++nextNestedEntryId.current}`
      nestedEntryKeys.current.push(nextKey)
      setRenderEntryKeys([...nestedEntryKeys.current])
      const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
      const currentNestedFocusables = route.nested.length > 0 ? nestedFocusableElements() : []
      nestedFocusTargets.current.set(
        nextKey,
        {
          element: activeElement,
          nestedFocusableIndex:
            activeElement && route.nested.length > 0
              ? currentNestedFocusables.indexOf(activeElement)
              : null,
        },
      )
      setRoute(nextRoute)
    },
    [route],
  )

  useLayoutEffect(() => {
    const el = contentRef.current
    if (!el) return
    const key = nestedEntryKeys.current.at(-1) ?? null
    // A newly pushed nested screen starts at the top. Returning to an existing
    // nested stack entry restores that entry's own slot; returning to a tab
    // root restores the root slot.
    el.scrollTop = key
      ? (nestedScrollTops.current.get(key) ?? 0)
      : (scrollTops.current[route.tab] ?? 0)
  }, [route])

  const homeActive = route.tab === 'home' && !nested
  // The single manual refresh in the app: pull down at the top of the surface.
  const handlePullRefresh = useCallback(() => resources.pullRefresh(), [resources])
  const positionsActive = route.tab === 'positions' && !nested
  const activityActive = route.tab === 'activity' && !nested
  const settingsActive = route.tab === 'settings' && !nested

  return (
    <div className="shell">
      {banner}
      <LiveStatus resources={resources} />
      <PullToRefresh scroller={contentRef} onRefresh={handlePullRefresh} />
      <div className="shell-content" ref={contentRef}>
        <div hidden={!homeActive}>
          <HomeScreen
            auth={auth}
            resources={resources}
            onNotConnected={onNotConnected}
            active={homeActive}
            onReady={handleHomeReady}
            onMutated={handleHomeMutated}
            onOpen={openNested}
            onTab={handleTab}
            foregroundSignal={foregroundSignal}
          />
        </div>
        <div hidden={!!nested || route.tab !== 'positions'}>
          <PositionsScreen
            auth={auth}
            resources={resources}
            accountMasked={accountMasked}
            active={positionsActive}
            warm={warmReady || visited.has('positions')}
            onOpen={openNested}
            foregroundSignal={foregroundSignal}
          />
        </div>
        <div hidden={!!nested || route.tab !== 'activity'}>
          <ActivityScreen
            auth={auth}
            resources={resources}
            onOpen={openNested}
            accountMasked={accountMasked}
            active={activityActive}
            warm={warmReady || visited.has('activity')}
            invalidateSignal={activityInvalidation}
            foregroundSignal={foregroundSignal}
          />
        </div>
        <div hidden={!!nested || route.tab !== 'settings'}>
          <SettingsScreen
            auth={auth}
            resources={resources}
            accountMasked={accountMasked}
            onOpen={openNested}
            environment={environment}
            onNotConnected={onNotConnected}
            active={settingsActive}
            warm={warmReady || visited.has('settings')}
            foregroundSignal={foregroundSignal}
          />
        </div>
        {route.nested.map((screen, index) => (
          <div key={renderEntryKeys[index]} hidden={index !== route.nested.length - 1} data-nested-surface={index === route.nested.length - 1 ? '' : undefined}>
            <NestedRoute nested={screen} auth={auth} resources={resources} accountMasked={accountMasked}
              onNotConnected={onNotConnected} onOpen={openNested} active={index === route.nested.length - 1}
              onActivityInvalidated={() => { resources.history.invalidate(); setActivityInvalidation(value => value + 1) }}
              foregroundSignal={foregroundSignal} onRiskDirtyChange={setRiskDirty} setBackOverride={setNestedBackOverride} />
          </div>
        ))}
        {leaveIntent && (
          <Sheet
            title="Отменить изменения?"
            body="Новое значение риска ещё не сохранено. При выходе выбранное значение будет потеряно."
            onClose={() => setLeaveIntent(null)}
          >
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => {
                const intent = leaveIntent
                setLeaveIntent(null)
                setRiskDirty(false)
                if (intent.kind === 'back') commitBack()
                else commitTab(intent.tab)
              }}
            >
              Выйти без сохранения
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => setLeaveIntent(null)}>
              Остаться
            </button>
          </Sheet>
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

function nestedFocusableElements(): HTMLElement[] {
  const surface = document.querySelector('[data-nested-surface]')
  if (!surface) return []
  return Array.from(
    surface.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
}

/** Nested screens live on the typed stack; unimplemented kinds render nothing
 * and are only reachable through their own future part. */
export function NestedRoute({
  nested,
  auth,
  resources,
  accountMasked,
  onNotConnected,
  onInvalidate,
  onActivityInvalidated,
  foregroundSignal,
  onRiskDirtyChange,
  setBackOverride,
  onOpen = () => {},
  active = true,
}: {
  nested: NestedScreen
  onOpen?: (route: NestedScreen) => void
  active?: boolean
  auth: AuthContext
  resources?: MiniAppResources
  accountMasked?: string | null
  onNotConnected?: () => void
  /** Mutation-driven invalidation sink; defaults to a no-op for direct renders. */
  onInvalidate?: (tabs: RootTab[]) => void
  onActivityInvalidated?: () => void
  foregroundSignal?: number
  onRiskDirtyChange?: (dirty: boolean) => void
  setBackOverride?: (handler: (() => boolean) | null) => void
}) {
  const handleNotConnected = onNotConnected ?? (() => {})
  const notify = onInvalidate ?? (() => {})
  switch (nested.kind) {
    case 'trade-list':
      return resources ? <TradeListScreen auth={auth} resources={resources} route={nested} onOpen={onOpen} active={active} foregroundSignal={foregroundSignal} /> : null
    case 'trade-detail':
      return resources ? <TradeDetailScreen resources={resources} tradeRef={nested.tradeRef} onOpen={onOpen} active={active} foregroundSignal={foregroundSignal} /> : null
    case 'position-detail':
      return resources ? <PositionDetailScreen resources={resources} symbol={nested.symbol} side={nested.side} active={active} foregroundSignal={foregroundSignal} /> : null
    case 'risk':
      // A saved risk percent is read by Home and Settings from their own
      // dashboard snapshots — both go stale here, without unmount or skeleton.
      return (
        <RiskScreen
          auth={auth}
          resources={resources}
          accountMasked={accountMasked}
          foregroundSignal={foregroundSignal}
          onDirtyChange={onRiskDirtyChange}
          onSaved={() => {
            onActivityInvalidated?.()
            notify(['home', 'positions', 'activity', 'settings'])
          }}
        />
      )
    case 'notifications':
      // Settings renders the toggle state from its dashboard snapshot.
      return (
        <NotificationsScreen
          auth={auth}
          resources={resources}
          accountMasked={accountMasked}
          foregroundSignal={foregroundSignal}
          onSaved={() => notify(['home', 'settings'])}
        />
      )
    case 'system-status':
      return (
        <SystemStatusScreen
          auth={auth}
          resources={resources}
          accountMasked={accountMasked}
          foregroundSignal={foregroundSignal}
        />
      )
    case 'account-builder':
      return (
        <AccountBuilderScreen
          auth={auth}
          resources={resources}
          accountMasked={accountMasked}
          onNotConnected={handleNotConnected}
          foregroundSignal={foregroundSignal}
          setBackOverride={setBackOverride}
        />
      )
    case 'security':
      return <SecurityScreen accountMasked={accountMasked} />
    case 'help':
      return <HelpScreen accountMasked={accountMasked} onOpen={onOpen} />
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
