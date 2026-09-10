/** Settings root (эталон 06-settings, Stage 3 part 2 P2.3–P2.4).
 *
 * A capability list with sections, not a card grid.  Rows that navigate
 * (Risk, Notifications, Status, Security, Help) get a chevron; Account+Builder
 * and the static Environment stay read-only.
 * Values come from the authoritative DashboardDTO — nothing is hardcoded
 * and stale/degraded never reads «Всё работает». */

import { useCallback, useEffect, useMemo } from 'react'
import { HOME_REVALIDATE_MS } from '../revalidate'
import type { AuthContext } from '../api'
import { formatPercent } from '../format'
import type { NestedScreen } from '../navigation'
import {
  notificationsValue,
  systemStatusValue,
  type SettingsTone,
} from '../settingsView'
import { hapticImpact } from '../telegram'
import type { DashboardDTO } from '../types'
import { Brand, ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import { MiniAppResources } from '../resources'
import { createSettingsPrefetch, type SettingsPrefetchKey } from '../settingsPrefetch'
import { useResource, useScreenResources, useTransportStale } from '../useResource'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dashboard: DashboardDTO }
  | { kind: 'error'; code: string }

export function SettingsScreen({
  auth,
  accountMasked,
  onOpen,
  onNotConnected,
  active = true,
  warm = true,
  resources: providedResources,
  foregroundSignal,
  environment,
  invalidateSignal: _invalidateSignal,
}: {
  auth: AuthContext
  environment?: string | null
  accountMasked?: string | null
  onOpen: (screen: NestedScreen) => void
  onNotConnected: () => void
  active?: boolean
  /** Gate for the first fetch: shell warmup or the user's first tap. */
  warm?: boolean
  resources?: MiniAppResources
  foregroundSignal?: number
  /** @deprecated S2 resources own invalidation; kept for standalone legacy tests. */
  invalidateSignal?: number
}) {
  void _invalidateSignal
  const resources = useScreenResources(providedResources, auth)
  const resource = useResource({
    resources,
    key: 'dashboard',
    enabled: warm,
    active,
    ttlMs: HOME_REVALIDATE_MS,
    strength: 'weak',
    foregroundSignal,
  })
  const prefetch = useMemo(() => createSettingsPrefetch(resources), [resources])
  const dashboard = resource.snapshot.data
  const transportUnhealthy = useTransportStale(resources, resource.snapshot.error)
  const transportStale = dashboard !== null && transportUnhealthy
  const load: LoadState = dashboard
    ? { kind: 'ready', dashboard }
    : resource.snapshot.error
      ? { kind: 'error', code: resource.snapshot.error }
      : { kind: 'loading' }

  useEffect(() => {
    if (dashboard?.copy_state === 'empty') onNotConnected()
  }, [dashboard, onNotConnected])

  useEffect(() => {
    // Hidden warm Settings may consume Dashboard, but nested speculative reads
    // begin only after the user actually enters the visible Settings root.
    if (active && dashboard) prefetch.start()
    else prefetch.stop()
    return () => prefetch.stop()
  }, [active, dashboard, prefetch])

  const openNested = useCallback(
    (screen: NestedScreen) => {
      const key = settingsResourceKey(screen)
      if (key) prefetch.prioritize(key)
      onOpen(screen)
    },
    [onOpen, prefetch],
  )

  if (load.kind === 'loading') {
    return (
      <div className="screen">
        <Brand
          right={
            accountMasked ? (
              <span className="wallet-chip num">{accountMasked}</span>
            ) : (
              <span className="badge-testnet">Aster testnet</span>
            )
          }
        />
        <h1 className="h1" style={{ marginTop: 4 }}>
          Настройки
        </h1>
        <div className="skeleton" style={{ minHeight: 92 }} aria-label="Загрузка настроек" />
        <div className="skeleton" style={{ minHeight: 220 }} />
      </div>
    )
  }

  if (load.kind === 'error') {
    const copy = describeError(load.code)
    return (
      <div className="screen">
        <Brand
          right={
            accountMasked ? (
              <span className="wallet-chip num">{accountMasked}</span>
            ) : (
              <span className="badge-testnet">Aster testnet</span>
            )
          }
        />
        <h1 className="h1" style={{ marginTop: 4 }}>
          Настройки
        </h1>
        <ErrorBox title={copy.title} hint={copy.hint} />
      </div>
    )
  }

  return (
    <SettingsView
      dashboard={load.dashboard}
      accountMasked={accountMasked}
      onOpen={openNested}
      transportStale={transportStale}
      environment={environment}
    />
  )
}

function settingsResourceKey(screen: NestedScreen): SettingsPrefetchKey | null {
  switch (screen.kind) {
    case 'risk':
      return 'risk'
    case 'notifications':
      return 'notifications'
    case 'system-status':
      return 'status'
    case 'account-builder':
      return 'accountBuilder'
    default:
      return null
  }
}

