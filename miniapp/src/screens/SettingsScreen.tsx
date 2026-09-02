/** Settings root (эталон 06-settings, Stage 3 part 2 P2.3).
 *
 * A capability list with sections, not a card grid.  Only rows that really
 * navigate in P2.3 (Notifications, Security, Help) get a chevron; Risk,
 * Status, Account+Builder and the static Environment are read-only rows.
 * Values come from the authoritative DashboardDTO — nothing is hardcoded
 * and stale/degraded never reads «Всё работает». */

import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { formatPercent } from '../format'
import type { NestedScreen } from '../navigation'
import {
  copySummary,
  notificationsValue,
  systemStatusValue,
  type SettingsTone,
} from '../settingsView'
import { hapticImpact } from '../telegram'
import type { DashboardDTO } from '../types'
import { Brand, ErrorBox } from '../components/ui'
import { describeError } from '../errors'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dashboard: DashboardDTO }
  | { kind: 'error'; code: string }

export function SettingsScreen({
  auth,
  accountMasked,
  onOpen,
  onNotConnected,
}: {
  auth: AuthContext
  accountMasked?: string | null
  onOpen: (screen: NestedScreen) => void
  onNotConnected: () => void
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })

  const refresh = useCallback(async () => {
    try {
      const dashboard = await api.dashboard(auth, false)
      if (dashboard.copy_state === 'empty') {
        // The shell is only rendered for connected accounts; an empty
        // dashboard means the session desynced — route back to onboarding.
        onNotConnected()
        return
      }
      setLoad({ kind: 'ready', dashboard })
    } catch (err) {
      setLoad({ kind: 'error', code: err instanceof ApiError ? err.code : 'network_error' })
    }
  }, [auth, onNotConnected])

  useEffect(() => {
    // Initial fetch only: refresh() awaits before its first setState.
    // oxlint-disable-next-line react/set-state-in-effect
    void refresh()
  }, [refresh])

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
        <div className="btn-stack">
          <button type="button" className="btn btn-secondary" onClick={() => void refresh()}>
            Повторить
          </button>
        </div>
      </div>
    )
  }

  return (
    <SettingsView dashboard={load.dashboard} accountMasked={accountMasked} onOpen={onOpen} />
  )
}

/** Presentational settings list over an authoritative dashboard snapshot. */
export function SettingsView({
  dashboard,
  accountMasked,
  onOpen,
}: {
  dashboard: DashboardDTO
  accountMasked?: string | null
  onOpen: (screen: NestedScreen) => void
}) {
  const copy = copySummary(dashboard.copy_state)
  const status = systemStatusValue(dashboard.system_state)
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

      <section className="panel settings-summary" aria-label={`Копирование: ${copy.label}`}>
        <div className="settings-summary-icon" aria-hidden>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
            <circle
              cx="12"
              cy="12"
              r="8"
              stroke={toneColor(copy.tone)}
              strokeWidth="1.8"
              opacity="0.55"
            />
            <circle cx="12" cy="12" r="3" fill={toneColor(copy.tone)} />
          </svg>
        </div>
        <div className="settings-summary-body">
          <div className="settings-summary-title">Копирование</div>
          <div className={`settings-summary-state tone-${copy.tone}`}>{copy.label}</div>
        </div>
        <span className={`dot ${copy.tone === 'muted' ? '' : copy.tone}`} aria-hidden />
      </section>

      <SettingsSection label="Настройки копирования" />
      <div className="settings-list">
        <SettingsRow
          icon={<RiskIcon />}
          label="Риск на Stop"
          value={formatPercent(dashboard.risk_percent)}
        />
        <SettingsRow
          icon={<BellIcon />}
          label="Уведомления"
          value={notif.label}
          valueTone={notif.tone}
          onClick={() => open({ kind: 'notifications' })}
        />
        <SettingsRow
          icon={<PulseIcon />}
          label="Статус Aster"
          value={status.label}
          valueTone={status.tone}
        />
      </div>

      <SettingsSection label="Подключение и безопасность" />
      <div className="settings-list">
        <SettingsRow
          icon={<UserIcon />}
          label="Аккаунт и Builder"
          value={account ?? 'Подключено'}
          valueTone="muted"
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
        <SettingsRow icon={<GlobeIcon />} label="Среда" value="Aster testnet" valueTone="muted" />
      </div>

      <div className="info-box settings-safe-note">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M12 3.5 19 6v5.2c0 4.6-2.9 7.6-7 9.3-4.1-1.7-7-4.7-7-9.3V6l7-2.5Z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
        </svg>
        <span>Секреты и ключи не хранятся в мини-апе</span>
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

function toneColor(tone: SettingsTone): string {
  switch (tone) {
    case 'success':
      return 'var(--success)'
    case 'warning':
      return 'var(--warning)'
    case 'danger':
      return 'var(--danger)'
    default:
      return 'var(--text-muted)'
  }
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
