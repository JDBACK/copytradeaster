/** Follower app shell (Stage 3 part 2, P2.0): exactly four root tabs with a
 * persistent bottom navigation.  Gate/Wizard/Recovery/Success stay outside
 * the shell; nested screens use the Telegram BackButton. */

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import type { AuthContext } from '../api'
import {
  ROOT_TABS,
  ROOT_TAB_LABELS,
  currentNested,
  initialRoute,
  popNested,
  selectTab,
  type RootTab,
} from '../navigation'
import { hapticImpact, setBackButton } from '../telegram'
import { ActivityScreen } from '../screens/ActivityScreen'
import { HomeScreen } from '../screens/HomeScreen'
import { PositionsScreen } from '../screens/PositionsScreen'
import { Brand } from './ui'

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

  useEffect(() => {
    if (nested) {
      setBackButton(true, () => setRoute((current) => popNested(current)))
    } else {
      setBackButton(false)
    }
    return () => setBackButton(false)
  }, [nested])

  const handleTab = useCallback((tab: RootTab) => {
    setRoute((current) => selectTab(current, tab))
    hapticImpact('light')
  }, [])

  return (
    <div className="shell">
      {banner}
      <div className="shell-content">
        {route.tab === 'home' && <HomeScreen auth={auth} onNotConnected={onNotConnected} />}
        {route.tab === 'positions' && (
          <PositionsScreen auth={auth} accountMasked={accountMasked} />
        )}
        {route.tab === 'activity' && (
          <ActivityScreen auth={auth} accountMasked={accountMasked} />
        )}
        {route.tab === 'settings' && (
          <StubScreen title="Настройки" note="Настройки копирования и подключения." />
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

/** Interim tab body until its own part wires the real data (P2.1–P2.3). */
function StubScreen({ title, note }: { title: string; note: string }) {
  return (
    <div className="screen">
      <Brand right={<span className="badge-testnet">Aster testnet</span>} />
      <h1 className="h1" style={{ marginTop: 4 }}>
        {title}
      </h1>
      <div className="info-box">{note}</div>
    </div>
  )
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