/** Presentational settings list over an authoritative dashboard snapshot. */
export function SettingsView({
  dashboard,
  accountMasked,
  onOpen,
  transportStale = false,
  environment,
}: {
  dashboard: DashboardDTO
  accountMasked?: string | null
  onOpen: (screen: NestedScreen) => void
  transportStale?: boolean
  environment?: string | null
}) {
  const status = systemStatusValue(
    transportStale ? 'stale' : dashboard.system_state,
    dashboard.snapshot_captured_at,
  )
  const notif = notificationsValue(dashboard.notification_enabled)
  const account = dashboard.account_masked ?? accountMasked ?? null

  const open = useCallback(
    (screen: NestedScreen) => {
      hapticImpact('light')
      onOpen(screen)
    },
    [onOpen],
  )

  return (
    <div className="screen">
      <Brand
        right={
          account ? (
            <span className="wallet-chip num">{account}</span>
          ) : (
            <span className="badge-testnet">Aster testnet</span>
          )
        }
      />

      <h1 className="h1" style={{ marginTop: 4 }}>
        Настройки
      </h1>

      <SettingsSection label="Настройки копирования" />
      <div className="settings-list">
        <SettingsRow
          icon={<RiskIcon />}
          label="Риск на сделку"
          value={`${formatPercent(dashboard.risk_percent)} · по стопу`}
          onClick={() => open({ kind: 'risk' })}
        />
        <SettingsRow
          icon={<BellIcon />}
          label="Уведомления в Telegram"
          value={notif.label}
          valueTone={notif.tone}
          onClick={() => open({ kind: 'notifications' })}
        />
        <SettingsRow
          icon={<PulseIcon />}
          label="Статус подключения"
          value={status.label}
          valueTone={status.tone}
          onClick={() => open({ kind: 'system-status' })}
        />
      </div>

      <SettingsSection label="Подключение и безопасность" />
      <div className="settings-list">
        <SettingsRow
          icon={<UserIcon />}
          label="Подключение Aster"
          value={account ?? 'Подключено'}
          valueTone="muted"
          onClick={() => open({ kind: 'account-builder' })}
        />
        <SettingsRow
          icon={<ShieldIcon />}
          label="Безопасность"
          value="Как защищён аккаунт"
          onClick={() => open({ kind: 'security' })}
        />
      </div>

      <SettingsSection label="Поддержка" />
      <div className="settings-list">
        <SettingsRow
          icon={<HelpIcon />}
          label="Помощь"
          value="Как работает копирование"
          onClick={() => open({ kind: 'help' })}
        />
        <SettingsRow icon={<GlobeIcon />} label="Среда" value={environment ? `Aster ${environment}` : 'Не определена'} valueTone="muted" />
      </div>


    </div>
  )
}

function SettingsSection({ label }: { label: string }) {
  return (
    <div className="settings-section-label">
      <span>{label}</span>
      <span className="settings-section-rule" aria-hidden />
    </div>
  )
}

function SettingsRow({
  icon,
  label,
  value,
  valueTone,
  onClick,
}: {
  icon: React.ReactNode
  label: string
  value: string
  valueTone?: SettingsTone
  onClick?: () => void
}) {
  const body = (
    <>
      <span className="settings-row-icon" aria-hidden>
        {icon}
      </span>
      <span className="settings-row-label">{label}</span>
      <span className={`settings-row-value num${valueTone ? ` tone-${valueTone}` : ''}`}>
        {value}
      </span>
      {onClick && <Chevron />}
    </>
  )
  if (!onClick) {
    return <div className="settings-row">{body}</div>
  }
  return (
    <button type="button" className="settings-row" onClick={onClick}>
      {body}
    </button>
  )
}

function Chevron() {
  return (
    <svg
      className="settings-chevron"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <path
        d="m9.5 6.5 5.5 5.5-5.5 5.5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

const ICON_STROKE = {
  stroke: 'var(--champagne)',
  strokeWidth: 1.6,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  fill: 'none',
}

function RiskIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path d="M12 3.5 19 6v5.2c0 4.6-2.9 7.6-7 9.3-4.1-1.7-7-4.7-7-9.3V6l7-2.5Z" {...ICON_STROKE} />
      <path d="M12 8.2v5.2M9.6 10.8h4.8" {...ICON_STROKE} />
    </svg>
  )
}

function BellIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path d="M6.2 10a5.8 5.8 0 0 1 11.6 0c0 3.1.7 4.9 1.7 6H4.5c1-1.1 1.7-2.9 1.7-6Z" {...ICON_STROKE} />
      <path d="M10 19.5a2.2 2.2 0 0 0 4 0" {...ICON_STROKE} />
    </svg>
  )
}

function PulseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <circle cx="12" cy="12" r="8.6" {...ICON_STROKE} />
      <path d="M7.4 12h2.2l1.5-3.2 1.8 6 1.5-2.8h2.2" {...ICON_STROKE} />
    </svg>
  )
}

function UserIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <circle cx="12" cy="8.4" r="3.4" {...ICON_STROKE} />
      <path d="M5 19c1.4-3.2 4-4.8 7-4.8s5.6 1.6 7 4.8" {...ICON_STROKE} />
    </svg>
  )
}

function ShieldIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path d="M12 3.5 19 6v5.2c0 4.6-2.9 7.6-7 9.3-4.1-1.7-7-4.7-7-9.3V6l7-2.5Z" {...ICON_STROKE} />
      <path d="m9.2 12 2 2 3.6-4" {...ICON_STROKE} />
    </svg>
  )
}

function HelpIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <circle cx="12" cy="12" r="8.6" {...ICON_STROKE} />
      <path d="M9.8 9.4A2.3 2.3 0 0 1 12 7.8c1.3 0 2.3.9 2.3 2 0 1.5-2.3 1.7-2.3 3.3" {...ICON_STROKE} />
      <path d="M12 16.4h.01" {...ICON_STROKE} />
    </svg>
  )
}

function GlobeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <circle cx="12" cy="12" r="8.6" {...ICON_STROKE} />
      <path d="M3.5 12h17M12 3.5c2.3 2.3 3.4 5.2 3.4 8.5s-1.1 6.2-3.4 8.5c-2.3-2.3-3.4-5.2-3.4-8.5S9.7 5.8 12 3.5Z" {...ICON_STROKE} />
    </svg>
  )
}
